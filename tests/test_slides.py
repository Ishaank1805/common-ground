"""NGO slides written by Gemini and checked by the server. Run with: python3 -m unittest tests.test_slides"""
import tempfile
import unittest
from unittest.mock import patch

import gemini
from app import create_app
from tests.support import signed_in
from tests.test_facilitator import PLAN, PREPARATION

SESSION = {
    "user": {"name": "Asha", "pronouns": "she/her"}, "preparation": PREPARATION, "chosen_plan": PLAN,
    "recording": {"started_at": "2026-11-17T10:00:00Z", "duration_seconds": 570}, "follow_up": None,
    "summary": {"text": "She explained her reasons calmly."},
    "evaluation": {"state": "completed", "reflection": "I think the fees worried them most, more than safety.",
                   "goals": [{"goal_id": PREPARATION["goals"][0]["id"], "assessment": "partly_achieved",
                              "achievement": "They listened to my reasons", "explanation": "", "next_step": "Share the fee plan"}]},
}
EVENTS = [{"elapsed_seconds": 40, "requested": True, "prompt": {"kind": "respond_to_concern", "text": "Answer the fee worry.", "example_wording": "x"}},
          {"elapsed_seconds": 90, "requested": True, "prompt": None},
          {"elapsed_seconds": 200, "requested": False, "prompt": {"kind": "return_to_goal", "text": "Back to your point.", "example_wording": "y"}}]


def slide(layout, **extra):
    base = {"layout": layout, "eyebrow": "", "heading": "A heading", "subtitle": "", "items": [], "rows": [],
            "quote": "", "note": "", "notes": ""}
    base.update(extra)
    return base


def deck(*middle):
    return {"slides": [slide("cover", heading="Studying further", subtitle="A conversation with her parents"),
                       *middle, slide("closing", heading="Thank you")]}


GOOD = deck(slide("cards", items=[{"label": "Goal 1", "title": "", "text": "Explain why it matters"}]),
            slide("table", rows=[{"label": "Length", "value": "9:30"}, {"label": "Cues shown", "value": "2"}]),
            slide("quote", heading="In her words", quote="the fees worried them most"))


class SlideCheckTests(unittest.TestCase):
    def setUp(self):
        self.facts = gemini.slide_facts(SESSION, EVENTS)
        self.words = gemini._own_words(SESSION)

    def test_the_app_counts_the_facts_itself(self):
        facts = {fact["label"]: fact["value"] for fact in self.facts}
        self.assertEqual((facts["Status"], facts["Length"], facts["Times she asked for a cue"], facts["Cues shown"]),
                         ("Reflected", "9:30", "2", "2"))
        self.assertEqual(facts["Safety card (Take a break)"], "Not needed")
        self.assertIn("Partly achieved", facts.values())

    def test_good_slides_pass(self):
        self.assertEqual(len(gemini.validate_slides(GOOD, self.words, self.facts)["slides"]), 5)

    def test_an_invented_quote_is_refused(self):
        bad = deck(slide("quote", quote="my parents were proud of me"), slide("table", rows=[{"label": "Length", "value": "9:30"}]),
                   slide("cards", items=[{"label": "", "title": "", "text": "x"}]))
        with self.assertRaisesRegex(ValueError, "word for word"):
            gemini.validate_slides(bad, self.words, self.facts)

    def test_a_number_the_app_did_not_count_is_refused(self):
        bad = deck(slide("table", rows=[{"label": "Cues shown", "value": "7"}]), slide("quote", quote="the fees worried them most"),
                   slide("cards", items=[{"label": "", "title": "", "text": "x"}]))
        with self.assertRaisesRegex(ValueError, "not one the app counted"):
            gemini.validate_slides(bad, self.words, self.facts)

    def test_it_must_open_with_a_cover_and_end_with_a_closing_slide(self):
        bad = {"slides": GOOD["slides"][1:] + [slide("closing")]}
        with self.assertRaisesRegex(ValueError, "cover"):
            gemini.validate_slides(bad, self.words, self.facts)

    def test_one_retry_then_a_clear_error(self):
        bad = {"slides": [slide("cards")] * 5}
        with patch("gemini._generate", side_effect=[bad, GOOD]) as model:
            self.assertEqual(len(gemini.generate_slides(SESSION, EVENTS)["slides"]), 5)
        self.assertEqual(model.call_count, 2)
        self.assertIn("I think the fees worried them most", str(model.call_args.args[1]))  # Her own words go in as data.
        with patch("gemini._generate", side_effect=[bad, bad]), self.assertRaisesRegex(gemini.GeminiError, "didn't pass the checks"):
            gemini.generate_slides(SESSION, EVENTS)


class SlideEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = signed_in(create_app({"TESTING": True, "DATA_DIR": self.temp.name}).test_client())
        self.url = "/api/sessions/" + self.client.post("/api/sessions", json={"preparation": PREPARATION}).get_json()["id"]

    def tearDown(self):
        self.temp.cleanup()

    def test_slides_need_a_plan_then_are_kept_with_the_conversation(self):
        self.assertEqual(self.client.post(self.url + "/slides", json={}).status_code, 400)
        self.assertEqual(self.client.put(self.url + "/chosen-plan", json={"plan": PLAN}).status_code, 200)
        with patch("app.gemini.generate_slides", return_value={"slides": GOOD["slides"]}) as made:
            reply = self.client.post(self.url + "/slides", json={})
        self.assertEqual(reply.status_code, 200, reply.get_json())
        self.assertEqual(made.call_args.args[1], [])  # No recording yet, so no cue history.
        kept = self.client.get(self.url).get_json()["slides"]
        self.assertEqual((len(kept["slides"]), kept["source_revision"]), (5, 0))


if __name__ == "__main__":
    unittest.main()
