"""End-to-end contracts with only the external Gemini HTTP call mocked."""
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from app import create_app
from tests.support import signed_in


class WorkflowTest(unittest.TestCase):
    def test_clarification_plan_reflection_and_summary(self):
        prep = {
            "audience": ["mother", "father"],
            "topic": "Taking a weekend course",
            "goals": [{"id": "g1", "description": "Explain why the course matters"}],
            "expectations": {
                "what_might_go_well": ["They may be interested in the skills"],
                "possible_barriers": ["They may worry about time"],
            },
        }
        with tempfile.TemporaryDirectory() as folder:
            app = create_app({"TESTING": True, "DATA_DIR": folder})
            client = signed_in(app.test_client())
            created = client.post("/api/sessions", json={"preparation": prep})
            self.assertEqual(created.status_code, 201)
            sid = created.json["id"]
            url = f"/api/sessions/{sid}"
            clarification = {
                "session_id": sid, "status": "clarification_required",
                "questions": [{"id": "q1", "field": "preparation.topic", "question": "How much time would the course require?"}],
                "plan": None,
            }
            chosen = {
                "understanding": "Ananth wants to explain the value of a course.",
                "assumptions": ["The time commitment is not yet known."],
                "steps": [{"id": "s1", "goal_ids": ["g1"], "purpose": "Explain and agree a next step", "talking_points": ["Explain the skills you hope to learn.", "Offer to check the timetable together."], "example_wording": "Could I explain why this matters, then hear your concerns?"}],
            }
            ready = {"session_id": sid, "status": "plan_ready", "questions": [], "plan": chosen}
            # Only synthetic fixture text leaves the app boundary in this test.
            summary = {"summary": "Ananth partly explained his reasons and wants to revisit the timetable."}
            envelopes = []
            for value in (clarification, ready, summary):
                envelopes.append(io.BytesIO(json.dumps({"candidates": [{"content": {"parts": [{"text": json.dumps(value)}]}, "finishReason": "STOP"}]}).encode()))
            with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}), patch("gemini.urlopen", side_effect=envelopes) as transport:
                result = client.post(url + "/plan", json={"clarification_answers": []})
                self.assertEqual(result.status_code, 200, result.json)
                self.assertEqual(result.json["model_response"]["status"], "clarification_required")
                result = client.post(url + "/plan", json={"clarification_answers": [{"question_id": "q1", "answer": "Not sure"}]})
                self.assertEqual(result.status_code, 200, result.json)
                self.assertEqual(result.json["model_response"]["status"], "plan_ready")
                result = client.put(url + "/chosen-plan", json={"plan": chosen})
                self.assertEqual(result.status_code, 200, result.json)
                evaluation = {"goals": [{"goal_id": "g1", "achievement": "I explained some of my reasons.", "assessment": "partly_achieved", "explanation": "We needed more time.", "next_step": "Revisit the timetable."}], "reflection": "They were more interested than I expected.", "state": "completed"}
                self.assertEqual(client.put(url + "/evaluation", json=evaluation).status_code, 200)
                result = client.post(url + "/summary", json={})
                self.assertEqual(result.status_code, 200, result.json)
                self.assertFalse(result.json["summary"]["outdated"])
                self.assertEqual(transport.call_count, 3)
            evaluation["reflection"] = "We agreed to look at the timetable together."
            result = client.put(url + "/evaluation", json=evaluation)
            self.assertTrue(result.json["summary"]["outdated"])
            reopened = signed_in(create_app({"TESTING": True, "DATA_DIR": folder}).test_client()).get(url)
            self.assertEqual(reopened.json["evaluation"]["reflection"], evaluation["reflection"])
            self.assertEqual(reopened.json["chosen_plan"], chosen)
            self.assertEqual(client.delete(url).status_code, 204)
            self.assertEqual(client.get(url).status_code, 404)


if __name__ == "__main__":
    unittest.main()
