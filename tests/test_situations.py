"""The live-prompt design: the model judges the situation, the server chooses the cue.

Run with: python3 -m unittest tests.test_situations
"""
import unittest

import gemini

STEPS, GOALS = ["step1", "step2"], ["g1", "g2"]
CUE = {"text": "Acknowledge the fees and share your budget.", "example_wording": "Could we look at the costs together?", "step_id": "step2"}


def reply(situation, threat=False, text=CUE["text"]):
    return {"threat": threat, "audio_status": "clear", "notes": "Fees came up.", "current_step_id": "step2",
            "raised_goals": [{"goal_id": "g1", "evidence": "Why the course matters was explained."}],
            "situation": situation, "situation_evidence": "A worry about fees was left unanswered.", "cue": {**CUE, "text": text}}


def kind(result):
    return result["prompt"] and result["prompt"]["kind"]


class SituationPolicyTests(unittest.TestCase):
    def test_a_tap_always_gets_the_cue_for_its_situation(self):
        for situation, expected in gemini.SITUATION_KINDS.items():
            with self.subTest(situation=situation):
                self.assertEqual(kind(gemini.validate_guidance(reply(situation), STEPS, GOALS, requested=True)), expected)

    def test_background_checks_speak_up_unless_the_conversation_is_on_track(self):
        for situation, expected in gemini.SITUATION_KINDS.items():
            with self.subTest(situation=situation):
                result = gemini.validate_guidance(reply(situation), STEPS, GOALS, requested=False)
                self.assertEqual(kind(result), None if situation == "on_track" else expected)
                self.assertEqual(result["situation"], situation)

    def test_a_threat_always_shows_the_fixed_safety_message(self):
        for situation in gemini.SITUATIONS:
            for requested in (True, False):
                result = gemini.validate_guidance(reply(situation, threat=True, text="Tell them they are abusive."), STEPS, GOALS,
                                                  requested=requested, recent_prompts=[{"kind": "step_away", "text": "", "elapsed_seconds": 10}],
                                                  elapsed_seconds=20)
                self.assertEqual(result["prompt"], {"kind": "step_away", "text": gemini.STEP_AWAY_TEXT,
                                                    "example_wording": gemini.STEP_AWAY_WORDING, "step_id": None,
                                                    "help": gemini.STEP_AWAY_HELP})

    def test_background_checks_never_repeat_a_kind_within_two_minutes(self):
        recent = [{"kind": "respond_to_concern", "text": "Earlier cue.", "elapsed_seconds": 100}]
        concern = reply("unanswered_concern")
        self.assertIsNone(gemini.validate_guidance(concern, STEPS, GOALS, False, recent, 100 + gemini.REPEAT_SECONDS - 1)["prompt"])
        self.assertEqual(kind(gemini.validate_guidance(concern, STEPS, GOALS, False, recent, 100 + gemini.REPEAT_SECONDS)), "respond_to_concern")
        self.assertIsNone(gemini.validate_guidance(concern, STEPS, GOALS, False, [{"kind": "respond_to_concern", "text": "x"}], 50)["prompt"])
        self.assertEqual(kind(gemini.validate_guidance(reply("off_topic"), STEPS, GOALS, False, recent, 160)), "return_to_goal")
        self.assertEqual(kind(gemini.validate_guidance(concern, STEPS, GOALS, True, recent, 160)), "respond_to_concern")  # She asked.

    def test_the_cue_must_be_usable_when_it_is_shown(self):
        with self.assertRaises(ValueError):  # The words she could say next are required for any cue she sees.
            gemini.validate_guidance({**reply("stuck"), "cue": {**CUE, "example_wording": " "}}, STEPS, GOALS, requested=True)
        self.assertIsNone(gemini.validate_guidance(reply("on_track", text=""), STEPS, GOALS, requested=False)["prompt"])
        with self.assertRaises(ValueError):
            gemini.validate_guidance(reply("stuck", text=" "), STEPS, GOALS, requested=True)
        for change in ({"situation": "chaos"}, {"threat": "yes"}, {"cue": None}, {"cue": {**CUE, "step_id": "step9"}},
                       {"cue": {**CUE, "kind": "pause"}}, {"situation_evidence": None}, {"prompt": None}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                gemini.validate_guidance({**reply("stuck"), **change}, STEPS, GOALS)
        for field in ("threat", "situation", "situation_evidence", "cue"):
            incomplete = reply("stuck")
            incomplete.pop(field)
            with self.subTest(missing=field), self.assertRaises(ValueError):
                gemini.validate_guidance(incomplete, STEPS, GOALS)

    def test_a_short_pause_or_an_invitation_is_not_stuck(self):
        for rule in ("A short pause is not stuck", "more\n  than about five seconds of silence", "inviting her to say more, even if she"):
            self.assertIn(rule, gemini.FACILITATOR_PROMPT)

    def test_the_model_judges_in_a_fixed_order_with_strict_definitions(self):
        schema = gemini._guidance_schema(STEPS, GOALS)
        self.assertEqual(list(schema["properties"]), ["threat", "audio_status", "notes", "current_step_id", "raised_goals",
                                                      "situation", "situation_evidence", "cue"])
        self.assertEqual(schema["properties"]["situation"]["enum"], list(gemini.SITUATIONS))
        self.assertNotIn("kind", schema["properties"]["cue"]["properties"])  # The server, not the model, picks the kind.
        for rule in ("choose the first that applies", "always write the cue that fits your situation",
                     "It\n  is still unanswered when it is followed by silence", "someone else responded to it",
                     "requested only says whether", "never soften or skip a\njudgement",
                     "what should she say next to move toward her own goals", "example_wording is the heart of the cue"):
            self.assertIn(rule, gemini.FACILITATOR_PROMPT)


if __name__ == "__main__":
    unittest.main()
