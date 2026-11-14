"""Quiet prompts during a recorded conversation.

Run with: python3 -m unittest tests.test_facilitator (no API key needed; Gemini is mocked).
"""
import base64
from contextlib import closing
import io
import json
import math
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

import gemini
from app import create_app
from tests.support import signed_in

PREPARATION = {"audience": ["mother", "father"], "topic": "Pursuing higher studies",
               "goals": [{"id": "g1", "description": "Explain why further study matters"},
                         {"id": "g2", "description": "Agree on a next step"}],
               "expectations": {"what_might_go_well": ["My brother may support me"],
                                "possible_barriers": ["They may want me to start working"]}}
PLAN = {"understanding": "Ananth wants to explain why further study matters and agree a next step.",
        "assumptions": [], "steps": [
            {"id": "step1", "goal_ids": ["g1"], "purpose": "Explain the reasons",
             "talking_points": ["Describe the course and where it leads."],
             "example_wording": "I'd like to explain why this course matters to me."},
            {"id": "step2", "goal_ids": ["g2"], "purpose": "Agree on a next step",
             "talking_points": ["Suggest looking at the costs together."],
             "example_wording": "Could we look at the costs together this week?"}]}
STARTED = "2026-11-12T10:00:00.000Z"
PROMPT = {"kind": "respond_to_concern", "text": "Acknowledge the worry about work, then share your plan.",
          "example_wording": "I understand you want me to start working. Here is how I have thought about it.",
          "step_id": "step1"}


def wav(seconds=1.0, rate=16000, channels=1):
    """A synthetic 16-bit window like the browser sends: a speech-band tone."""
    frames = int(seconds * rate)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        samples = (int(6000 * math.sin(2 * math.pi * 220 * i / rate)) for i in range(frames))
        writer.writeframes(b"".join(struct.pack("<h", sample) * channels for sample in samples))
    return buffer.getvalue()


def api_response(content):
    return io.BytesIO(json.dumps({"candidates": [{"finishReason": "STOP", "content": {
        "parts": [{"text": json.dumps(content)}]}}]}).encode())


def guidance(**changes):
    """What gemini.facilitate returns after validation."""
    return {"audio_status": "clear", "notes": "Further study came up.", "current_step_id": "step1",
            "raised_goal_ids": ["g1"], "prompt": None, **changes}


class FacilitatorEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name})
        self.client = signed_in(self.app.test_client())
        created = self.client.post("/api/sessions", json={"preparation": PREPARATION})
        self.assertEqual(created.status_code, 201)
        self.session = created.get_json()
        self.url = f'/api/sessions/{self.session["id"]}'

    def tearDown(self):
        self.temp.cleanup()

    def ready(self, url=None):
        result = self.client.put((url or self.url) + "/chosen-plan", json={"plan": PLAN})
        self.assertEqual(result.status_code, 200, result.get_json())

    def ask(self, audio=None, url=None, **context):
        details = {"mode": "auto", "requested": False, "capture_id": STARTED, "elapsed_seconds": 75, **context}
        return self.client.post((url or self.url) + "/facilitator", content_type="multipart/form-data", data={
            "context": json.dumps(details),
            "audio": (io.BytesIO(wav() if audio is None else audio), "latest.wav", "audio/wav")})

    def database(self, query, parameters=()):
        with closing(sqlite3.connect(Path(self.temp.name) / "sessions.sqlite3")) as conn:
            return conn.execute(query, parameters).fetchall()

    def test_requires_a_saved_plan_and_validates_every_input(self):
        with patch("app.gemini.facilitate") as model:
            self.assertEqual(self.ask().status_code, 409)  # No saved plan yet.
            self.ready()
            for audio in (b"not audio", wav(channels=2), wav(seconds=0), wav(rate=96000)):
                with self.subTest(audio=audio[:12]):
                    self.assertEqual(self.ask(audio=audio).status_code, 422)
            with patch("app.FACILITATOR_MAX_SECONDS", 0.5):  # Same branch as a >2 minute window, without a 4 MB fixture.
                self.assertEqual(self.ask().status_code, 413)
            for change in ({"mode": "always"}, {"requested": "yes"}, {"capture_id": "yesterday"},
                           {"capture_id": "2026-11-12T10:00:00"}, {"elapsed_seconds": -1},
                           {"elapsed_seconds": True}, {"elapsed_seconds": "75"}):
                with self.subTest(change=change):
                    self.assertEqual(self.ask(**change).status_code, 400)
            self.assertEqual(self.client.post(self.url + "/facilitator", json={"mode": "auto"}).status_code, 415)
            no_audio = self.client.post(self.url + "/facilitator", content_type="multipart/form-data", data={
                "context": json.dumps({"mode": "auto", "requested": False, "capture_id": STARTED, "elapsed_seconds": 5})})
            self.assertEqual(no_audio.status_code, 415)
            self.assertEqual(self.ask(url="/api/sessions/missing").status_code, 404)
            model.assert_not_called()
        self.assertEqual(self.database("SELECT COUNT(*) FROM facilitator_events"), [(0,)])

    def test_prompts_follow_one_recording_without_rewriting_the_session(self):
        self.ready()
        other = self.client.post("/api/sessions", json={"preparation": PREPARATION}).get_json()
        other_url = f'/api/sessions/{other["id"]}'
        self.ready(other_url)
        before = self.client.get(self.url).get_json()
        version = self.database("SELECT version FROM sessions WHERE id=?", (self.session["id"],))
        replies = [guidance(prompt=PROMPT),
                   guidance(notes="A next step came up.", current_step_id="step2", raised_goal_ids=[]),
                   guidance(notes="", raised_goal_ids=[]), guidance(notes="", raised_goal_ids=[])]
        with patch("app.gemini.facilitate", side_effect=replies) as model:
            first = self.ask(mode="on_request", requested=True, elapsed_seconds=64)
            self.assertEqual(first.status_code, 200, first.get_json())
            self.assertEqual(first.get_json(), {
                "mode": "on_request", "requested": True, "elapsed_seconds": 64, "audio_seconds": 1.0, "conversation_seconds": None, "language": "en",
                "audio_status": "clear", "current_step_id": "step1", "raised_goal_ids": ["g1"], "prompt": PROMPT})
            second = self.ask(elapsed_seconds=110).get_json()
            # A goal that has come up stays raised, even when a later window doesn't mention it.
            self.assertEqual(second["raised_goal_ids"], ["g1"])
            self.assertEqual(second["current_step_id"], "step2")
            self.assertIsNone(second["prompt"])
            self.assertNotIn("notes", second)  # The model's notes are never shown mid-conversation.
            self.assertNotIn("goal_evidence", second)
            self.ask(capture_id="2026-11-12T11:00:00.000Z")  # A new recording of this conversation.
            self.ask(url=other_url)  # Same start time, different conversation.
        opening, later, new_capture, other_session = (call.args[2] for call in model.call_args_list)
        self.assertEqual((opening["notes"], opening["raised_goal_ids"], opening["recent_prompts"]), ("", [], []))
        self.assertTrue(opening["requested"])
        self.assertAlmostEqual(opening["audio_seconds"], 1.0)
        self.assertEqual(later["notes"], "Further study came up.")
        self.assertEqual(later["raised_goal_ids"], ["g1"])
        self.assertEqual([{key: shown[key] for key in ("kind", "text")} for shown in later["recent_prompts"]],
                         [{"kind": PROMPT["kind"], "text": PROMPT["text"]}])
        self.assertIsInstance(later["recent_prompts"][0]["elapsed_seconds"], int)  # Times let the server avoid repeats.
        for fresh in (new_capture, other_session):
            self.assertEqual((fresh["notes"], fresh["raised_goal_ids"], fresh["recent_prompts"]), ("", [], []))
        self.assertEqual(model.call_args_list[0].args[0]["id"], self.session["id"])
        self.assertEqual(model.call_args_list[0].args[1], wav())  # The uploaded window reaches the model unchanged.
        # Prompts never write the session row, so they cannot conflict with saving the recording.
        self.assertEqual(self.client.get(self.url).get_json(), before)
        self.assertEqual(self.database("SELECT version FROM sessions WHERE id=?", (self.session["id"],)), version)

    def test_prompt_language_is_her_choice(self):
        self.ready()
        with patch("app.gemini.facilitate", return_value=guidance()) as model:
            self.assertEqual(self.ask(language="hi").status_code, 200)
            self.assertEqual(self.ask(language="klingon").status_code, 400)
            self.assertEqual(self.ask().status_code, 200)
        self.assertEqual([call.args[2]["language"] for call in model.call_args_list], ["hi", "en"])

    def test_model_failures_are_safe_and_store_nothing(self):
        self.ready()
        quota = gemini.GeminiError("Gemini's quota or rate limit was reached. Please try again later.")
        with patch("app.gemini.facilitate", side_effect=quota):
            failed = self.ask(requested=True)
        self.assertEqual(failed.status_code, 502)
        self.assertIn("quota", failed.get_json()["error"])
        with patch("app.gemini.facilitate", side_effect=RuntimeError("fake credential must not leak")):
            failed = self.ask()
        self.assertEqual(failed.status_code, 502)
        self.assertNotIn("credential", failed.get_data(as_text=True))
        with patch.dict("os.environ", {"GEMINI_API_KEY": ""}), patch("gemini.urlopen") as transport:
            unconfigured = self.ask(requested=True)
        self.assertEqual(unconfigured.status_code, 502)
        self.assertIn("GEMINI_API_KEY", unconfigured.get_json()["error"])
        transport.assert_not_called()
        self.assertEqual(self.database("SELECT COUNT(*) FROM facilitator_events"), [(0,)])

    def test_end_to_end_with_only_the_gemini_transport_mocked(self):
        self.ready()
        output = {"audio_status": "clear", "notes": "Further study came up.", "current_step_id": "step1",
                  "raised_goals": [{"goal_id": "g1", "evidence": "Why further study matters came up."}],
                  "threat": False, "situation": "unanswered_concern", "situation_evidence": "A worry about work was raised.",
                  "cue": {"text": PROMPT["text"], "example_wording": PROMPT["example_wording"], "step_id": gemini.NO_STEP}}
        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}), \
                patch("gemini.urlopen", return_value=api_response(output)) as transport:
            result = self.ask(mode="on_request", requested=True)
        self.assertEqual(result.status_code, 200, result.get_json())
        self.assertEqual(result.get_json()["prompt"], {**PROMPT, "step_id": None})
        self.assertEqual(transport.call_args.kwargs["timeout"], gemini.FACILITATOR_TIMEOUT)
        body = json.loads(transport.call_args.args[0].data)
        attached = body["contents"][0]["parts"][1]["inlineData"]
        self.assertEqual(attached["mimeType"], "audio/wav")
        self.assertEqual(base64.b64decode(attached["data"]), wav())
        self.assertEqual(body["systemInstruction"]["parts"][0]["text"], gemini.FACILITATOR_PROMPT)

    def opus(self, seconds):
        source = Path(self.temp.name) / "conversation.webm"
        ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
        subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "libopus", str(source)], check=True, capture_output=True)
        return source.read_bytes()

    def ask_with_conversation(self, conversation, mimetype="audio/webm", **context):
        details = {"mode": "on_request", "requested": True, "capture_id": STARTED, "elapsed_seconds": 75, **context}
        return self.client.post(self.url + "/facilitator", content_type="multipart/form-data", data={
            "context": json.dumps(details), "audio": (io.BytesIO(wav()), "latest.wav", "audio/wav"),
            "conversation": (io.BytesIO(conversation), "conversation", mimetype)})

    @unittest.skipUnless(shutil.which("ffmpeg") or Path("/opt/homebrew/bin/ffmpeg").is_file(), "ffmpeg unavailable")
    def test_asking_sends_the_whole_conversation_so_far(self):
        self.ready()
        with patch("app.gemini.facilitate", return_value=guidance(prompt=PROMPT)) as model:
            result = self.ask_with_conversation(self.opus(3))
            self.assertEqual(result.status_code, 200, result.get_json())
            self.assertAlmostEqual(result.get_json()["conversation_seconds"], 3.0, delta=0.2)
            _, window, context, conversation = model.call_args.args
            self.assertEqual(window, wav())  # The latest minute still goes too, as "now".
            self.assertEqual(conversation[4:8], b"ftyp")  # Compacted to M4A on the server.
            self.assertAlmostEqual(context["conversation_seconds"], 3.0, delta=0.2)
            with patch("app.CONVERSATION_MAX_SECONDS", 1):  # Very long conversations keep their latest part.
                self.assertLess(self.ask_with_conversation(self.opus(3)).get_json()["conversation_seconds"], 1.5)
            self.assertEqual(self.ask_with_conversation(self.opus(1), requested=False).status_code, 400)
            self.assertEqual(self.ask_with_conversation(b"not audio").status_code, 422)
            self.assertEqual(self.ask_with_conversation(self.opus(1), mimetype="text/plain").status_code, 415)
            self.assertEqual(model.call_count, 2)

    @unittest.skipUnless(shutil.which("ffmpeg") or Path("/opt/homebrew/bin/ffmpeg").is_file(), "ffmpeg unavailable")
    def test_history_follows_the_saved_recording_and_is_deleted_with_it(self):
        self.ready()
        replies = [guidance(prompt=PROMPT), guidance(), guidance(prompt={**PROMPT, "text": "From a discarded capture."})]
        with patch("app.gemini.facilitate", side_effect=replies):
            self.ask(mode="on_request", requested=True, elapsed_seconds=64)
            self.ask(elapsed_seconds=110)
            self.ask(capture_id="2026-11-12T09:00:00.000Z")
        self.assertEqual(self.client.get(self.url + "/facilitator").get_json(), {"prompts": []})
        listed = {item["id"]: item for item in self.client.get("/api/sessions").get_json()}
        self.assertFalse(listed[self.session["id"]]["recorded"])
        source = Path(self.temp.name) / "sample.webm"
        ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
        subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.2", "-c:a", "libopus", str(source)], check=True, capture_output=True)
        headers = {"X-Recording-Started-At": STARTED, "X-Recording-Ended-At": "2026-11-12T10:05:00.000Z"}
        saved = self.client.post(self.url + "/recording", data=source.read_bytes(), content_type="audio/webm", headers=headers)
        self.assertEqual(saved.status_code, 200, saved.get_json())
        # Only this recording's prompts are listed; the discarded capture's are not.
        self.assertEqual(self.client.get(self.url + "/facilitator").get_json(),
                         {"prompts": [{"elapsed_seconds": 64, "requested": True, **PROMPT}]})
        with patch("app.gemini.facilitate") as model:
            self.assertEqual(self.ask().status_code, 409)  # Prompts end with the recording.
            model.assert_not_called()
        listed = {item["id"]: item for item in self.client.get("/api/sessions").get_json()}
        self.assertTrue(listed[self.session["id"]]["recorded"])
        self.assertEqual(self.client.delete(self.url).status_code, 204)
        self.assertEqual(self.database("SELECT COUNT(*) FROM facilitator_events"), [(0,)])


@patch.dict("os.environ", {"GEMINI_API_KEY": "test-secret"})
class FacilitatorModelTests(unittest.TestCase):
    SESSION = {"id": "s001", "user": {"name": "Ananth", "pronouns": "he/him", "gender": None},
               "preparation": PREPARATION, "chosen_plan": PLAN, "_recording_file": "PRIVATE_FILE",
               "evaluation": {"goals": [], "reflection": "PRIVATE_REFLECTION"}}
    CONTEXT = {"elapsed_seconds": 75, "notes": "Further study came up.", "raised_goal_ids": ["g1", "unknown"],
               "recent_prompts": [{"kind": "next_point", "text": "Bring up the course."}],
               "requested": True, "audio_seconds": 1.0}

    @patch("gemini.urlopen")
    def test_request_sends_plan_and_window_with_a_constrained_schema(self, transport):
        transport.return_value = api_response({
            "threat": False, "audio_status": "unclear", "notes": " A cost concern came up. ", "current_step_id": gemini.NO_STEP,
            "raised_goals": [{"goal_id": "g2", "evidence": "A next step was proposed."}, {"goal_id": "g1", "evidence": " Why the course matters came up. "},
                             {"goal_id": "g1", "evidence": "Said again later."}],
            "situation": "stuck", "situation_evidence": "Her point was followed by silence.",
            "cue": {"text": " Bring up the costs. ", "example_wording": "", "step_id": "step2"}})
        result = gemini.facilitate(self.SESSION, wav(), self.CONTEXT)
        self.assertEqual(result, {"audio_status": "unclear", "notes": "A cost concern came up.", "current_step_id": None,
                                  "raised_goal_ids": ["g1", "g2"], "goal_evidence": {"g1": "Why the course matters came up.", "g2": "A next step was proposed."}, "situation": "stuck", "prompt": {
                                      "kind": "next_point", "text": "Bring up the costs.", "example_wording": "", "step_id": "step2"}})
        body = json.loads(transport.call_args.args[0].data)
        text = body["contents"][0]["parts"][0]["text"]
        sent = json.loads(text)
        self.assertEqual(sent["plan"]["steps"][1]["example_wording"], PLAN["steps"][1]["example_wording"])
        self.assertEqual(sent["conversation_so_far"]["raised_goal_ids"], ["g1"])  # Unknown IDs never reach the model.
        self.assertTrue(sent["requested"])
        self.assertEqual(sent["cue_language"], "English")  # The default when no language is chosen.
        self.assertNotIn("user", sent)
        self.assertNotIn("PRIVATE", text)
        schema = body["generationConfig"]["responseFormat"]["text"]["schema"]
        self.assertEqual(list(schema["properties"]), ["threat", "audio_status", "notes", "current_step_id", "raised_goals", "situation", "situation_evidence", "cue"])
        self.assertEqual(schema["properties"]["current_step_id"]["enum"], ["step1", "step2", gemini.NO_STEP])
        self.assertEqual(schema["properties"]["raised_goals"]["items"]["properties"]["goal_id"]["enum"], ["g1", "g2"])
        self.assertEqual(schema["properties"]["situation"]["enum"], list(gemini.SITUATIONS))
        self.assertEqual(list(schema["properties"]["cue"]["properties"]), ["text", "example_wording", "step_id"])  # The server picks the kind.
        self.assertIn("untrusted", gemini.FACILITATOR_PROMPT)

    def test_validation_rejects_anything_outside_the_contract(self):
        steps, goals = ["step1", "step2"], ["g1", "g2"]
        cue = {"text": "Take a breath; you can ask to finish your point.",
               "example_wording": "Could I finish what I was saying?", "step_id": "step1"}
        valid = {"threat": False, "audio_status": "clear", "notes": "", "current_step_id": "step2", "raised_goals": [],
                 "situation": "heated", "situation_evidence": "Her point was cut off twice.", "cue": cue}
        self.assertEqual(gemini.validate_guidance(valid, steps, goals)["prompt"], {"kind": "pause", **cue})
        self.assertIsNone(gemini.validate_guidance({**valid, "situation": "on_track"}, steps, goals, requested=False)["prompt"])
        for change in ({"current_step_id": "step9"}, {"current_step_id": 2}, {"raised_goals": [{"goal_id": "g9", "evidence": "x"}]},
                       {"raised_goals": "g1"}, {"raised_goals": [{"goal_id": "g1"}]},
                       {"raised_goals": [{"goal_id": "g1", "evidence": None}]}, {"audio_status": "loud"}, {"notes": "x" * 2001}, {"notes": None},
                       {"situation": "alert"}, {"cue": {**cue, "text": " "}}, {"cue": {**cue, "text": "x" * 401}},
                       {"cue": {**cue, "example_wording": None}}, {"cue": {**cue, "step_id": "step9"}}, {"cue": {**cue, "extra": 1}},
                       {"extra": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                gemini.validate_guidance({**valid, **change}, steps, goals)

    def test_safety_cue_always_uses_the_fixed_wording(self):
        for written in ("", "Tell them they are being abusive.", "x" * 900):
            with self.subTest(written=written[:20]):
                result = gemini.validate_guidance({
                    "threat": True, "audio_status": "clear", "notes": "A threat about the topic was made.", "current_step_id": "step1",
                    "raised_goals": [], "situation": "heated", "situation_evidence": "A threat was made.",
                    "cue": {"text": written[:300], "example_wording": written[:400], "step_id": "step1"}}, ["step1", "step2"], ["g1", "g2"])
                self.assertEqual(result["prompt"], {"kind": "step_away", "text": gemini.STEP_AWAY_TEXT,
                                                    "example_wording": gemini.STEP_AWAY_WORDING, "step_id": None,
                                                    "help": gemini.STEP_AWAY_HELP})
        # Visible wording is neutral in case others see the screen; helplines are kept apart.
        for alarming in ("112", "181", "unsafe", "helpline"):
            self.assertNotIn(alarming, gemini.STEP_AWAY_TEXT.lower())
        self.assertIn("call 112", gemini.STEP_AWAY_HELP)
        self.assertIn("Women Helpline on 181", gemini.STEP_AWAY_HELP)
        self.assertIn("First set threat", gemini.FACILITATOR_PROMPT)

    @patch("gemini.urlopen")
    def test_asking_sends_the_whole_conversation_then_the_latest_minute(self, transport):
        transport.return_value = api_response({"threat": False, "audio_status": "clear", "notes": "Fees came up.", "current_step_id": "step1",
                                               "raised_goals": [{"goal_id": "g1", "evidence": "Fees came up."}], "situation": "on_track", "situation_evidence": "", "cue": {"text": "Bring up the course.", "example_wording": "", "step_id": "no step"}})
        gemini.facilitate(self.SESSION, wav(), {**self.CONTEXT, "conversation_seconds": 312.46}, conversation=b"M4A-CONVERSATION")
        self.assertEqual(transport.call_args.kwargs["timeout"], gemini.FACILITATOR_CONVERSATION_TIMEOUT)
        parts = json.loads(transport.call_args.args[0].data)["contents"][0]["parts"]
        self.assertEqual([part["inlineData"]["mimeType"] for part in parts[1:]], ["audio/m4a", "audio/wav"])
        self.assertEqual(base64.b64decode(parts[1]["inlineData"]["data"]), b"M4A-CONVERSATION")
        self.assertEqual(base64.b64decode(parts[2]["inlineData"]["data"]), wav())
        self.assertEqual(json.loads(parts[0]["text"])["conversation_audio_seconds"], 312.5)
        self.assertIn("first the whole\nconversation so far", gemini.FACILITATOR_PROMPT)

    def test_cues_stay_truthful_on_her_side_and_in_her_language(self):
        for rule in ("argue the other side's position", "never claim they have prepared", "question or an offer",
                     "if you cannot point to", "example_wording in cue_language", "translate plan wording naturally"):
            self.assertIn(rule, gemini.FACILITATOR_PROMPT)

    def test_goals_count_only_with_evidence_from_the_conversation(self):
        result = gemini.validate_guidance({
            "audio_status": "clear", "notes": "", "current_step_id": "step1", "threat": False, "situation": "on_track", "situation_evidence": "",
            "cue": {"text": "Bring up the course.", "example_wording": "", "step_id": "no step"},
            "raised_goals": [{"goal_id": "g1", "evidence": "Why the course matters was explained."},
                             {"goal_id": "g2", "evidence": "   "}]}, ["step1", "step2"], ["g1", "g2"])
        self.assertEqual(result["raised_goal_ids"], ["g1"])  # g2 had no evidence, so it is dropped.
        self.assertEqual(result["goal_evidence"], {"g1": "Why the course matters was explained."})

    @patch("gemini.urlopen")
    def test_cues_use_the_chosen_language(self, transport):
        transport.return_value = api_response({"threat": False, "audio_status": "clear", "notes": "", "current_step_id": "step1",
                                               "raised_goals": [], "situation": "on_track", "situation_evidence": "", "cue": {"text": "Bring up the course.", "example_wording": "", "step_id": "no step"}})
        gemini.facilitate(self.SESSION, wav(), {**self.CONTEXT, "language": "hi"})
        sent = json.loads(json.loads(transport.call_args.args[0].data)["contents"][0]["parts"][0]["text"])
        self.assertEqual(sent["cue_language"], "Hindi in Devanagari script")

    def test_notes_are_asked_to_record_topics_not_people(self):
        self.assertIn("never name anyone", gemini.FACILITATOR_PROMPT)
        self.assertIn("never address anyone as 'you'", gemini.FACILITATOR_PROMPT)

    @patch("gemini.urlopen")
    def test_invalid_output_and_missing_plan_are_safe_errors(self, transport):
        with self.assertRaises(gemini.GeminiError):
            gemini.facilitate({**self.SESSION, "chosen_plan": None}, wav(), self.CONTEXT)
        transport.assert_not_called()
        transport.return_value = api_response({"threat": False, "audio_status": "clear", "notes": "", "current_step_id": "step9",
                                               "raised_goals": [], "situation": "on_track", "situation_evidence": "", "cue": {"text": "Bring up the course.", "example_wording": "", "step_id": "no step"}})
        with self.assertRaisesRegex(gemini.GeminiError, "recording continues"):
            gemini.facilitate(self.SESSION, wav(), self.CONTEXT)


if __name__ == "__main__":
    unittest.main()
