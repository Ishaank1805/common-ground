"""Measure how well voice matching tells one person from another, on real recorded speech.

Run with: .venv/bin/python scripts/evaluate_voice.py [folder of WAVs]
Without a folder it downloads SpeechBrain's own test sentences (Apache 2.0): six short
sentences each from two real speakers. Each speaker in turn "enrols" with three sentences;
the other sentences are scored against that sample, and a mock conversation alternating the
two voices is labelled second by second. Use your own recordings for a realistic check:
name them <speaker>_<anything>.wav (16 kHz mono), at least four sentences per speaker.
"""
from pathlib import Path
import sys
import time
import urllib.request
import wave

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np  # noqa: E402
import voice  # noqa: E402

SOURCE = "https://raw.githubusercontent.com/speechbrain/speechbrain/develop/tests/samples/ASR/{name}"
CACHE = Path.home() / ".cache" / "common-ground-voice"


def download():
    CACHE.mkdir(parents=True, exist_ok=True)
    for speaker in (1, 2):
        for take in range(1, 7):
            name = f"spk{speaker}_snt{take}.wav"
            if not (CACHE / name).exists():
                urllib.request.urlretrieve(SOURCE.format(name=name), CACHE / name)
    return CACHE


def load(path):
    with wave.open(str(path)) as reader:
        if reader.getframerate() != voice.RATE or reader.getnchannels() != 1:
            raise SystemExit(f"{path.name}: needs 16 kHz mono WAV")
        return np.frombuffer(reader.readframes(reader.getnframes()), "<i2").astype("float32") / 32768


def main():
    if not voice.available():
        raise SystemExit("Install the voice libraries first (see requirements-voice.txt).")
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else download()
    speakers = {}
    for path in sorted(folder.glob("*.wav")):
        speakers.setdefault(path.stem.split("_")[0], []).append(load(path))
    names = [name for name, takes in speakers.items() if len(takes) >= 4]
    if len(names) < 2:
        raise SystemExit("Need at least two speakers with four or more sentences each.")
    gap = np.zeros(int(0.5 * voice.RATE), "float32")
    started = time.time()
    print(f"Thresholds: her >= {voice.HER}, family <= {voice.FAMILY}, otherwise unclear.\n")
    for name in names:
        takes = speakers[name]
        others = [take for other in names if other != name for take in speakers[other]]
        embedding, speech = voice.enrol(np.concatenate([takes[0], gap, takes[1], gap, takes[2]]))
        reference = np.asarray(embedding, "float32")
        same = [float(voice._embed([take])[0] @ reference) for take in takes[3:]]
        different = [float(voice._embed([take])[0] @ reference) for take in others]
        print(f"{name}: sample has {speech}s of speech")
        print(f"  same person, whole sentences:  {np.round(same, 2)}")
        print(f"  other people, whole sentences: {np.round(different, 2)}")
        parts, truth = [], []
        for mine, theirs in zip(takes[3:], others):
            for clip, who in ((mine, "her"), (theirs, "family")):
                parts += [clip, gap]
                truth += [who] * len(clip) + [None] * len(gap)
        audio, truth = np.concatenate(parts), np.array(truth, dtype=object)
        result = voice.timeline(audio, embedding)
        counts = {"correct": 0, "wrong": 0, "unclear": 0}
        for segment in result["segments"]:
            for who in truth[int(segment["start"] * voice.RATE):int(segment["end"] * voice.RATE)]:
                if who is None:
                    continue
                counts["unclear" if segment["speaker"] == "unclear" else "correct" if segment["speaker"] == who else "wrong"] += 1
        total = sum(counts.values()) or 1
        print("  mock conversation, share of speech: " + ", ".join(f"{key} {value / total:.0%}" for key, value in counts.items()))
        print(f"  segments: {[(s['start'], s['end'], s['speaker']) for s in result['segments']]}\n")
    print(f"Done in {time.time() - started:.1f}s (the first run also loads the models).")


if __name__ == "__main__":
    main()
