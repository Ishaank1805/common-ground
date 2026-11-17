"""Optional voice matching: which parts of a recording are the person using the app.

Runs entirely on this server, so no audio leaves it for this step. Silero VAD finds the
speech; SpeechBrain's ECAPA-TDNN speaker embeddings (Apache 2.0) are compared, by cosine
similarity, with the embedding of the person's own voice sample. Each stretch of speech is
labelled "her", "family" or "unclear". Only the sample's embedding is stored, never its audio.

If the libraries (torch, speechbrain, silero-vad) are not installed, available() is False and
the rest of the app works exactly as before.
"""
from concurrent.futures import ThreadPoolExecutor
import collections
import logging
import os
import queue
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


LOG = logging.getLogger("voice")
LONE_HER = 0.60  # With no other voice to compare against yet, "You" needs a clearly strong match.
MERGE_SIMILARITY = 0.62  # Two groups this alike are the same person, split early on.
MAX_LIVE_VOICES = 6


class LiveSpeakers:
    """Who is speaking as a conversation streams in, for the live transcript on her screen. Nothing is saved.

    Every second, the last two seconds of audio (if mostly speech) get an ECAPA fingerprint and join the most similar
    group of voices, or start a new one. The group that matches her voice sample clearly better than every other
    group (MARGIN) is "You"; while it is the only group, it needs LONE_HER. Other groups heard at least twice are
    "Person 1", "Person 2", ... in the order they first spoke; anything else is unnamed. Names can change as more
    is heard (for example once a second voice appears), and the phone renames earlier lines to match.
    """
    STEP_SECONDS, SPAN_SECONDS, KEEP_SECONDS, MIN_SPEECH_SECONDS = 1.0, 3.0, 30.0, 1.0

    def __init__(self, her_embedding=None):
        import numpy as np
        self.her = None
        if her_embedding:
            her = np.asarray(her_embedding, dtype="float32")
            self.her = her / (np.linalg.norm(her) or 1.0)
        self.samples, self.origin, self.received = np.zeros(0, dtype="float32"), 0.0, 0.0
        self.next_step = self.SPAN_SECONDS
        self.voices, self.aliases, self.names = [], {}, {}
        self.turns = collections.deque(maxlen=900)  # (start, end, voice id), in seconds of streamed audio
        self.lock, self.inbox, self.thread, self.changed = threading.Lock(), queue.Queue(), None, False
        self.processed, self.speech_seconds, self.reported_speech, self.failed = 0.0, 0.0, 0.0, False
        self.has_sample = self.her is not None

    def start(self):
        self.thread = threading.Thread(target=self._work, name="live-speakers", daemon=True)
        self.thread.start()

    def add(self, pcm):
        self.inbox.put(pcm)

    def close(self):
        self.inbox.put(None)

    def _work(self):
        try:
            _load()  # Load the fingerprint model now, so the first seconds of speech are not held up.
        except Exception:
            pass
        while True:
            pcm = self.inbox.get()
            if pcm is None:
                return
            try:
                self._absorb(pcm)
            except Exception as error:  # Labels are a bonus: the transcript carries on without them.
                if not self.failed:
                    self.failed = True
                    LOG.warning("Live speaker labels stopped working: %s", error)
                with self.lock:
                    self.processed = self.received

    def _absorb(self, pcm):
        import numpy as np
        chunk = samples_from_pcm(pcm)
        self.samples = np.concatenate([self.samples, chunk])
        self.received += len(chunk) / RATE
        excess = len(self.samples) - int(self.KEEP_SECONDS * RATE)
        if excess > 0:
            self.samples, self.origin = self.samples[excess:], self.origin + excess / RATE
        while self.received >= self.next_step:
            end = self.next_step
            begin = end - self.SPAN_SECONDS
            self.next_step += self.STEP_SECONDS
            window = self.samples[max(0, int((begin - self.origin) * RATE)):int((end - self.origin) * RATE)]
            # Fingerprint only the speech in the last few seconds: pauses are normal in conversation.
            voiced = [window[int(start * RATE):int(stop * RATE)] for start, stop in speech_segments(window)]
            voiced = np.concatenate(voiced) if voiced else window[:0]
            if len(voiced) < self.MIN_SPEECH_SECONDS * RATE:
                continue
            voice_id = self._group(_embed([voiced])[0], begin)
            with self.lock:
                self.turns.append((begin, end, voice_id))
                self.speech_seconds += len(voiced) / RATE / (self.SPAN_SECONDS / self.STEP_SECONDS)
                self._name()
        with self.lock:
            self.processed = self.received

    @staticmethod
    def _unit(vector):
        import numpy as np
        return vector / (np.linalg.norm(vector) or 1.0)

    def _group(self, vector, moment):
        best, similarity = None, -1.0
        for voice in self.voices:
            score = float(self._unit(voice["sum"]) @ vector)
            if score > similarity:
                best, similarity = voice, score
        if best is None or (similarity < GROUP_SIMILARITY and len(self.voices) < MAX_LIVE_VOICES):
            best = {"id": f"v{len(self.voices) + len(self.aliases) + 1}", "sum": vector * 0, "count": 0, "first": moment}
            self.voices.append(best)
        best["sum"] = best["sum"] + vector
        best["count"] += 1
        return best["id"]

    def _resolve(self, voice_id):
        while voice_id in self.aliases:
            voice_id = self.aliases[voice_id]
        return voice_id

    def _name(self):
        merged = True
        while merged:  # Groups that turn out to be one person become one.
            merged = False
            for index, first in enumerate(self.voices):
                for second in self.voices[index + 1:]:
                    if float(self._unit(first["sum"]) @ self._unit(second["sum"])) >= MERGE_SIMILARITY:
                        first["sum"], first["count"] = first["sum"] + second["sum"], first["count"] + second["count"]
                        first["first"] = min(first["first"], second["first"])
                        self.aliases[second["id"]] = first["id"]
                        self.voices.remove(second)
                        merged = True
                        break
                if merged:
                    break
        heard = [voice for voice in self.voices if voice["count"] >= 2]
        you = None
        if self.her is not None and heard:
            ranked = sorted(((float(self._unit(voice["sum"]) @ self.her), voice["id"]) for voice in heard), reverse=True)
            top, rival = ranked[0], (ranked[1][0] if len(ranked) > 1 else None)
            if top[0] >= HER and ((rival is None and top[0] >= LONE_HER) or (rival is not None and top[0] - rival >= MARGIN)):
                you = top[1]
        names, number = {}, 0
        for voice in sorted(self.voices, key=lambda item: item["first"]):
            if voice["id"] == you:
                names[voice["id"]] = "You"
            elif voice["count"] >= 2:
                number += 1
                names[voice["id"]] = f"Person {number}"
        for alias in self.aliases:  # Earlier lines may carry a merged group's old id.
            if self._resolve(alias) in names:
                names[alias] = names[self._resolve(alias)]
        if names != self.names:
            self.names, self.changed = names, True

    def speaker_at(self, seconds, lookback=2.5):
        """The voice heard most in the lookback seconds before `seconds` of streamed audio, or None."""
        with self.lock:
            counts = collections.Counter(self._resolve(voice) for start, end, voice in self.turns
                                         if end > seconds - lookback and start < seconds)
        return counts.most_common(1)[0][0] if counts else None

    def take_names(self):
        """Names (voice id -> You / Person N) and progress, when they changed or every few seconds of speech."""
        with self.lock:
            if not self.changed and self.speech_seconds - self.reported_speech < 3:
                return None
            self.changed, self.reported_speech = False, self.speech_seconds
            return {"names": dict(self.names), "voices": len(set(self.names.values())),
                    "speech_seconds": round(self.speech_seconds), "has_sample": self.has_sample}
