"""Instant prompts in the app. Run with: python3 -m unittest tests.test_live_app"""
import importlib.util
import tempfile
import unittest
from unittest.mock import patch

from app import create_app


class LiveAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name})

    def tearDown(self):
        self.temp.cleanup()

    def test_instant_prompts_are_offered_only_when_they_can_run(self):
        client = self.app.test_client()
        with patch("app.live.enabled", return_value=False):
            self.assertFalse(client.get("/api/config").get_json()["live_prompts"])
        with patch("app.live.enabled", return_value=True):
            self.assertTrue(client.get("/api/config").get_json()["live_prompts"])

    @unittest.skipUnless(importlib.util.find_spec("flask_sock"), "flask-sock unavailable")
    def test_the_live_socket_is_routed_and_needs_a_sign_in(self):
        self.assertIn("/api/sessions/<session_id>/live", {rule.rule for rule in self.app.url_map.iter_rules()})
        self.assertEqual(self.app.test_client().get("/api/sessions/anything/live").status_code, 401)



class InspectTests(unittest.TestCase):
    """The optional view of exactly what goes to Gemini and what comes back, for regular prompts."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name})

    def tearDown(self):
        self.temp.cleanup()

    @patch.dict("os.environ", {"GEMINI_API_KEY": "test-secret"})
    @patch("gemini.urlopen")
    def test_facilitate_traces_exactly_what_went_in_and_came_back(self, transport):
        import gemini
        from tests.test_facilitator import PLAN, PREPARATION, api_response, wav
        raw = {"threat": False, "audio_status": "clear", "notes": "Fees came up.", "current_step_id": "step1", "raised_goals": [],
               "situation": "unanswered_concern", "situation_evidence": "A worry about fees was left unanswered.",
               "cue": {"text": "Acknowledge the fees.", "example_wording": "Could I explain why this matters to me?", "step_id": "step1"}}
        transport.return_value = api_response(raw)
        trace = {}
        gemini.facilitate({"preparation": PREPARATION, "chosen_plan": PLAN}, wav(),
                          {"elapsed_seconds": 30, "requested": True, "audio_seconds": 1.0}, trace=trace)
        self.assertEqual(trace["output"], raw)
        self.assertEqual(trace["input"]["instruction"], gemini.FACILITATOR_PROMPT)
        self.assertTrue(trace["input"]["data"]["requested"])
        self.assertEqual(trace["input"]["audio"], [{"type": "audio/wav", "seconds": 1.0}])

    def test_the_reply_includes_input_and_output_only_when_asked(self):
        import io
        import json
        from tests.support import signed_in
        from tests.test_facilitator import PLAN, PREPARATION, STARTED, guidance, wav
        client = signed_in(self.app.test_client())
        url = "/api/sessions/" + client.post("/api/sessions", json={"preparation": PREPARATION}).get_json()["id"]
        self.assertEqual(client.put(url + "/chosen-plan", json={"plan": PLAN}).status_code, 200)

        def fake(session, audio, context, conversation=None, trace=None):
            if trace is not None:
                trace.update({"input": {"data": {"requested": False}}, "output": {"situation": "on_track"}})
            return guidance()

        def ask(inspect):
            return client.post(url + "/facilitator", content_type="multipart/form-data", data={
                "context": json.dumps({"mode": "auto", "requested": False, "capture_id": STARTED, "elapsed_seconds": 30, "inspect": inspect}),
                "audio": (io.BytesIO(wav()), "latest.wav", "audio/wav")})
        with patch("app.gemini.facilitate", side_effect=fake):
            plain = ask(False)
            self.assertEqual(plain.status_code, 200, plain.get_json())
            self.assertNotIn("inspect", plain.get_json())
            self.assertEqual(ask(True).get_json()["inspect"], {"input": {"data": {"requested": False}}, "output": {"situation": "on_track"}})


if __name__ == "__main__":
    unittest.main()
