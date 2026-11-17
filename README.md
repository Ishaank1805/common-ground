# Common Ground

Supporting facilitators in sensitive family conversations: helping women prepare their goals, navigate difficult interpersonal dynamics, stay safe, and build common ground.

---

## Quick Start & Running the Server

### Option A: Run with Docker (Recommended)

Requires Docker with Compose v2. The Docker image includes `ffmpeg`, Python 3.12, PyTorch CPU, SpeechBrain voice models, and serves the app with Gunicorn as a non-root user. Data persists in the `common-ground-data` Docker volume.

1. **Configure Environment**:
   ```bash
   cp .env.example .env
   # Open .env and add your GEMINI_API_KEY (never commit or stage .env)
   ```

2. **Build and Start Container in Background**:
   ```bash
   docker compose up --build -d
   ```

3. **Verify Server Health & Configuration**:
   ```bash
   # Check container status (should be "Up ... (healthy)")
   docker compose ps

   # Verify the running configuration (without exposing API keys)
   curl -s http://localhost:5001/api/config
   ```
   *Expected response*:
   ```json
   {"fallback_model":"gemini-3.1-flash-lite","live_prompts":true,"model":"gemini-3.5-flash-lite","model_configured":true,"voice_matching":true}
   ```

4. **Access the App**:
   Open [http://localhost:5001](http://localhost:5001) in your browser.

5. **View Logs or Stop**:
   ```bash
   docker compose logs -f --tail 40   # Stream live application logs
   docker compose down               # Stop container (preserves conversation data)
   docker compose down -v            # Stop container and wipe all database records
   ```

---

### Option B: Run Locally (Development Server)

For rapid local code development and debugging:

1. **Set Up Python Environment**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

2. **(Optional) Local Voice Matching Dependencies**:
   To enable local voice identification without Docker:
   ```bash
   pip install --index-url https://download.pytorch.org/whl/cpu torch==2.5.1 torchaudio==2.5.1
   pip install -r requirements-voice.txt
   ```

3. **Run Development Server**:
   ```bash
   set -a; source .env; set +a
   python app.py --port 5001
   ```
   Open [http://localhost:5001](http://localhost:5001).

---

### Option C: Mobile Testing with HTTPS (Microphone Access)

Mobile browsers require HTTPS to grant microphone permissions for recording and live voice prompts.

1. Generate trusted local development certificates (e.g. using `mkcert`):
   ```bash
   mkdir -p .certs
   mkcert -cert-file .certs/cert.pem -key-file .certs/key.pem localhost <YOUR_LAN_IP>
   chmod 644 .certs/*.pem
   ```

2. Start the HTTPS container:
   ```bash
   docker compose -f compose.yaml -f compose.https.yaml up --build -d
   ```

3. Open `https://<YOUR_LAN_IP>:5001` on your mobile device.

---

## Running the Complete Test Suite

All tests mock external AI calls by default and run without requiring an active API key or internet access:

### 1. Python Unittests (114 tests)
```bash
.venv/bin/python -W ignore -m unittest discover -s tests -p 'test_*.py'
```
*Expected output*: `Ran 114 tests ... OK (skipped=2)`

### 2. Browser & Frontend Verification (Node.js)
```bash
node tests/livestream.mjs      # 7 WebSocket and live streaming tests
node tests/facilitator.mjs     # Rolling audio window, resampling, and WAV encoding tests
node tests/frontend.mjs        # Recording lifecycle, retry, and interruption tests
```

### 3. Run Tests Inside Docker
```bash
docker run --rm -v "$PWD/tests:/app/tests:ro" common-ground python -m unittest discover -s tests -p 'test_*.py'
```

---

## Core Capabilities & Architecture

- **Cues That Answer "What Should She Say Next?"**:
  Rather than abstract observations, the prompt card provides the exact line she could say to move toward her goals, with a short situational explanation.

- **Instant Prompts via Gemini Live (WebSocket Streaming)**:
  `live.py` streams 16 kHz audio bidirectionally to Gemini Live over WebSockets (`/api/sessions/<id>/live`), enabling prompt delivery in ~1 second. Includes a live debug inspector (*"What goes to Gemini, and what comes back"*) displaying live transcripts and server checks.

- **Automated Fallback Model Resilience**:
  When primary models encounter rate limits or temporary service unavailabilities, requests automatically fail over seamlessly to `gemini-3.1-flash-lite`.

- **NGO Conversation Deck Generation**:
  `POST /api/sessions/<id>/slides` writes structured presentation slide decks for NGO facilitators directly from the recorded conversation and reflection.

- **Embedded Umeed Pitch Deck**:
  Integrated in-app slide deck viewer accessible from any screen (`data-open-deck`) explaining the organizational context, problem statement, and solution.

- **Voice Matching & Speaker Labels (Opt-In, Local)**:
  Enrolls a local voice fingerprint during account sign-up using SpeechBrain ECAPA-TDNN embeddings (`voice.py`). Generates live speaker labels (`You`, `Person 1`, `Person 2`) with contrastive margins to prevent false attributions.

- **Strict Safety First Architecture**:
  Any threatening language triggers a fixed, discrete "Take a break" safety cue and quick-access emergency helpline numbers (112, 181), overriding any AI suggestions.

---

## Repository Structure

```
├── app.py                      # Flask backend routes, sessions, audio endpoints, and slides API
├── gemini.py                   # Gemini client: planning, cues, slides generation, and fallback logic
├── live.py                     # WebSocket relay for Gemini Live instant prompts
├── voice.py                    # Local voice embedding, diarization, and speaker labelling
├── auth.py                     # Accounts, authentication, sessions, and recovery codes
├── requirements.txt            # Python dependencies (google-genai, flask, flask-sock, gunicorn)
├── requirements-voice.txt      # Voice model dependencies (speechbrain, torchaudio, silero-vad)
├── Dockerfile                  # Production container build with ffmpeg and non-root execution
├── compose.yaml                # Standard HTTP compose configuration (port 5001)
├── compose.https.yaml          # Optional TLS/HTTPS overlay for mobile microphone testing
├── static/
│   ├── app.js                  # Main UI controller, live monitor, and stage navigation
│   ├── index.html              # Single-page application markup with deck & slides views
│   ├── style.css               # Application stylesheet and responsive layout
│   ├── account.mjs             # User account management and voice sample enrollment
│   ├── livestream.mjs          # Client-side audio streamer for Gemini Live
│   ├── facilitator.mjs         # Rolling 60s audio buffer for standard quiet prompts
│   ├── conversation-deck.mjs   # Slide deck renderer for NGO conversation reports
│   └── deck/                   # Embedded Umeed presentation slides and styles
├── tests/                      # Automated unit, integration, and browser test suites
│   ├── test_backend.py         # Session lifecycle and API endpoint tests
│   ├── test_gemini.py          # Prompt engineering and sanitization tests
│   ├── test_live.py            # Gemini Live session contract tests
│   ├── test_live_app.py        # Live WebSocket application integration tests
│   ├── test_live_speakers.py   # Speaker diarization and live labelling tests
│   ├── test_situations.py      # Situational cue decision policy tests
│   ├── test_slides.py          # NGO slides generation and validation tests
│   ├── test_deck.py            # Umeed deck endpoint tests
│   ├── test_voice.py           # Local voice matching and margin threshold tests
│   ├── test_auth.py            # OWASP/NIST account security and password tests
│   ├── test_recording.py       # Audio recording upload and persistence tests
│   ├── livestream.mjs          # Client-side livestreaming unit tests
│   ├── facilitator.mjs         # Audio resampling and windowing tests
│   └── frontend.mjs           # Browser frontend integration tests
└── data/hackathon1/            # Conversation datasets, tone annotations, and benchmarks
```

---

## Detailed Feature Guidelines

### Voice Matching (Experimental, Opt-In)
Prompts can distinguish her voice from other participants. Participants choose whether to enable it in **Account** (*"Try voice matching"*, off by default), or administrators can disable it server-wide via `VOICE_MATCHING=0`.

When enabled, she records a short single-sentence sample. The server extracts and stores only a 192-dimensional SpeechBrain ECAPA-TDNN embedding, never the raw audio sample. During conversations, Silero VAD detects speech segments, fingerprints them, and attributes speech to `her` only when the cosine margin exceeds `voice.MARGIN` (0.35) over any other voice present. If ambiguity exists, the segment is labelled `unclear` to prevent false confidence.

### Quiet Prompts
On the Record step, participants select:
- **No prompts**: (Default) Records without prompts.
- **When I ask**: Delivers a cue only upon tapping *Get a prompt*.
- **As we go**: Automatically refreshes guidance every ~45 seconds when a relevant intervention point occurs.

Prompts never produce audio notifications, chimes, or vibrations. Tapping *Get a prompt* displays her next planned talking point instantly, replaced by the AI cue once generated.

### Anonymous Insights for NGOs
With explicit opt-in consent (Account -> *"Share anonymous insights"*), aggregate metrics are made accessible to authorized NGO staff (`/api/insights`):
- Aggregated counts of conversation progress stages (planned, completed, reflected).
- Common barriers encountered.
- Self-reported goal success rates.
- No names, topics, transcripts, recordings, or plans are ever included.
- Strict $k$-anonymity: metrics require at least 5 participating accounts and mask any count below 3 as `"fewer than 3"`.

### Security & Privacy Compliance
- **Authentication**: Passwords must be at least 15 characters, hashed using `scrypt` with OWASP-recommended parameters ($N=2^{17}, r=8, p=1$).
- **Lockout Protection**: Generic authentication errors with exponential pause timers after repeated failures.
- **Session Security**: Session tokens are cryptographically random, stored hashed on the server, and transmitted via `HttpOnly`, `SameSite=Lax` cookies (with `Secure` and `__Host-` prefixes under HTTPS).
- **Offline Recovery**: Deterministic recovery codes generated at account creation allow single-use credential resets without email requirements.
- **Full Data Ownership**: Deleting an account permanently purges all linked sessions, recordings, voice fingerprints, and reflection data.
