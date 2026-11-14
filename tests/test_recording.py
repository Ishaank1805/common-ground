"""Discarding a saved recording to record again. Run with: python3 -m unittest tests.test_recording"""
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

from app import create_app
from tests.support import signed_in
from tests.test_facilitator import PLAN, PREPARATION

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"


@unittest.skipUnless(Path(FFMPEG).is_file(), "ffmpeg unavailable")
class DiscardRecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name})
        self.client = signed_in(self.app.test_client())
        self.session_id = self.client.post("/api/sessions", json={"preparation": PREPARATION}).get_json()["id"]
        self.url = f"/api/sessions/{self.session_id}"
        self.assertEqual(self.client.put(self.url + "/chosen-plan", json={"plan": PLAN}).status_code, 200)
        self.audio = self.data / "take.webm"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.4", "-c:a", "libopus", str(self.audio)],
                       check=True, capture_output=True)

    def tearDown(self):
        self.temp.cleanup()

    def record(self, started):
        ended = started.replace(":00.000Z", ":09.000Z")
        return self.client.post(self.url + "/recording", data=self.audio.read_bytes(), content_type="audio/webm",
                                headers={"X-Recording-Started-At": started, "X-Recording-Ended-At": ended})

    def prompt_rows(self):
        with closing(sqlite3.connect(self.data / "sessions.sqlite3")) as conn:
            return conn.execute("SELECT COUNT(*) FROM facilitator_events").fetchone()[0]

    def test_discarding_removes_the_audio_and_its_prompts_and_allows_a_new_take(self):
        first = self.record("2026-11-13T10:00:00.000Z")
        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(len(list((self.data / "recordings").iterdir())), 1)
        with closing(sqlite3.connect(self.data / "sessions.sqlite3")) as conn, conn:
            conn.execute("INSERT INTO facilitator_events (session_id, capture_id, created_at, body) VALUES (?,?,?,?)",
                         (self.session_id, "2026-11-13T10:00:00.000Z", "now", "{}"))
        stranger = signed_in(self.app.test_client(), username="stranger")
        self.assertEqual(stranger.delete(self.url + "/recording").status_code, 404)  # Only the owner can discard.

        discarded = self.client.delete(self.url + "/recording")
        self.assertEqual(discarded.status_code, 200, discarded.get_json())
        self.assertIsNone(discarded.get_json()["recording"])
        self.assertEqual(list((self.data / "recordings").iterdir()), [])  # The audio file is gone...
        self.assertEqual(self.prompt_rows(), 0)  # ...and so are its prompts.
        self.assertEqual(self.client.get(self.url + "/recording").status_code, 404)
        self.assertEqual(self.client.delete(self.url + "/recording").status_code, 404)  # Nothing left to discard.

        second = self.record("2026-11-13T11:00:00.000Z")
        self.assertEqual(second.status_code, 200, second.get_json())
        self.assertIsNotNone(self.client.get(self.url).get_json()["recording"])
        self.assertEqual(self.client.get(self.url + "/recording").status_code, 200)


if __name__ == "__main__":
    unittest.main()
