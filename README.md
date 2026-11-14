# dsi-monsoon-2026-05a
Supporting facilitators: How might we help facilitators recognize what is happening in a sensitive group conversation and decide when and how intervention may be useful?

## Repository Structure

- `app.py`: Backend Flask service for session management, conversation preparation, plan generation, evaluation, and optional quiet prompts during a recording.
- `gemini.py`: Gemini client integrating planning, clarification, tone analysis, live quiet prompts, and audio reflection summaries.
- `static/facilitator.mjs`: In-browser rolling audio window and lifecycle for the optional quiet prompts.
- `static/wake.mjs`: Keeps the screen on while recording.
- `auth.py`: Accounts, sign-in sessions, recovery codes and account deletion.
- `voice.py`: Optional local voice matching (her / family / unclear) from her voice sample.
- `scripts/evaluate_voice.py`: Measures voice-matching accuracy on real recorded speech.
  - `tests/test_voice.py`: Voice sample, prompt timeline and threshold tests.
- `static/account.mjs`: Sign-in, sign-up, recovery and account settings in the browser.
  - `tests/test_auth.py`: Account, password, session-timeout and privacy tests.
- `requirements.txt`: Python dependencies for the backend service.
- `tests/`: Test suite including backend endpoint tests and Gemini integration tests.
  - `tests/test_backend.py`: Unit tests for session lifecycle, validation, and API routes.
  - `tests/test_gemini.py`: Unit tests for Gemini prompt construction and response parsing.
  - `tests/test_facilitator.py`: Quiet-prompt endpoint, validation, storage, deletion and Gemini contract tests.
  - `tests/facilitator.mjs`: Browser-module checks for resampling, the rolling window, WAV encoding and the prompt lifecycle.
- `data/hackathon1/`: Curated conversation datasets, annotations, export utilities, and validation scripts.

## Running Tests

Run all checks (no API key needed; Gemini is mocked):
```bash
python3 -m unittest tests.test_backend tests.test_gemini tests.test_workflow tests.test_facilitator tests.test_auth tests.test_recording tests.test_voice tests.test_follow_up_and_insights
node tests/frontend.mjs && node tests/facilitator.mjs
```

## Voice matching (experimental, opt-in)

Prompts can tell her voice from her family's. Each participant chooses whether to try it in **Account** ("Try voice matching", off by default), since it has not yet been checked on consented recordings of real participants; a server can stop offering it with `VOICE_MATCHING=0`. Once she turns it on, she can record a short voice sample in **Account** by reading one sentence aloud. The server keeps only a voice fingerprint (a 192-number SpeechBrain ECAPA-TDNN embedding), never the recording; she can delete it at any time, and it is deleted with her account.

When a prompt is prepared, `voice.py` runs entirely on the server. Silero VAD finds the speech; each stretch of about two seconds gets a fingerprint; the stretches are grouped into voices; and a voice is labelled `her` only if it matches her sample clearly better (`voice.MARGIN`, 0.35) than every other voice in the conversation. A lone voice, or two voices that both resemble hers, come out `unclear`. Comparing voices within the room matters: a fixed threshold alone labelled similar voices wrongly 41-45% of the time, and in a live test a parent's threat attributed to her switched off the safety cue. With the contrast check, 8 real pairs (5 female-female) were labelled 100% correctly and two sets of deliberately similar synthetic voices produced no wrong labels, only `unclear`. Gemini is told to treat `unclear` as unknown, never to let a label lead to a harmful prompt, and that a threat always calls for the safety cue whoever the timeline says spoke. Each stored prompt keeps only the seconds of speech per label.

On a tap, the conversation is labelled once, and only its last three minutes (`voice.LABEL_SECONDS`), so a prompt stays quick however long the conversation runs; fingerprints are computed in parallel (about 1.3 s per minute of speech on a 16-core laptop). Once downloaded, the model loads from its own folder without contacting Hugging Face, whatever `HF_HOME` says.

Install with `pip install --index-url https://download.pytorch.org/whl/cpu torch==2.5.1 torchaudio==2.5.1 && pip install -r requirements-voice.txt` (the Docker image includes them and the model, about 1.8 GB in total). Without them, or with `VOICE_MATCHING=0`, voice matching is simply not offered. Turning it off in Account deletes her voice fingerprint. Measure accuracy with `.venv/bin/python scripts/evaluate_voice.py [folder]`, ideally on consented recordings of real participants: family members can sound more alike than strangers.

## How quiet prompts decide

The AI does not decide on its own whether to speak. For every check it first answers whether anything said is a threat, then judges the latest moment as exactly one situation, checked in a fixed order: heated, an unanswered concern, drifted off topic, wrapping up, stuck, or on track. It always writes the cue for that situation. The server then decides: a threat always shows the fixed safety message; otherwise the situation sets the cue kind (pause, respond to a concern, come back to the goal, close, next point). When she taps, she always gets a cue; a background check ("As we go") stays quiet while the conversation is on track and never repeats a kind of cue within two minutes. A goal to agree on something counts only once a proposal has been made and someone has responded to it.

## Instant prompts (experimental)

With Gemini 3.8 Live, a cue can arrive in about a second instead of several. It is chosen for each conversation on the Record step ("Instant prompts", off by default) and is offered only when the server has a Gemini key, the `google-genai` and `flask-sock` packages, and `LIVE_PROMPTS` is not `0`. While prompts are on, her phone streams the conversation's 16 kHz audio over a WebSocket to this server (`/api/sessions/<id>/live`), which relays it to a Gemini Live session that listens continuously (`live.py`). When she taps, or at each "As we go" check (every 15 seconds while instant), the server asks for a judgement, and Gemini answers by calling `assess_moment`, whose arguments are exactly the regular prompt contract; the same `validate_guidance` and cue policy apply, and a threat is reported at once without waiting for a check. Cues reach her phone as text: the model's spoken audio is discarded and the API key never leaves the server. Sessions use context window compression and resume automatically when Gemini ends a connection. If instant prompts stop, regular prompts continue. Privacy: the whole conversation is streamed to Google while instant prompts are on, so this needs the NGO's agreement and a paid Gemini plan before any pilot.

## Follow-up when a conversation hasn't happened yet

Planned conversations do not always happen. On the Record step, "Haven't had this conversation yet?" lets her say what got in the way (no right moment, nervous, family not available, didn't feel safe, changed her mind, something else), an optional date to try again and a private note. She then sees fixed advice for that reason, written in advance so the NGO can review it, with no AI involved; "didn't feel safe" includes the helplines. Her conversation list shows where each one stands (planned, not yet, happened, recorded, reflected), and when she comes back after the date she chose, or three days after planning, a quiet check-in asks whether it happened: "Yes" takes her to reflection, "Not yet" to the follow-up.

## Anonymous insights for the NGO

With her consent only (Account, "Share anonymous insights", off by default), the NGO can see counts that help it improve its support: where conversations stand, what got in the way, how goals went in participants' own assessment, and how quiet prompts were used, including how many conversations showed the safety cue. Never names, topics, words, plans, notes, reflections or recordings. Nothing is shown until at least five people share, and any count below three is shown only as "fewer than 3", so no one can be singled out. Only admins and the NGO staff they choose see the Insights button; for everyone else the endpoint returns "not found".

**Team access.** The first account created on an installation is its admin (on an existing installation, its oldest account). In **Account, Team access**, an admin gives or removes "can see anonymous insights" or "admin" for any username; there is always at least one admin. On a shared server, create the NGO's admin account first. `INSIGHT_VIEWERS` (comma-separated usernames) can still grant insights access from the server.

## Project documents

- `docs/evaluation-results.md`: what we tested and what we found.
- `docs/privacy-and-consent.md`: a plain-language privacy and consent summary for Umeed for Women, with the decisions we need from them.
- `docs/existing-solutions.md`: research on existing solutions and where Common Ground fits.

## Accounts and privacy

Each person creates her own account (username, password, name, pronouns and optional gender), and her conversations, recordings and prompts are visible only to her. The design follows the OWASP Authentication, Password Storage and Session Management cheat sheets and NIST SP 800-63B-4:

- **Passwords:** at least 15 characters (there is no second factor) and up to 256; any characters, no composition rules; normalised (NFKC) and screened against common, repeated, sequential and context-specific choices (her username, the app's name). The sign-up form suggests a short sentence.
- **Storage:** scrypt at OWASP's minimum cost (N=2^17, r=8, p=1), set explicitly because Werkzeug's default is lower. At most two hashes run at once.
- **Sign-in:** one generic error for every failure. After 5 failures in a row the account pauses for a minute, doubling up to an hour; sign-in, recovery and sign-up are also limited per network address.
- **Sessions:** a random token per sign-in, stored only as a hash on the server, in an HttpOnly, SameSite=Lax browser-session cookie (Secure, with the `__Host-` prefix, when `SECURE_COOKIES=1`). Sessions end after 30 minutes idle or 12 hours in total, and sign-out ends them on the server. There is no "keep me signed in", because these phones are often shared; recording counts as activity.
- **Recovery:** no email is needed. A recovery code is shown once at sign-up (stored as a hash). Resetting the password with it signs out every device and issues a new code. Sign-in pauses do not block recovery.
- **Sensitive changes** (password, new recovery code, deleting the account) need the current password. Changing the password signs out her other devices; deleting the account removes every conversation, recording and prompt.
- **Before accounts:** conversations created before accounts existed belong to the first account created on that installation.

Set `SECURE_COOKIES=1` whenever the app is served over HTTPS (`compose.https.yaml` does). The attempt limits are kept in memory, so run one server process (the Docker image does); behind a reverse proxy, make sure the client's address reaches the app.

## Run with Docker

Needs Docker with Compose v2. The image includes ffmpeg and serves the app with gunicorn as a non-root user; conversations and recordings live in the `common-ground-data` volume.

```bash
cp .env.example .env          # add your Gemini key to .env (never commit it)
docker compose up --build     # open http://localhost:5001
```

Without a key the app still runs, with AI planning, prompts and summaries switched off. `docker compose logs -f` shows requests; `docker compose down` stops the app and keeps your data; `docker compose down -v` also deletes every conversation and recording.

Phones need HTTPS for the microphone. Put a certificate the phone trusts in `.certs/cert.pem` and `.certs/key.pem` (for example from mkcert for this computer's LAN address), make them readable with `chmod 644 .certs/*.pem`, then run `docker compose -f compose.yaml -f compose.https.yaml up --build` and open `https://<this computer's LAN address>:5001` on the phone.

Run the Python tests inside the image (they mock Gemini): `docker run --rm -v "$PWD/tests:/app/tests:ro" common-ground python -m unittest tests.test_facilitator tests.test_backend tests.test_gemini tests.test_workflow tests.test_auth tests.test_recording tests.test_voice tests.test_follow_up_and_insights`

## Quiet prompts during the conversation

Organizers asked for no notifications or alerts during a session, and later feedback noted that participants may need guidance in early sessions. Quiet prompts answer both: optional support that the participant chooses and that waits to be looked at.

On the Record step the participant picks **No prompts** (the default for every conversation), **When I ask** (a cue only after tapping *Get a prompt*) or **As we go** (a cue refreshes about every 45 seconds, only when it might help), and can change or turn this off mid-conversation. Nothing beeps, vibrates, pops up, moves or is announced. For a participant's first three recorded conversations the app marks *As we go* as suggested, and *When I ask* after that, so support fades as confidence grows; it never turns prompts on by itself. If the conversation sounds threatening, the app shows fixed wording instead of model wording: a neutral “Take a break” cue that looks like any other, since family members may see the screen, with the helplines (112 and the Women Helpline on 181, India) behind a “More help” tap and in full in the prompt list afterwards. While recording, the screen is kept awake so prompts stay visible. Tapping *Get a prompt* shows her next planned point at once; the AI cue, in the prompt language she chooses (English, Hindi or Hinglish), replaces it when ready.

`static/facilitator.mjs` taps the microphone stream with Web Audio and keeps only the latest 60 seconds in memory (16 kHz mono); the normal MP4 recording is untouched. When a prompt is prepared, that window is posted as WAV to `POST /api/sessions/{id}/facilitator`, together with the whole conversation so far when the person asks (compacted on the server to 16 kHz mono AAC, at most the last 45 minutes, so the cue draws on everything said). The server validates the audio and calls `gemini.facilitate` with the plan, the goals and the model's running notes from earlier prompts in the same recording. Prompts are stored in a separate `facilitator_events` table, so preparing one never changes a session's version or makes saving the recording fail. `GET /api/sessions/{id}/facilitator` lists the prompts from the saved recording, shown on the Record step afterwards. Deleting a conversation deletes its prompts. The full contract is in `interface/plan.md`.
