"""The presentation for Umeed for Women, shown inside the app. Run with: python3 -m unittest tests.test_deck"""
import tempfile
import unittest

from app import create_app


class DeckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = create_app({"TESTING": True, "DATA_DIR": self.temp.name}).test_client()

    def tearDown(self):
        self.temp.cleanup()

    def test_the_presentation_opens_from_the_app_signed_in_or_not(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertEqual(page.count("data-open-deck"), 2)  # The header, and the sign-in screen.
        for part in ('id="deck-dialog"', 'id="deck-viewer"', 'data-stage="slides"', 'id="slides-stage"', "/static/deck.mjs", "/static/deck/deck.css"):
            self.assertIn(part, page)
        self.assertEqual(self.client.get("/static/deck.mjs").status_code, 200)
        builder = self.client.get("/static/conversation-deck.mjs").get_data(as_text=True)
        self.assertIn("export function renderSlides", builder)
        self.assertNotIn("innerHTML", builder)  # Her words are inserted as text, never as markup.

    def test_all_twelve_slides_work_under_the_apps_security_policy(self):
        slides = self.client.get("/static/deck/slides.html").get_data(as_text=True)
        self.assertEqual(slides.count('<section id="'), 12)
        self.assertEqual(slides.count("<aside>"), 12)  # Speaker notes for every slide.
        for blocked in ("style=", "<script", "<style", "http://", "https://"):
            self.assertNotIn(blocked, slides)  # Inline styles, scripts and outside files would be blocked.
        styles = self.client.get("/static/deck/deck.css").get_data(as_text=True)
        self.assertNotIn("@import", styles)
        for line in styles.splitlines():  # The deck's rules never reach the rest of the app.
            if line.startswith("."):
                self.assertTrue(line.startswith((".deck-", ".auth-deck")), line)


if __name__ == "__main__":
    unittest.main()
