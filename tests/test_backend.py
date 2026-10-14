"""Run with python -m unittest tests.test_backend (no API key needed)."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from app import create_app

PREPARATION = {"audience": ["mother", "father"], "topic": "Taking a course",
               "goals": [{"id": "g1", "description": "Explain why it matters"}],
               "expectations": {"what_might_go_well": [], "possible_barriers": ["Time concerns"]}}
PLAN = {"understanding": "Explain the course", "assumptions": [], "steps": [
    {"id": "step1", "goal_ids": ["g1"], "purpose": "Open and discuss", "talking_points": ["Explain the course", "Ask about concerns", "Agree a next step"], "example_wording": "Could we talk about this course?"}]}
EVALUATION = {"goals": [{"goal_id": "g1", "achievement": "I explained why it matters.", "assessment": "achieved", "explanation": "", "next_step": "Discuss the schedule"}], "reflection": "They asked more questions than expected.", "state": "completed"}


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name})
        self.client = self.app.test_client()
        created = self.client.post("/api/sessions", json={"preparation": PREPARATION})
        self.assertEqual(created.status_code, 201)
        self.session = created.get_json()
        self.url = f'/api/sessions/{self.session["id"]}'

    def tearDown(self):
        self.temp.cleanup()

    def ready(self):
        result = self.client.put(self.url + "/chosen-plan", json={"plan": PLAN})
        self.assertEqual(result.status_code, 200, result.get_json())

    def test_validation_and_session_isolation(self):
        invalid = copy.deepcopy(PREPARATION)
        invalid["goals"].append(invalid["goals"][0])
        self.assertEqual(self.client.post("/api/sessions", json={"preparation": invalid}).status_code, 400)
        self.assertEqual(self.client.put(self.url + "/evaluation", json={"goals": [{"goal_id": "foreign"}]}).status_code, 400)
        self.assertEqual(self.client.put(self.url + "/evaluation", json={"goals": [{"goal_id": []}]}).status_code, 400)
        other = self.client.post("/api/sessions", json={"preparation": PREPARATION}).get_json()
        self.client.put(self.url + "/evaluation", json=EVALUATION)
        self.assertEqual(self.client.get(f'/api/sessions/{other["id"]}').get_json()["evaluation"]["revision"], 0)
        self.assertEqual(self.client.get("/api/sessions/missing").status_code, 404)

    def test_clarification_failure_preserves_answers_and_preparation_reset(self):
        output = {"session_id": self.session["id"], "status": "clarification_required", "questions": [{"id": "q1", "field": "preparation.topic", "question": "How long is the course?"}], "plan": None}
        with patch("app.gemini.generate_plan", return_value=output):
            self.assertEqual(self.client.post(self.url + "/plan", json={}).status_code, 200)
        answer = [{"question_id": "q1", "answer": "Not sure"}]
        with patch("app.gemini.generate_plan", side_effect=RuntimeError("fake credential must not leak")):
            failed = self.client.post(self.url + "/plan", json={"clarification_answers": answer})
        self.assertEqual(failed.status_code, 502)
        self.assertNotIn("credential", failed.get_data(as_text=True))
        self.assertEqual(self.client.get(self.url).get_json()["clarification_answers"], answer)
        changed = copy.deepcopy(PREPARATION)
        changed["topic"] = "A different course"
        updated = self.client.put(self.url + "/preparation", json={"preparation": changed}).get_json()
        self.assertEqual(updated["questions"], [])
        self.assertEqual(updated["clarification_answers"], [])
        self.assertIsNone(updated["model_response"])

    def test_model_result_cannot_overwrite_concurrent_input(self):
        changed = copy.deepcopy(PREPARATION)
        changed["topic"] = "New topic during inference"
        def generate(*_args):
            def change():
                with self.app.test_client() as concurrent:
                    return concurrent.put(self.url + "/preparation", json={"preparation": changed}).status_code
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertEqual(pool.submit(change).result(), 200)
            return {"session_id": self.session["id"], "status": "plan_ready", "questions": [], "plan": PLAN}
        with patch("app.gemini.generate_plan", side_effect=generate):
            result = self.client.post(self.url + "/plan", json={})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.client.get(self.url).get_json()["preparation"]["topic"], changed["topic"])
        self.assertIsNone(self.client.get(self.url).get_json()["model_response"])

    def test_evaluation_summary_revisions_and_reopening(self):
        self.assertEqual(self.client.post(self.url + "/summary", json={}).status_code, 400)
        self.assertEqual(self.client.put(self.url + "/evaluation", json={"goals": [], "state": "completed"}).status_code, 400)
        self.assertEqual(self.client.put(self.url + "/evaluation", json=EVALUATION).get_json()["evaluation"]["revision"], 1)
        self.assertEqual(self.client.put(self.url + "/evaluation", json=EVALUATION).get_json()["evaluation"]["revision"], 1)
        with patch("app.gemini.summarize_evaluation", return_value="Ananth explained why the course matters."):
            result = self.client.post(self.url + "/summary", json={}).get_json()
        self.assertEqual(result["summary"]["source_revision"], 1)
        self.assertFalse(result["summary"]["includes_audio"])
        self.client.put(self.url + "/summary", json={"text": "My edited summary"})
        revised = copy.deepcopy(EVALUATION)
        revised["reflection"] = "A revised reflection."
        result = self.client.put(self.url + "/evaluation", json=revised).get_json()
        self.assertTrue(result["summary"]["outdated"])
        self.assertEqual(result["summary"]["text"], "My edited summary")
        changed = copy.deepcopy(PREPARATION)
        changed["topic"] = "Trying to overwrite original intent"
        self.assertEqual(self.client.put(self.url + "/preparation", json={"preparation": changed}).status_code, 409)
        reopened = create_app({"TESTING": True, "DATA_DIR": self.temp.name}).test_client().get(self.url).get_json()
        self.assertEqual(reopened["evaluation"]["reflection"], revised["reflection"])
        self.assertEqual(self.client.delete(self.url).status_code, 204)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_request_boundary(self):
        self.assertEqual(self.client.delete(self.url, headers={"Origin": "https://malicious.example"}).status_code, 403)
        self.assertEqual(self.client.put(self.url + "/evaluation", data='{}', content_type="text/plain").status_code, 415)
        self.assertEqual(self.client.post("/api/sessions", json={"preparation": None}).status_code, 400)
        self.assertEqual(self.client.post("/api/sessions", json=[]).status_code, 400)
        self.assertNotIn("api_key", json.dumps(self.client.get("/api/config").get_json()).lower())

    @unittest.skipUnless(shutil.which("ffmpeg") or Path("/opt/homebrew/bin/ffmpeg").is_file(), "ffmpeg unavailable")
    def test_real_recording_mp4_range_and_delete(self):
        self.ready()
        self.client.put(self.url + "/evaluation", json=EVALUATION)
        with patch("app.gemini.summarize_evaluation", return_value="Written evaluation only."):
            self.client.post(self.url + "/summary", json={})
        source = Path(self.temp.name) / "sample.webm"
        ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
        subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.2", "-c:a", "libopus", str(source)], check=True, capture_output=True)
        headers = {"X-Recording-Started-At": "2026-10-14T12:00:00Z", "X-Recording-Ended-At": "2026-10-14T12:00:01Z"}
        invalid = self.client.post(self.url + "/recording", data=b"not media", content_type="audio/webm", headers=headers)
        self.assertEqual(invalid.status_code, 422)
        self.assertIsNone(self.client.get(self.url).get_json()["recording"])
        result = self.client.post(self.url + "/recording", data=source.read_bytes(), content_type="audio/webm;codecs=opus", headers=headers)
        self.assertEqual(result.status_code, 200, result.get_json())
        saved = result.get_json()
        self.assertTrue(saved["summary"]["outdated"])
        self.assertGreater(saved["recording"]["duration_seconds"], 0)
        self.assertNotIn("_recording_file", saved)
        playback = self.client.get(saved["recording"]["url"], headers={"Range": "bytes=0-31"})
        self.assertEqual(playback.status_code, 206)
        self.assertIn(b"ftyp", playback.data)
        playback.close()
        # A retry after a lost success response must recover the saved recording.
        retried = self.client.post(self.url + "/recording", data=source.read_bytes(), content_type="audio/webm", headers=headers)
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.get_json()["recording"], saved["recording"])
        self.assertEqual(len(list((Path(self.temp.name) / "recordings").iterdir())), 1)
        different_capture = {**headers, "X-Recording-Ended-At": "2026-10-14T12:00:02Z"}
        self.assertEqual(self.client.post(self.url + "/recording", data=source.read_bytes(), content_type="audio/webm", headers=different_capture).status_code, 409)
        # Summary receives this session's actual persisted AAC/MP4 bytes, not a URL.
        recording_path = next((Path(self.temp.name) / "recordings").iterdir())
        with patch("app.gemini.summarize_evaluation", return_value="Audio and reflection.") as model:
            summarized = self.client.post(self.url + "/summary", json={})
        self.assertEqual(summarized.status_code, 200)
        self.assertTrue(summarized.json["summary"]["includes_audio"])
        self.assertFalse(summarized.json["summary"]["outdated"])
        self.assertEqual(model.call_args.args[1], recording_path.read_bytes())
        # Missing or oversized audio must not silently become a text-only summary.
        with patch("app.gemini.MAX_AUDIO_BYTES", 1), patch("app.gemini.summarize_evaluation") as model:
            self.assertEqual(self.client.post(self.url + "/summary", json={}).status_code, 413)
            model.assert_not_called()
        recording_path.unlink()
        with patch("app.gemini.summarize_evaluation") as model:
            self.assertEqual(self.client.post(self.url + "/summary", json={}).status_code, 409)
            model.assert_not_called()
        self.assertEqual(self.client.get(self.url).json["summary"]["text"], "Audio and reflection.")
        self.assertEqual(self.client.delete(self.url).status_code, 204)
        self.assertEqual(list((Path(self.temp.name) / "recordings").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
