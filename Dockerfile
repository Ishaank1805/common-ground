# Common Ground: the Flask app plus ffmpeg, which converts recordings and conversation audio.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt gunicorn==23.0.0

# Optional voice matching (voice.py): CPU-only PyTorch, SpeechBrain and Silero VAD.
COPY requirements-voice.txt .
ENV HF_HOME=/opt/models/hf VOICE_MODEL_DIR=/opt/models/spkrec-ecapa-voxceleb
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.5.1 torchaudio==2.5.1 \
 && pip install -r requirements-voice.txt

COPY app.py gemini.py auth.py voice.py live.py ./
COPY static ./static
COPY docker/healthcheck.py ./docker/

# Run unprivileged; the database and recordings live in the /data volume.
RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown app:app /data
# Bake the speaker model into the image so voice matching works offline.
RUN python -c "import voice; voice._load()" && chown -R app:app /opt/models
USER app
VOLUME /data
EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD ["python", "docker/healthcheck.py"]

# One worker keeps SQLite simple; threads let recording uploads, prompts and page loads run together.
# Each instant-prompt (Gemini Live) connection holds a thread while it is open.
# The long timeout covers prompts that listen to the whole conversation (up to 120 s at Gemini).
# HTTPS: pass "--certfile ... --keyfile ..." through GUNICORN_CMD_ARGS (see compose.https.yaml).
CMD ["gunicorn", "--bind", "0.0.0.0:5001", "--workers", "1", "--threads", "32", "--timeout", "180", "--access-logfile", "-", "app:create_app()"]
