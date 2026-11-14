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


if __name__ == "__main__":
    unittest.main()
