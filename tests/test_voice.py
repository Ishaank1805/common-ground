"""Optional voice matching. Run with: python3 -m unittest tests.test_voice

Most tests replace the voice models with stand-ins, so they run without PyTorch. The last test
uses the real models on real recorded speech when VOICE_SAMPLES points to a folder of
spk1_snt1..6.wav and spk2_snt1..6.wav (scripts/evaluate_voice.py downloads them).
"""
from contextlib import closing
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

import gemini
import voice
from app import create_app
from tests.support import signed_in
from tests.test_facilitator import PLAN, PREPARATION, STARTED, api_response, guidance, wav

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
EMBEDDING = [0.1] * 192
TIMELINE = {"segments": [{"start": 0.0, "end": 0.6, "speaker": "her"}], "seconds": {"her": 0.6, "family": 0.0, "unclear": 0.0}}


class VoiceSampleTests(unittest.TestCase):
    def setUp(self):
        self.temp, self.scratch = tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name})
        self.client = signed_in(self.app.test_client())

    def tearDown(self):
        self.temp.cleanup()
        self.scratch.cleanup()

    def stored(self):
        with closing(sqlite3.connect(self.data / "sessions.sqlite3")) as conn:
            return conn.execute("SELECT voice_embedding FROM users").fetchone()[0]

    @unittest.skipUnless(Path(FFMPEG).is_file(), "ffmpeg unavailable")
    def test_a_sample_keeps_only_a_fingerprint_and_can_be_deleted(self):
        clip = Path(self.scratch.name) / "sample.webm"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=1", "-c:a", "libopus", str(clip)],
                       check=True, capture_output=True)
        with patch("app.voice.enabled", return_value=True), patch("app.voice.enrol", return_value=(EMBEDDING, 5.2)) as enrol:
            self.assertEqual(self.client.post("/api/voice", data=clip.read_bytes(), content_type="audio/webm").status_code, 409)  # Not opted in.
            self.assertTrue(self.client.put("/api/voice/opt-in", json={"on": True}).get_json()["opted_in"])
            saved = self.client.post("/api/voice", data=clip.read_bytes(), content_type="audio/webm;codecs=opus")
            self.assertEqual(saved.status_code, 200, saved.get_json())
            self.assertEqual(saved.get_json(), {"enrolled": True, "speech_seconds": 5.2})
            self.assertAlmostEqual(len(enrol.call_args.args[0]) / voice.RATE, 1.0, delta=0.1)  # Decoded to 16 kHz mono.
            self.assertEqual(self.client.get("/api/voice").get_json(), {"available": True, "opted_in": True, "enrolled": True})
            self.assertTrue(self.client.get("/api/auth/me").get_json()["user"]["voice_sample"])
            self.assertEqual(json.loads(self.stored()), EMBEDDING)
            self.assertEqual(sorted(path.name for path in self.data.iterdir()), ["recordings", "sessions.sqlite3"])  # No audio kept.
            self.assertEqual(self.client.post("/api/voice", data=b"x", content_type="text/plain").status_code, 415)
            enrol.side_effect = ValueError("The sample needs at least 4 seconds of clear speech.")
            refused = self.client.post("/api/voice", data=clip.read_bytes(), content_type="audio/webm")
            self.assertEqual(refused.status_code, 422)
            self.assertIn("4 seconds", refused.get_json()["error"])
        self.assertEqual(self.client.delete("/api/voice").get_json(), {"enrolled": False})
        self.assertIsNone(self.stored())
        self.assertFalse(self.client.get("/api/auth/me").get_json()["user"]["voice_sample"])

    @unittest.skipUnless(Path(FFMPEG).is_file(), "ffmpeg unavailable")
    def test_a_tap_labels_the_conversation_once_and_only_its_latest_minutes(self):
        url = "/api/sessions/" + self.client.post("/api/sessions", json={"preparation": PREPARATION}).get_json()["id"]
        self.assertEqual(self.client.put(url + "/chosen-plan", json={"plan": PLAN}).status_code, 200)
        with closing(sqlite3.connect(self.data / "sessions.sqlite3")) as conn, conn:
            conn.execute("UPDATE users SET voice_embedding=?, voice_opt_in=1", (json.dumps(EMBEDDING),))
        clip = Path(self.scratch.name) / "conversation.webm"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=2", "-c:a", "libopus", str(clip)],
                       check=True, capture_output=True)
        with patch("app.voice.enabled", return_value=True), patch("app.voice.timeline", return_value=TIMELINE) as timeline, \
                patch("app.gemini.facilitate", return_value=guidance()) as model:
            reply = self.client.post(url + "/facilitator", content_type="multipart/form-data", data={
                "context": json.dumps({"mode": "on_request", "requested": True, "capture_id": STARTED, "elapsed_seconds": 30}),
                "audio": (io.BytesIO(wav()), "latest.wav", "audio/wav"),
                "conversation": (io.BytesIO(clip.read_bytes()), "conversation", "audio/webm")})
            self.assertEqual(reply.status_code, 200, reply.get_json())
        self.assertEqual(model.call_args.args[2]["speakers"], {"conversation": TIMELINE})
        self.assertEqual(timeline.call_count, 1)  # The latest minute is not labelled a second time.
        self.assertEqual(timeline.call_args.kwargs, {"last_seconds": voice.LABEL_SECONDS})
        self.assertAlmostEqual(len(timeline.call_args.args[0]) / voice.RATE, 2.0, delta=0.2)  # The decoded conversation.

    def test_without_the_libraries_voice_matching_is_simply_off(self):
        with patch("app.voice.enabled", return_value=False):
            self.assertEqual(self.client.post("/api/voice", data=b"x", content_type="audio/webm").status_code, 503)
            self.assertFalse(self.client.get("/api/config").get_json()["voice_matching"])
            self.assertFalse(self.client.get("/api/voice").get_json()["available"])

    def test_prompts_get_a_speaker_timeline_only_with_a_sample(self):
        url = "/api/sessions/" + self.client.post("/api/sessions", json={"preparation": PREPARATION}).get_json()["id"]
        self.assertEqual(self.client.put(url + "/chosen-plan", json={"plan": PLAN}).status_code, 200)

        def ask():
            return self.client.post(url + "/facilitator", content_type="multipart/form-data", data={
                "context": json.dumps({"mode": "auto", "requested": False, "capture_id": STARTED, "elapsed_seconds": 30}),
                "audio": (io.BytesIO(wav()), "latest.wav", "audio/wav")})
        with patch("app.voice.enabled", return_value=True), patch("app.voice.timeline", return_value=TIMELINE) as timeline, \
                patch("app.voice.samples_from_wav", return_value="samples"), patch("app.gemini.facilitate", return_value=guidance()) as model:
            self.assertEqual(ask().status_code, 200)  # No sample yet: no timeline.
            self.assertIsNone(model.call_args.args[2]["speakers"])
            timeline.assert_not_called()
            with closing(sqlite3.connect(self.data / "sessions.sqlite3")) as conn, conn:
                conn.execute("UPDATE users SET voice_embedding=?, voice_opt_in=1", (json.dumps(EMBEDDING),))
            reply = ask()
            self.assertEqual(reply.status_code, 200)
            self.assertNotIn("talk_seconds", reply.get_json())
            self.assertEqual(model.call_args.args[2]["speakers"], {"latest": TIMELINE})
            self.assertEqual(timeline.call_args.args, ("samples", EMBEDDING))
            timeline.side_effect = RuntimeError("model crashed")
            self.assertEqual(ask().status_code, 200)  # A failure in voice matching never blocks a prompt.
            self.assertIsNone(model.call_args.args[2]["speakers"])
        with closing(sqlite3.connect(self.data / "sessions.sqlite3")) as conn:
            talk = [json.loads(body).get("talk_seconds") for (body,) in conn.execute("SELECT body FROM facilitator_events ORDER BY id")]
        self.assertEqual(talk, [None, {"latest": TIMELINE["seconds"]}, None])


class VoicePromptTests(unittest.TestCase):
    @patch.dict("os.environ", {"GEMINI_API_KEY": "test-secret"})
    @patch("gemini.urlopen")
    def test_the_model_receives_the_timeline_and_its_rules(self, transport):
        transport.return_value = api_response({"threat": False, "audio_status": "clear", "notes": "", "current_step_id": "step1",
                                               "raised_goals": [], "situation": "on_track", "situation_evidence": "", "cue": {"text": "Bring up the course.", "example_wording": "Could I explain why this matters to me?", "step_id": "no step"}})
        gemini.facilitate({"preparation": PREPARATION, "chosen_plan": PLAN}, wav(),
                          {"elapsed_seconds": 30, "requested": False, "audio_seconds": 1.0, "speakers": {"latest": TIMELINE}})
        sent = json.loads(json.loads(transport.call_args.args[0].data)["contents"][0]["parts"][0]["text"])
        self.assertEqual(sent["speaker_timeline"], {"latest": TIMELINE["segments"]})
        for rule in ("a threat counts even when the timeline marks it", "Do not guess who is speaking from voices", "Treat 'unclear' as unknown", "never write 'the person'",
                     "someone else responded to it", "told to be quiet or refused is heated", "covers only the last few minutes"):
            self.assertIn(rule, gemini.FACILITATOR_PROMPT)

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "numpy unavailable")
    def test_long_conversations_label_only_the_latest_minutes(self):
        import numpy as np
        with patch("voice.speech_segments", return_value=[(0.5, 3.0)]) as segments, \
                patch("voice._embed", side_effect=lambda chunks: np.ones((len(chunks), 2), "float32") / np.sqrt(2)):
            result = voice.timeline(np.zeros(10 * voice.RATE, "float32"), [1.0, 1.0], last_seconds=4)
        self.assertEqual(len(segments.call_args.args[0]), 4 * voice.RATE)  # Only the last 4 seconds were analysed...
        self.assertEqual(result["segments"], [{"start": 6.5, "end": 9.0, "speaker": "unclear"}])  # ...times from the start; a lone voice stays unclear.

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "numpy unavailable")
    def test_voices_are_judged_against_each_other_not_a_fixed_number(self):
        import numpy as np
        unit = lambda *v: np.array(v, "float32") / np.linalg.norm(v)
        reference = unit(1, 0, 0)
        def labels(*vectors):
            embeddings = np.stack(vectors)
            return voice.contrast_labels(embeddings, embeddings @ reference)
        her, family = unit(0.95, 0.31, 0), unit(0.05, 0, 1)
        self.assertEqual(labels(her, family, her, family), ["her", "family", "her", "family"])  # Clearly different voices.
        similar = unit(0.6, -0.3, 0.74)  # Another voice scoring 0.6 against her sample: above the old threshold.
        close = unit(0.8, 0.6, 0)
        self.assertEqual(labels(close, similar, close, similar), ["unclear"] * 4)  # Too alike to tell: never a wrong label.
        self.assertEqual(labels(her, her), ["unclear", "unclear"])  # A lone voice can't be checked against anyone.
        self.assertEqual(voice.contrast_labels(np.zeros((0, 3), "float32"), np.zeros(0, "float32")), [])

    def test_voice_matching_is_offered_when_installed_unless_the_server_switches_it_off(self):
        with patch.dict("os.environ", {"VOICE_MATCHING": ""}), patch("voice.available", return_value=True):
            self.assertTrue(voice.enabled())
        with patch.dict("os.environ", {"VOICE_MATCHING": "0"}), patch("voice.available", return_value=True):
            self.assertFalse(voice.enabled())
        with patch.dict("os.environ", {"VOICE_MATCHING": ""}), patch("voice.available", return_value=False):
            self.assertFalse(voice.enabled())

    def test_a_threat_always_gets_the_safety_message(self):
        steps, goals = ["step1", "step2"], ["g1", "g2"]
        reply = {"threat": False, "audio_status": "clear", "notes": "", "current_step_id": "step1", "raised_goals": [],
                 "situation": "stuck", "situation_evidence": "Her point was met with silence.",
                 "cue": {"text": "Explain why it matters.", "example_wording": "Could I explain why this matters to me?", "step_id": "step1"}}
        self.assertEqual(gemini.validate_guidance({**reply, "threat": True}, steps, goals)["prompt"]["kind"], "step_away")
        self.assertEqual(gemini.validate_guidance(reply, steps, goals)["prompt"]["kind"], "next_point")
        with self.assertRaises(ValueError):
            gemini.validate_guidance({**reply, "threat": "yes"}, steps, goals)
        missing = dict(reply)
        missing.pop("threat")
        with self.assertRaises(ValueError):
            gemini.validate_guidance(missing, steps, goals)
        self.assertIn("First set threat", gemini.FACILITATOR_PROMPT)

    def test_labels_follow_the_thresholds(self):
        self.assertEqual([voice.label(score) for score in (None, voice.HER, voice.FAMILY, (voice.HER + voice.FAMILY) / 2)],
                         ["unclear", "her", "family", "unclear"])


@unittest.skipUnless(voice.available() and os.environ.get("VOICE_SAMPLES"), "set VOICE_SAMPLES to run the real voice models")
class RealVoiceTests(unittest.TestCase):
    def load(self, name):
        with wave.open(str(Path(os.environ["VOICE_SAMPLES"]) / name)) as reader:
            return voice.samples_from_pcm(reader.readframes(reader.getnframes()))

    def test_loads_from_its_own_folder_offline_even_with_a_broken_hf_home(self):
        if not (voice.MODEL_DIR / "hyperparams.yaml").exists():
            self.skipTest("model not downloaded yet")
        code = ("import voice; voice._prepare_environment()\n"
                "import huggingface_hub\n"
                "def refuse(*args, **kwargs): raise RuntimeError('tried to use the network')\n"
                "huggingface_hub.hf_hub_download = refuse\n"
                "voice._load(); print('loaded offline')")
        env = {**os.environ, "HF_HOME": "/nonexistent/broken/hf"}
        env.pop("HF_HUB_OFFLINE", None)
        result = subprocess.run([sys.executable, "-W", "ignore", "-c", code], cwd=Path(voice.__file__).parent,
                                env=env, capture_output=True, text=True, timeout=180)
        self.assertIn("loaded offline", result.stdout, result.stderr[-2000:])

    def test_real_voices_are_told_apart(self):
        import numpy as np
        gap = np.zeros(voice.RATE // 2, "float32")
        embedding, speech = voice.enrol(np.concatenate([self.load(f"spk1_snt{take}.wav") for take in (1, 2, 3)] + [gap]))
        self.assertGreaterEqual(speech, voice.MIN_SAMPLE_SPEECH_SECONDS)
        conversation = np.concatenate([self.load("spk1_snt4.wav"), gap, self.load("spk2_snt1.wav"), gap,
                                       self.load("spk1_snt5.wav"), gap, self.load("spk2_snt2.wav"), gap])
        result = voice.timeline(conversation, embedding)
        self.assertEqual([s["speaker"] for s in result["segments"] if s["speaker"] != "unclear"], ["her", "family", "her", "family"])
        self.assertGreater(result["seconds"]["her"], 3)
        self.assertGreater(result["seconds"]["family"], 2)
        alone = voice.timeline(np.concatenate([self.load("spk1_snt6.wav"), gap]), embedding)["seconds"]
        self.assertEqual(alone["her"], 0)  # Her voice alone can't be checked against anyone: unclear, never assumed.


if __name__ == "__main__":
    unittest.main()
