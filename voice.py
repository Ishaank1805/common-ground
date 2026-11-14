"""Optional voice matching: which parts of a recording are the person using the app.

Runs entirely on this server, so no audio leaves it for this step. Silero VAD finds the
speech; SpeechBrain's ECAPA-TDNN speaker embeddings (Apache 2.0) are compared, by cosine
similarity, with the embedding of the person's own voice sample. Each stretch of speech is
labelled "her", "family" or "unclear". Only the sample's embedding is stored, never its audio.

If the libraries (torch, speechbrain, silero-vad) are not installed, available() is False and
the rest of the app works exactly as before.
"""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import threading
import wave

RATE = 16000
WINDOW_SECONDS = 2.0  # Long enough for a reliable embedding, short enough to follow turn-taking.
MIN_WINDOW_SECONDS = 0.8
MIN_SAMPLE_SPEECH_SECONDS = 4.0
# Cosine similarity with her sample: at least HER means her, at most FAMILY means someone else,
# anything between is "unclear". Checked on 16 real speakers, including same-gender pairs, with no
# wrong labels; override with environment variables.
HER = float(os.environ.get("VOICE_HER_THRESHOLD", "0.40"))
FAMILY = float(os.environ.get("VOICE_FAMILY_THRESHOLD", "0.20"))
# Labels compare voices within the conversation, not just against a fixed number: windows at least
# GROUP_SIMILARITY alike are grouped as one voice, and a voice is "her" only if it matches her sample
# MARGIN better than every other voice in the room. Similar voices therefore come out "unclear"
# instead of wrong. Chosen on 16 real speakers (8 pairs, 5 of them female-female) plus two sets of
# deliberately similar synthetic voices: 100% right on the real pairs, no wrong labels anywhere.
GROUP_SIMILARITY = 0.45
MARGIN = 0.35
LABEL_SECONDS = 180  # Only the latest three minutes of a long conversation are labelled, so a tap stays fast.
MAX_SEGMENTS = 300
WORKERS = max(1, min(4, (os.cpu_count() or 2) // 2))  # Windows are embedded in parallel, one core each.
MODEL_SOURCE = "speechbrain/spkrec-ecapa-voxceleb"
MODEL_DIR = Path(os.environ.get("VOICE_MODEL_DIR", str(Path(__file__).parent / "models" / "spkrec-ecapa-voxceleb")))

_lock = threading.Lock()
_models = None
_pool = None
_available = None


def _prepare_environment():
    """Keep model loading independent of the machine's Hugging Face settings."""
    home = os.environ.get("HF_HOME")
    if home and not os.access(home, os.W_OK):  # e.g. a cache on an unmounted drive
        os.environ["HF_HOME"] = str(MODEL_DIR.parent / "hf")
    if (MODEL_DIR / "hyperparams.yaml").exists():  # Already downloaded: never contact Hugging Face.
        os.environ["HF_HUB_OFFLINE"] = "1"


def enabled():
    """Voice matching can be offered: the libraries are installed and the server hasn't switched it off
    with VOICE_MATCHING=0. Each participant still chooses to try it in Account; it is off for everyone by default."""
    return os.environ.get("VOICE_MATCHING", "").strip().lower() not in ("0", "false", "no", "off") and available()


def available():
    """True when the voice-matching libraries are installed."""
    global _available
    if _available is None:
        _prepare_environment()
        try:
            import numpy  # noqa: F401
            import silero_vad  # noqa: F401
            import speechbrain  # noqa: F401
            import torch  # noqa: F401
            _available = True
        except ImportError:
            _available = False
    return _available


def _load():
    global _models, _pool
    with _lock:
        if _models is None:
            _prepare_environment()
            import torch
            from silero_vad import load_silero_vad
            from speechbrain.inference.speaker import EncoderClassifier
            # One thread per task with denormal floats flushed is about twice as fast on CPU;
            # the parallelism comes from embedding several windows at once (_pool).
            torch.set_num_threads(1)
            torch.set_flush_denormal(True)
            if (MODEL_DIR / "hyperparams.yaml").exists():
                encoder = EncoderClassifier.from_hparams(source=str(MODEL_DIR), savedir=str(MODEL_DIR), run_opts={"device": "cpu"})
            else:
                encoder = EncoderClassifier.from_hparams(source=MODEL_SOURCE, savedir=str(MODEL_DIR), run_opts={"device": "cpu"})
            _models = (load_silero_vad(), encoder)
            _pool = ThreadPoolExecutor(WORKERS, thread_name_prefix="voice")
    return _models


def samples_from_wav(data):
    """16 kHz mono 16-bit WAV bytes as float32 samples in [-1, 1]."""
    import io
    import numpy as np
    with wave.open(io.BytesIO(data), "rb") as reader:
        if reader.getnchannels() != 1 or reader.getsampwidth() != 2 or reader.getframerate() != RATE:
            raise ValueError("Voice matching needs 16 kHz mono 16-bit audio.")
        frames = reader.readframes(reader.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype("float32") / 32768.0


def samples_from_pcm(raw):
    """Raw 16 kHz mono signed 16-bit little-endian PCM as float32 samples in [-1, 1]."""
    import numpy as np
    return np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0


def speech_segments(samples):
    """(start, end) seconds of speech, from Silero VAD."""
    import torch
    from silero_vad import get_speech_timestamps
    vad, _ = _load()
    stamps = get_speech_timestamps(torch.from_numpy(samples), vad, sampling_rate=RATE,
                                   min_speech_duration_ms=300, min_silence_duration_ms=300)
    return [(stamp["start"] / RATE, stamp["end"] / RATE) for stamp in stamps]


def _embed(chunks):
    """Unit-length speaker embeddings, one per chunk of samples, computed in parallel."""
    import numpy as np
    import torch
    _, encoder = _load()

    def one(chunk):
        with torch.inference_mode():
            vector = encoder.encode_batch(torch.from_numpy(np.ascontiguousarray(chunk)).unsqueeze(0)).squeeze(1)
        return torch.nn.functional.normalize(vector, dim=1)[0].numpy()
    if not chunks:
        return np.zeros((0, 192), dtype="float32")
    return np.stack(list(_pool.map(one, chunks)))


def enrol(samples):
    """Embedding of a voice sample (list of floats) and its seconds of speech. ValueError if too little speech."""
    import numpy as np
    pieces = [samples[int(start * RATE):int(end * RATE)] for start, end in speech_segments(samples)]
    speech = np.concatenate(pieces) if pieces else np.zeros(0, dtype="float32")
    if len(speech) < MIN_SAMPLE_SPEECH_SECONDS * RATE:
        raise ValueError("The sample needs at least 4 seconds of clear speech.")
    return [round(float(value), 6) for value in _embed([speech])[0]], round(len(speech) / RATE, 1)


def label(score):
    if score is None:
        return "unclear"
    return "her" if score >= HER else "family" if score <= FAMILY else "unclear"


def _voices(embeddings):
    """Group windows into voices: centroid-linkage clustering on cosine similarity."""
    import numpy as np
    groups = [[index] for index in range(len(embeddings))]
    centres = [embeddings[index].copy() for index in range(len(embeddings))]
    while len(groups) > 1:
        unit = np.stack([centre / (np.linalg.norm(centre) or 1.0) for centre in centres])
        similarity = unit @ unit.T
        np.fill_diagonal(similarity, -1)
        first, second = np.unravel_index(np.argmax(similarity), similarity.shape)
        if similarity[first, second] < GROUP_SIMILARITY:
            break
        groups[first] += groups[second]
        centres[first] = embeddings[groups[first]].mean(0)
        del groups[second], centres[second]
    return groups


def contrast_labels(embeddings, scores):
    """her / family / unclear for each window, judged against the other voices in the conversation."""
    labels = ["unclear"] * len(scores)
    if not len(scores):
        return labels
    ranked = sorted(((float(scores[group].mean()), group) for group in _voices(embeddings)), key=lambda item: -item[0])
    for _, group in ranked[1:]:
        for index in group:
            labels[index] = "family" if scores[index] < HER else "unclear"
    top_score, top = ranked[0]
    rival = ranked[1][0] if len(ranked) > 1 else None
    for index in top:
        if top_score < HER:  # Nobody here sounds like her.
            labels[index] = "family" if scores[index] <= FAMILY else "unclear"
        elif rival is None or top_score - rival < MARGIN:  # A lone voice, or another voice sounds like hers too.
            labels[index] = "unclear"
        else:
            labels[index] = "her" if scores[index] > FAMILY else "unclear"
    return labels


def timeline(samples, sample_embedding, last_seconds=None):
    """Who is speaking when: merged segments of her / family / unclear, plus seconds of each.

    With last_seconds, only the end of the recording is labelled; times stay relative to its start.
    """
    import numpy as np
    offset = max(0.0, len(samples) / RATE - last_seconds) if last_seconds else 0.0
    samples = samples[int(offset * RATE):]
    reference = np.asarray(sample_embedding, dtype="float32")
    reference /= np.linalg.norm(reference) or 1.0
    windows = []
    for start, end in speech_segments(samples):
        moment = start
        while moment < end:
            stop = min(end, moment + WINDOW_SECONDS)
            if end - stop < MIN_WINDOW_SECONDS:  # Fold a short tail into this window.
                stop = end
            piece = samples[int(moment * RATE):int(stop * RATE)]
            windows.append([moment, stop, piece if stop - moment >= MIN_WINDOW_SECONDS else None])
            moment = stop
    chunks = [window[2] for window in windows if window[2] is not None]
    embeddings = _embed(chunks) if chunks else np.zeros((0, len(reference)), dtype="float32")
    labels = iter(contrast_labels(embeddings, embeddings @ reference))
    segments = []
    for start, end, piece in windows:
        speaker = "unclear" if piece is None else next(labels)
        start, end = start + offset, end + offset
        if segments and segments[-1]["speaker"] == speaker and start - segments[-1]["end"] < 1.0:
            segments[-1]["end"] = round(end, 1)
        else:
            segments.append({"start": round(start, 1), "end": round(end, 1), "speaker": speaker})
    seconds = {speaker: round(sum(s["end"] - s["start"] for s in segments if s["speaker"] == speaker), 1)
               for speaker in ("her", "family", "unclear")}
    if len(segments) > MAX_SEGMENTS:  # Keep the latest part.
        segments = segments[-MAX_SEGMENTS:]
    return {"segments": segments, "seconds": seconds}
