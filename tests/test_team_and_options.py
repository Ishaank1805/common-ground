"""Options chosen in the web app rather than on the server: team access for admins, and voice matching for each participant.

Run with: python3 -m unittest tests.test_team_and_options
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import create_app
from tests.support import signed_in


class TeamAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name})
        self.admin = signed_in(self.app.test_client(), username="coordinator")
        self.staff = signed_in(self.app.test_client(), username="ngostaff")
        self.participant = signed_in(self.app.test_client(), username="asha")

    def tearDown(self):
        self.temp.cleanup()

    def me(self, client):
        return client.get("/api/auth/me").get_json()["user"]

    def role(self, client, username, role, on):
        return client.put("/api/admin/team", json={"username": username, "role": role, "on": on})

    def test_the_first_account_runs_the_installation(self):
        self.assertEqual((self.me(self.admin)["admin"], self.me(self.staff)["admin"]), (True, False))
        self.assertEqual(self.admin.get("/api/admin/team").get_json()["team"],
                         [{"username": "coordinator", "admin": True, "insight_viewer": False}])
        self.assertEqual(self.staff.get("/api/admin/team").status_code, 404)
        self.assertEqual(self.role(self.staff, "ngostaff", "insight_viewer", True).status_code, 404)

    def test_admins_choose_who_sees_insights_in_the_app(self):
        self.assertEqual(self.staff.get("/api/insights").status_code, 404)
        self.assertEqual(self.role(self.admin, "NGOstaff", "insight_viewer", True).status_code, 200)  # Any capitalisation.
        self.assertTrue(self.me(self.staff)["insight_viewer"])
        self.assertEqual(self.staff.get("/api/insights").get_json(), {"enough": False, "minimum_people": 5})
        self.assertEqual(self.participant.get("/api/insights").status_code, 404)
        self.assertEqual(self.role(self.admin, "ngostaff", "insight_viewer", False).status_code, 200)
        self.assertEqual(self.staff.get("/api/insights").status_code, 404)

    def test_roles_are_checked_and_there_is_always_an_admin(self):
        self.assertEqual(self.role(self.admin, "nobody", "admin", True).status_code, 404)
        for bad in ({"username": "asha", "role": "owner", "on": True}, {"username": "asha", "role": "admin", "on": "yes"},
                    {"username": "", "role": "admin", "on": True}):
            self.assertEqual(self.admin.put("/api/admin/team", json=bad).status_code, 400, bad)
        self.assertEqual(self.role(self.admin, "coordinator", "admin", False).status_code, 409)  # The last admin stays.
        self.assertEqual(self.role(self.admin, "ngostaff", "admin", True).status_code, 200)
        self.assertEqual(self.role(self.admin, "coordinator", "admin", False).status_code, 200)
        self.assertEqual((self.me(self.admin)["admin"], self.me(self.staff)["admin"]), (False, True))
        self.assertEqual(self.admin.get("/api/admin/team").status_code, 404)


class VoiceChoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name})
        self.client = signed_in(self.app.test_client())

    def tearDown(self):
        self.temp.cleanup()

    def test_voice_matching_is_her_choice_and_turning_it_off_deletes_her_sample(self):
        with patch("app.voice.enabled", return_value=False):
            self.assertEqual(self.client.put("/api/voice/opt-in", json={"on": True}).status_code, 503)
        with patch("app.voice.enabled", return_value=True):
            self.assertEqual(self.client.get("/api/voice").get_json(), {"available": True, "opted_in": False, "enrolled": False})
            self.assertEqual(self.client.put("/api/voice/opt-in", json={"on": "yes"}).status_code, 400)
            self.assertTrue(self.client.put("/api/voice/opt-in", json={"on": True}).get_json()["opted_in"])
            self.assertTrue(self.client.get("/api/auth/me").get_json()["user"]["voice_matching"])
            with closing(sqlite3.connect(Path(self.temp.name) / "sessions.sqlite3")) as conn, conn:
                conn.execute("UPDATE users SET voice_embedding = ?", (json.dumps([0.1] * 192),))
            self.assertTrue(self.client.get("/api/voice").get_json()["enrolled"])
            self.assertEqual(self.client.put("/api/voice/opt-in", json={"on": False}).get_json(),
                             {"available": True, "opted_in": False, "enrolled": False})
        with closing(sqlite3.connect(Path(self.temp.name) / "sessions.sqlite3")) as conn:
            self.assertEqual(conn.execute("SELECT voice_opt_in, voice_embedding FROM users").fetchone(), (0, None))


if __name__ == "__main__":
    unittest.main()
