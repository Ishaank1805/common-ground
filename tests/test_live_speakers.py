"""Who is speaking, live: You (from her voice sample), Person 1, 2... Run with: python3 -m unittest tests.test_live_speakers

The fingerprint model and speech detection are replaced by fixed answers, so these run without PyTorch.
"""
import importlib.util
import unittest
from unittest.mock import patch

import voice


@unittest.skipUnless(importlib.util.find_spec("numpy"), "numpy unavailable")
class LiveSpeakerTests(unittest.TestCase):
    def unit(self, *values):
        import numpy as np
        vector = np.array(values, "float32")
        return vector / np.linalg.norm(vector)

    def run_voices(self, her, schedule, speech=((0.0, 2.0),)):
        """Feed one second of audio per entry; each fingerprint is taken from whoever speaks in that second."""
        import numpy as np
        speakers = voice.LiveSpeakers(her)
        vectors = iter(schedule[2:] + schedule[-1:] * 3)
        with patch("voice.speech_segments", return_value=list(speech)), \
                patch("voice._embed", side_effect=lambda chunks: np.stack([next(vectors)])):
            for _ in schedule:
                speakers._absorb(np.zeros(voice.RATE, "int16").tobytes())
        return speakers

    def test_her_voice_is_you_and_the_others_are_numbered_in_order(self):
        her, mother, father = self.unit(1, 0, 0), self.unit(0, 1, 0), self.unit(0, 0, 1)
        speakers = self.run_voices([0.95, 0.31, 0], [her, her, her, her, mother, mother, mother, father, father, father])
        progress = speakers.take_names()
        self.assertEqual(sorted(progress["names"].values()), ["Person 1", "Person 2", "You"])
        self.assertEqual((progress["voices"], progress["has_sample"]), (3, True))
        self.assertGreater(progress["speech_seconds"], 0)
        by_name = {name: voice_id for voice_id, name in progress["names"].items()}
        self.assertEqual(speakers.speaker_at(6.5, lookback=1.5), by_name["Person 1"])
        self.assertEqual(speakers.processed, 10.0)  # Everything streamed has been worked through.
        self.assertIsNone(speakers.take_names())  # Only sent again when something changes.

    def test_pauses_are_fine_but_silence_teaches_nothing(self):
        her = self.unit(1, 0, 0)
        talk = self.run_voices([1.0, 0, 0], [her] * 6, speech=((0.0, 0.6), (1.5, 2.2)))  # 1.3 s of speech in 3 s
        self.assertIn("You", talk.take_names()["names"].values())
        quiet = self.run_voices([1.0, 0, 0], [her] * 6, speech=((0.0, 0.5),))  # too little speech to fingerprint
        self.assertIsNone(quiet.take_names())

    def test_without_a_voice_sample_everyone_is_a_person(self):
        her, mother = self.unit(1, 0, 0), self.unit(0, 1, 0)
        progress = self.run_voices(None, [her, her, her, her, mother, mother, mother]).take_names()
        self.assertEqual(sorted(progress["names"].values()), ["Person 1", "Person 2"])
        self.assertFalse(progress["has_sample"])

    def test_a_voice_too_like_hers_is_never_called_you(self):
        close, similar = self.unit(0.8, 0.6, 0), self.unit(0.6, -0.3, 0.74)
        progress = self.run_voices([1.0, 0.0, 0.0], [close, close, close, close, similar, similar, similar]).take_names()
        self.assertNotIn("You", progress["names"].values())  # Two voices both resemble the sample: no guess.

    def test_it_runs_on_its_own_thread_as_audio_streams(self):
        import numpy as np
        her, mother = self.unit(1, 0, 0), self.unit(0, 1, 0)
        vectors = iter([her, her, mother, mother, mother, mother])
        with patch("voice.speech_segments", return_value=[(0.0, 2.0)]), patch("voice._load"), \
                patch("voice._embed", side_effect=lambda chunks: np.stack([next(vectors)])):
            speakers = voice.LiveSpeakers([1.0, 0.0, 0.0])
            speakers.start()
            for _ in range(7):
                speakers.add(np.zeros(voice.RATE, "int16").tobytes())
            speakers.close()
            speakers.thread.join(5)
        self.assertEqual(sorted(speakers.take_names()["names"].values()), ["Person 1", "You"])


if __name__ == "__main__":
    unittest.main()
