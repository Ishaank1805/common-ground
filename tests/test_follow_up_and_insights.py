"""Follow-up when a conversation hasn't happened yet, and anonymous insights for the NGO.

Run with: python3 -m unittest tests.test_follow_up_and_insights
"""
import tempfile
import unittest
from unittest.mock import patch

from app import create_app
from tests.support import signed_in
from tests.test_facilitator import PLAN, PREPARATION


class FollowUpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name})
        self.client = signed_in(self.app.test_client())
        self.session_id = self.client.post("/api/sessions", json={"preparation": PREPARATION}).get_json()["id"]
        self.url = f"/api/sessions/{self.session_id}"

    def tearDown(self):
        self.temp.cleanup()

    def listed(self):
        return next(item for item in self.client.get("/api/sessions").get_json() if item["id"] == self.session_id)

    def test_her_list_shows_where_each_conversation_stands(self):
        self.assertEqual(self.listed()["status"], "preparing")
        self.assertEqual(self.client.put(self.url + "/chosen-plan", json={"plan": PLAN}).status_code, 200)
        self.assertEqual(self.listed()["status"], "planned")
        saved = self.client.put(self.url + "/follow-up", json={"status": "not_yet", "reason": "felt_nervous",
                                                                "next_try": "2026-11-20", "note": "Try after dinner."})
        self.assertEqual(saved.status_code, 200, saved.get_json())
        self.assertEqual({key: saved.get_json()["follow_up"][key] for key in ("status", "reason", "next_try", "note")},
                         {"status": "not_yet", "reason": "felt_nervous", "next_try": "2026-11-20", "note": "Try after dinner."})
        self.assertEqual((self.listed()["status"], self.listed()["next_try"]), ("not_yet", "2026-11-20"))
        self.assertEqual(self.client.put(self.url + "/follow-up", json={"status": "happened"}).status_code, 200)
        self.assertEqual((self.listed()["status"], self.listed()["next_try"]), ("happened", None))

    def test_follow_up_is_checked_and_private(self):
        for bad in ({"status": "maybe"}, {"status": "not_yet"}, {"status": "not_yet", "reason": "bored"},
                    {"status": "not_yet", "reason": "other", "next_try": "next week"},
                    {"status": "not_yet", "reason": "other", "next_try": 20261120},
                    {"status": "not_yet", "reason": "other", "note": "x" * 501}):
            self.assertEqual(self.client.put(self.url + "/follow-up", json=bad).status_code, 400, bad)
        stranger = signed_in(self.app.test_client(), username="stranger")
        self.assertEqual(stranger.put(self.url + "/follow-up", json={"status": "happened"}).status_code, 404)


@patch.dict("os.environ", {"INSIGHT_VIEWERS": "ngostaff"})
class InsightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name})

    def tearDown(self):
        self.temp.cleanup()

    def person(self, name, reason, share=True):
        client = signed_in(self.app.test_client(), username=name)
        session_id = client.post("/api/sessions", json={"preparation": {**PREPARATION, "topic": f"Secret topic of {name}"}}).get_json()["id"]
        self.assertEqual(client.put(f"/api/sessions/{session_id}/chosen-plan", json={"plan": PLAN}).status_code, 200)
        self.assertEqual(client.put(f"/api/sessions/{session_id}/follow-up", json={
            "status": "not_yet", "reason": reason, "note": f"Private note of {name}"}).status_code, 200)
        if share:
            self.assertTrue(client.put("/api/auth/insights", json={"share": True}).get_json()["user"]["share_insights"])

    def test_counts_come_only_from_people_who_share_and_never_include_their_words(self):
        viewer = signed_in(self.app.test_client(), username="ngostaff")
        self.assertTrue(viewer.get("/api/auth/me").get_json()["user"]["insight_viewer"])
        for index in range(4):
            self.person(f"girl{index}", "felt_nervous")
        self.assertEqual(viewer.get("/api/insights").get_json(), {"enough": False, "minimum_people": 5})
        self.person("girl4", "felt_unsafe")
        self.person("private", "felt_nervous", share=False)
        reply = viewer.get("/api/insights")
        data = reply.get_json()
        self.assertTrue(data["enough"])
        self.assertEqual((data["people"], data["conversations"]), (5, 5))  # The person who didn't share isn't counted.
        self.assertEqual(data["status"], {"not_yet": 5})
        self.assertEqual(data["not_yet_reasons"], {"felt_nervous": 4, "felt_unsafe": "fewer than 3"})  # Small numbers hidden.
        for private in ("Secret topic", "Private note", "girl", "private", "asha"):
            self.assertNotIn(private, reply.get_data(as_text=True))

    def test_only_named_ngo_staff_see_insights_and_sharing_is_opt_in(self):
        signed_in(self.app.test_client(), username="coordinator")  # The first account is the admin.
        participant = signed_in(self.app.test_client())
        me = participant.get("/api/auth/me").get_json()["user"]
        self.assertEqual((me["share_insights"], me["insight_viewer"]), (False, False))
        self.assertEqual(participant.get("/api/insights").status_code, 404)
        self.assertEqual(participant.put("/api/auth/insights", json={"share": "yes"}).status_code, 400)
        self.assertTrue(participant.put("/api/auth/insights", json={"share": True}).get_json()["user"]["share_insights"])
        self.assertFalse(participant.put("/api/auth/insights", json={"share": False}).get_json()["user"]["share_insights"])


if __name__ == "__main__":
    unittest.main()
