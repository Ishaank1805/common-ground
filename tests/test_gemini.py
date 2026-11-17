import base64
import copy
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import gemini


INPUT = {
    "session_id": "s001",
    "user": {"name": "Ananth", "pronouns": "he/him", "gender": None},
    "preparation": {
        "audience": ["parents"], "topic": "Taking a course",
        "goals": [{"id": "g1", "description": "Explain my reasons"}],
        "expectations": {"what_might_go_well": [], "possible_barriers": ["Time concerns"]},
    },
    "clarification_answers": [],
}
QUESTION = {"id": "q1", "field": "preparation.topic", "question": "How much time would it need?"}
CLARIFY = {"session_id": "s001", "status": "clarification_required", "questions": [QUESTION], "plan": None}
READY = {
    "session_id": "s001", "status": "plan_ready", "questions": [],
    "plan": {"understanding": "Ananth wants to explain why the course matters.", "steps": [
        {"id": "step1", "goal_ids": ["g1"], "purpose": "Open and discuss the course",
         "talking_points": ["Explain the reasons; invite concerns and agree on a next step."],
         "example_wording": "Could we discuss why this course matters to me?"}], "assumptions": []},
}


def api_response(content, finish="STOP"):
    return io.BytesIO(json.dumps({"candidates": [{"finishReason": finish, "content": {
        "parts": [{"thought": True, "text": "ignored reasoning"}, {"text": json.dumps(content)}]}}]}).encode())


@patch.dict("os.environ", {"GEMINI_API_KEY": "test-secret"})
class GeminiTests(unittest.TestCase):
    def test_prepare_normalizes_rows_without_mutating_ids(self):
        original = copy.deepcopy(INPUT["preparation"])
        original["audience"] = " mother, father, "
        original["expectations"]["what_might_go_well"] = ["  ", " They will listen "]
        normalized = gemini.validate_preparation(original)
        self.assertEqual(normalized["audience"], ["mother", "father"])
        self.assertEqual(normalized["expectations"]["what_might_go_well"], ["They will listen"])
        self.assertEqual(normalized["goals"][0]["id"], "g1")
        self.assertIsInstance(original["audience"], str)

    def test_invalid_preparation_rejected(self):
        for field, value in (("goals", []), ("audience", [None]), ("topic", " " * 10),
                             ("topic", "x" * 2001), ("expectations", [])):
            with self.subTest(field=field, value=value):
                data = copy.deepcopy(INPUT["preparation"])
                data[field] = value
                with self.assertRaises(ValueError):
                    gemini.validate_preparation(data)

    def test_both_response_states(self):
        self.assertEqual(gemini.validate_response(CLARIFY, INPUT), CLARIFY)
        self.assertEqual(gemini.validate_response(READY, INPUT), READY)

    def test_invalid_states_and_references(self):
        examples = []
        wrong = copy.deepcopy(READY)
        wrong["session_id"] = "another"
        examples.append(wrong)
        wrong = copy.deepcopy(READY)
        wrong["plan"]["steps"][0]["goal_ids"] = ["unknown"]
        examples.append(wrong)
        wrong = copy.deepcopy(READY)
        wrong["plan"]["steps"] *= 2
        examples.append(wrong)
        wrong = copy.deepcopy(CLARIFY)
        wrong["questions"] *= 4
        examples.append(wrong)
        wrong = copy.deepcopy(CLARIFY)
        wrong["questions"][0]["field"] = "user.name"
        examples.append(wrong)
        wrong = copy.deepcopy(CLARIFY)
        wrong["plan"] = READY["plan"]
        examples.append(wrong)
        for invalid in examples:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                gemini.validate_response(invalid, INPUT)
        extra_goal = copy.deepcopy(INPUT)
        extra_goal["preparation"]["goals"].append({"id": "g2", "description": "Hear their concerns"})
        with self.assertRaises(ValueError):
            gemini.validate_response(READY, extra_goal)

    @patch("gemini.urlopen")
    def test_plan_request_uses_header_and_json_schema(self, transport):
        transport.return_value = api_response(CLARIFY)
        self.assertEqual(gemini.generate_plan(INPUT, []), CLARIFY)
        request = transport.call_args.args[0]
        self.assertNotIn("test-secret", request.full_url)
        self.assertEqual(request.get_header("X-goog-api-key"), "test-secret")
        self.assertIn("gemini-3.5-flash-lite:generateContent", request.full_url)
        body = json.loads(request.data)
        self.assertEqual(body["generationConfig"]["responseFormat"]["text"]["mimeType"], "APPLICATION_JSON")
        self.assertIn("untrusted", body["systemInstruction"]["parts"][0]["text"])

    @patch("gemini.urlopen")
    def test_not_sure_requires_assumptions_and_forces_plan(self, transport):
        data = copy.deepcopy(INPUT)
        data["clarification_answers"] = [{"question_id": "q1", "answer": "Not sure"}]
        valid = copy.deepcopy(READY)
        valid["plan"]["assumptions"] = ["Time commitment is unknown; discuss it before committing."]
        transport.return_value = api_response(valid)
        self.assertEqual(gemini.generate_plan(data, [QUESTION]), valid)
        body = json.loads(transport.call_args.args[0].data)
        schema = body["generationConfig"]["responseFormat"]["text"]["schema"]
        self.assertEqual(schema["properties"]["status"]["enum"], ["plan_ready"])
        payload = json.loads(body["contents"][0]["parts"][0]["text"])
        self.assertEqual(payload["previous_questions"], [QUESTION])
        self.assertEqual(payload["clarification_answers"], data["clarification_answers"])
        for invalid in (CLARIFY, READY):
            with self.assertRaises(ValueError):
                gemini.validate_response(invalid, data)

    @patch("gemini.urlopen")
    def test_bad_answer_reference_never_calls_api(self, transport):
        data = copy.deepcopy(INPUT)
        data["clarification_answers"] = [{"question_id": "q9", "answer": "Not sure"}]
        with self.assertRaises(ValueError):
            gemini.generate_plan(data, [QUESTION])
        transport.assert_not_called()

    @patch("gemini.urlopen")
    def test_safe_transport_errors(self, transport):
        for error in (TimeoutError("test-secret"), URLError("test-secret"),
                      HTTPError(gemini.ENDPOINT, 429, "test-secret", None, None)):
            with self.subTest(error=error):
                transport.side_effect = error
                with self.assertRaises(gemini.GeminiError) as caught:
                    gemini.generate_plan(INPUT, [])
                self.assertNotIn("test-secret", str(caught.exception))

    @patch("gemini.urlopen")
    def test_fallback_model_on_rate_limit_or_unavailable(self, transport):
        transport.side_effect = [
            HTTPError(gemini.ENDPOINT, 429, "Rate limit", None, io.BytesIO(b'{"error":{"message":"quota"}}')),
            api_response(READY)
        ]
        self.assertEqual(gemini.generate_plan(INPUT, []), READY)
        self.assertEqual(transport.call_count, 2)
        second_request = transport.call_args_list[1].args[0]
        self.assertIn("gemini-3.1-flash-lite:generateContent", second_request.full_url)

    @patch("gemini.urlopen")
    def test_provider_rejection_explains_reason_without_credentials(self, transport):
        detail = 'Invalid JSON payload: unknown field responseFormat. key=test-secret https://example.test/?key=other-secret x-goog-api-key: another-secret Bearer token-secret AIzaExampleSecret'
        transport.side_effect = HTTPError(gemini.ENDPOINT, 400, "Bad Request", None,
            io.BytesIO(json.dumps({"error": {"message": detail}}).encode()))
        with self.assertRaises(gemini.GeminiError) as caught:
            gemini.generate_plan(INPUT, [])
        message = str(caught.exception)
        self.assertIn("Google API (400)", message)
        self.assertIn("unknown field responseFormat", message)
        for secret in ("test-secret", "other-secret", "another-secret", "token-secret", "AIzaExampleSecret"):
            self.assertNotIn(secret, message)

    @patch("gemini.urlopen")
    def test_non_json_provider_error_uses_safe_fallback(self, transport):
        for body in (b"test-secret", b'[]', b'{"error":{"message":42}}'):
            transport.side_effect = HTTPError(gemini.ENDPOINT, 400, "test-secret", None, io.BytesIO(body))
            with self.assertRaises(gemini.GeminiError) as caught:
                gemini.generate_plan(INPUT, [])
            self.assertNotIn("test-secret", str(caught.exception))
            self.assertNotIn("Google API (400)", str(caught.exception))

    @patch("gemini.urlopen")
    def test_blocked_truncated_malformed_and_invalid_are_errors(self, transport):
        for response in (
            io.BytesIO(b'{"promptFeedback":{"blockReason":"SAFETY"}}'),
            api_response(READY, "MAX_TOKENS"), io.BytesIO(b"not json"),
            io.BytesIO(b'{"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":"oops"}]}}]}'),
            api_response({}), io.BytesIO(b"[]"),
        ):
            with self.subTest(response=response):
                transport.return_value = response
                with self.assertRaises(gemini.GeminiError):
                    gemini.generate_plan(INPUT, [])

    @patch("gemini.urlopen")
    def test_summary_only_sends_explicit_text_fields(self, transport):
        session = copy.deepcopy(INPUT)
        session.update({"recording": {"path": "PRIVATE_RECORDING"}, "_recording_file": "PRIVATE_FILE",
                        "chosen_plan": READY["plan"], "evaluation": {
                            "goals": [{"goal_id": "g1", "achievement": "Explained my reasons",
                                       "assessment": "partly_achieved", "internal": "PRIVATE_INTERNAL"}],
                            "reflection": "We needed more time", "revision": 3}})
        transport.return_value = api_response({"summary": "You explained your reasons and need more time."})
        summary = gemini.summarize_evaluation(session)
        self.assertIn("explained", summary)
        body = json.loads(transport.call_args.args[0].data)
        sent = body["contents"][0]["parts"][0]["text"]
        self.assertNotIn("PRIVATE", sent)
        self.assertIn("partly_achieved", sent)
        self.assertIn("We needed more time", sent)

    @patch("gemini.urlopen")
    def test_audio_summary_attaches_selected_audio_and_written_evaluation(self, transport):
        session = copy.deepcopy(INPUT)
        session.update(chosen_plan=READY["plan"], evaluation={
            "goals": [{"goal_id": "g1", "achievement": "We discussed the course", "assessment": "partly_achieved"}],
            "reflection": "They were interested."}, _recording_file="PRIVATE_LOCAL_PATH")
        transport.return_value = api_response({"summary": "The conversation explored the course and the time it would require. You felt they were interested and rated your goal partly achieved."})
        audio = b"synthetic-test-audio"
        gemini.summarize_evaluation(session, audio)
        body = json.loads(transport.call_args.args[0].data)
        parts = body["contents"][0]["parts"]
        self.assertEqual(base64.b64decode(parts[1]["inlineData"]["data"]), audio)
        self.assertEqual(parts[1]["inlineData"]["mimeType"], "audio/m4a")
        self.assertTrue(json.loads(parts[0]["text"])["audio_attached"])
        self.assertIn("partly_achieved", parts[0]["text"])
        self.assertNotIn("PRIVATE_LOCAL_PATH", json.dumps(body))
        self.assertEqual(body["systemInstruction"]["parts"][0]["text"], gemini.SUMMARY_PROMPT)

    @patch("gemini.urlopen")
    def test_oversized_audio_is_rejected_before_network(self, transport):
        with patch("gemini.MAX_AUDIO_BYTES", 3):
            with self.assertRaisesRegex(gemini.GeminiError, "12 MiB"):
                gemini._generate("summary", {}, {}, audio=b"long")
        transport.assert_not_called()

    @patch("gemini.urlopen")
    def test_missing_key_does_not_attempt_network(self, transport):
        with patch.dict("os.environ", {"GEMINI_API_KEY": ""}):
            with self.assertRaisesRegex(gemini.GeminiError, "GEMINI_API_KEY"):
                gemini.generate_plan(INPUT, [])
        transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
