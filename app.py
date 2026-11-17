"""Local, single-user conversation preparation app."""
import argparse
import io
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import wave
from collections import Counter
from datetime import datetime, timezone
from uuid import uuid4

from flask import Flask, g, jsonify, request, send_file
from werkzeug.exceptions import HTTPException
import auth
import gemini
import live
import voice
from urllib.parse import urlparse

# Quiet prompts: the browser sends at most the latest minute; the server allows two.
FACILITATOR_MODES = ("on_request", "auto")
FACILITATOR_MAX_UPLOAD = 5 * 1024 * 1024
FACILITATOR_MAX_SECONDS = 120
# When the person asks, the whole conversation so far is sent too, compacted to 16 kHz mono AAC.
# 45 minutes at 24 kbps (~8 MB) plus the latest-minute WAV fits Gemini's inline request limit.
CONVERSATION_MAX_UPLOAD = 200 * 1024 * 1024
CONVERSATION_MAX_SECONDS = 45 * 60
RECORDING_FORMATS = {"audio/webm": "matroska", "video/webm": "matroska", "audio/mp4": "mov", "video/mp4": "mov", "audio/ogg": "ogg", "audio/wav": "wav"}


class APIError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


def string(value, label, required=True, limit=10000):
    if not isinstance(value, str) or len(value) > limit:
        raise APIError(f"{label} must be text of at most {limit} characters.")
    value = value.strip()
    if required and not value:
        raise APIError(f"{label} is required.")
    return value


def items(value, label, maximum=30):
    if not isinstance(value, list) or len(value) > maximum:
        raise APIError(f"{label} must be a list with at most {maximum} entries.")
    return value


def preparation(value):
    try:
        return gemini.validate_preparation(value)
    except ValueError as error:
        raise APIError(str(error))


def capture_time(value):
    """A recording's ISO start time identifies the capture that prompts belong to."""
    raw = string(value, "Recording start", limit=64)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise APIError("The recording start time must include a timezone.")
    return raw


def probe_duration(ffprobe, path):
    """Seconds of playable audio in a converted file; ValueError when there is none."""
    probe = subprocess.run([ffprobe, "-v", "error", "-show_entries", "stream=codec_type:format=duration", "-of", "json", str(path)], check=True, capture_output=True, timeout=20)
    info = json.loads(probe.stdout)
    duration = float(info["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0 or not any(stream.get("codec_type") == "audio" for stream in info["streams"]):
        raise ValueError("No playable audio.")
    return duration


def wav_seconds(audio):
    """Check the browser's 16-bit mono PCM window and return its length in seconds."""
    if len(audio) > FACILITATOR_MAX_UPLOAD:
        raise APIError("Prompt audio must be at most two minutes.", 413)
    try:
        with wave.open(io.BytesIO(audio), "rb") as reader:
            channels, width, rate = reader.getnchannels(), reader.getsampwidth(), reader.getframerate()
            frames = reader.getnframes()
            samples = reader.readframes(frames)
    except Exception:  # Untrusted bytes: any parsing failure means the audio is unusable.
        raise APIError("The prompt audio could not be read.", 422)
    if channels != 1 or width != 2 or not 8000 <= rate <= 48000 or not frames or len(samples) != frames * width:
        raise APIError("The prompt audio could not be read.", 422)
    if frames / rate > FACILITATOR_MAX_SECONDS:
        raise APIError("Prompt audio must be at most two minutes.", 413)
    return frames / rate


FOLLOW_UP_REASONS = ("no_right_moment", "felt_nervous", "family_busy", "felt_unsafe", "changed_mind", "other")
INSIGHTS_MIN_PEOPLE = 5  # No insights at all until at least this many people share...
INSIGHTS_MIN_COUNT = 3  # ...and any smaller count is shown only as "fewer than 3", so no one can be singled out.


def conversation_status(stored):
    """Where a conversation stands, for her list and for anonymous counts."""
    follow = stored.get("follow_up") or {}
    if (stored.get("evaluation") or {}).get("state") == "completed":
        return "reflected"
    if stored.get("recording"):
        return "recorded"
    if follow.get("status") in ("happened", "not_yet"):
        return follow["status"]
    return "planned" if stored.get("chosen_plan") else "preparing"


def load_local_env(path=None):
    """Read KEY=VALUE lines from .env. Existing environment variables win, so a shell export is kept."""
    file = Path(path or Path(__file__).resolve().parent / ".env")
    if not file.is_file():
        return
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name and name not in os.environ:
            os.environ[name] = value


def create_app(test_config=None):
    # Tests pass TESTING and must not pick up a developer's key. Local and gunicorn runs do.
    if not (test_config or {}).get("TESTING"):
        load_local_env()
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config.from_mapping(DATA_DIR=os.environ.get("DATA_DIR", str(Path(__file__).parent / "data")),
                            MAX_CONTENT_LENGTH=256 * 1024 * 1024)
    if test_config:
        app.config.update(test_config)
    data = Path(app.config["DATA_DIR"])
    recordings = data / "recordings"
    recordings.mkdir(parents=True, exist_ok=True)
    database = data / "sessions.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)")
        # Prompts live beside sessions, so preparing one never changes a session's version
        # and can never make saving the recording fail with a conflict.
        conn.execute("CREATE TABLE IF NOT EXISTS facilitator_events (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, capture_id TEXT NOT NULL, created_at TEXT NOT NULL, body TEXT NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS facilitator_events_capture ON facilitator_events (session_id, capture_id, id)")

    def db():
        if "db" not in g:
            g.db = sqlite3.connect(database, timeout=10)
        return g.db

    @app.teardown_appcontext
    def close_db(_error):
        conn = g.pop("db", None)
        if conn:
            conn.close()

    def load(session_id):
        # Only the owner reaches a conversation; anyone else gets "not found", never "forbidden".
        row = db().execute("SELECT body, version FROM sessions WHERE id=? AND owner_id=?", (session_id, g.user["id"])).fetchone()
        if not row:
            raise APIError("Session not found.", 404)
        g.version = row[1]
        session = json.loads(row[0])
        session["user"] = g.user["profile"]  # Plans and summaries use her current name and pronouns.
        return session

    def save(session):
        now = datetime.now(timezone.utc).isoformat()
        with db():
            result = db().execute("UPDATE sessions SET body=?, version=version+1, updated_at=? WHERE id=? AND version=?",
                                  (json.dumps(session), now, session["id"], g.version))
            if result.rowcount != 1:
                raise APIError("This session changed. Reload it before trying again.", 409)
        g.version += 1

    def response(session):
        return jsonify({k: v for k, v in session.items() if not k.startswith("_")})

    def body():
        if not request.is_json:
            raise APIError("Send application/json.", 415)
        if request.content_length and request.content_length > 200000:
            raise APIError("Form data is too large.", 413)
        value = request.get_json()
        if not isinstance(value, dict):
            raise APIError("Request body must be an object.")
        return value

    def delete_user_data(conn, user_id):
        """Remove every conversation and prompt a person owns; returns recording files to delete after commit."""
        files = []
        for session_id, stored in conn.execute("SELECT id, body FROM sessions WHERE owner_id=?", (user_id,)).fetchall():
            conn.execute("DELETE FROM facilitator_events WHERE session_id=?", (session_id,))
            name = json.loads(stored).get("_recording_file")
            if name:
                files.append(recordings / name)
        conn.execute("DELETE FROM sessions WHERE owner_id=?", (user_id,))
        return files

    # Accounts: every API route below needs a signed-in person (g.user), except auth.PUBLIC_PATHS.
    auth.init_auth(app, database=database, db=db, body=body, error=APIError, delete_user_data=delete_user_data)

    def model_input(session):
        return {"session_id": session["id"], "user": session["user"],
                "preparation": session["preparation"], "clarification_answers": session["clarification_answers"]}

    def model_call(fn, *args):
        try:
            return fn(*args)
        except gemini.GeminiError as error:
            raise APIError(str(error), 502)
        except Exception:
            # Do not return provider errors: they can include request data or credentials.
            app.logger.warning("Gemini request failed (%s).", getattr(fn, "__name__", "model call"))
            raise APIError("The model could not complete this request. Your saved input is safe; please retry.", 502)

    def locked(session):
        evaluation = session["evaluation"]
        return bool(session["recording"] or evaluation["goals"] or evaluation["reflection"] or evaluation["state"] == "completed")

    @app.before_request
    def same_origin():
        if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
            origin = request.headers.get("Origin")
            if (origin and origin != request.host_url.rstrip("/")) or request.headers.get("Sec-Fetch-Site") == "cross-site":
                raise APIError("Cross-origin changes are not allowed.", 403)

    @app.after_request
    def headers(result):
        result.headers["X-Content-Type-Options"] = "nosniff"
        result.headers["Referrer-Policy"] = "same-origin"
        result.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"
        if request.path.startswith("/api/"):
            result.headers["Cache-Control"] = "no-store"
        return result

    @app.errorhandler(APIError)
    def api_error(error):
        return jsonify(error=error.message), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/config")
    def config():
        return jsonify(model=gemini.MODEL, fallback_model=gemini.FALLBACK_MODEL,
                       voice_matching=voice.enabled(), live_prompts=live.enabled(),
                       model_configured=bool(os.environ.get("GEMINI_API_KEY")))

    @app.get("/api/sessions")
    def list_sessions():
        rows = db().execute("SELECT id,body,updated_at FROM sessions WHERE owner_id=? ORDER BY updated_at DESC", (g.user["id"],)).fetchall()
        listed = []
        for session_id, stored, updated_at in rows:
            stored = json.loads(stored)
            # "recorded" lets the interface suggest prompts for a person's first few conversations.
            listed.append({"id": session_id, "topic": stored["preparation"]["topic"],
                           "recorded": bool(stored.get("recording")), "updated_at": updated_at,
                           "status": conversation_status(stored), "next_try": (stored.get("follow_up") or {}).get("next_try")})
        return jsonify(listed)

    @app.post("/api/sessions")
    def create_session():
        prep = preparation(body().get("preparation"))
        session = {"id": uuid4().hex, "user": g.user["profile"], "preparation": prep,
                   "clarification_answers": [], "questions": [], "model_response": None,
                   "chosen_plan": None, "recording": None,
                   "evaluation": {"goals": [], "reflection": "", "state": "draft", "revision": 0}, "summary": None, "follow_up": None}
        with db():
            db().execute("INSERT INTO sessions (id,body,updated_at,owner_id) VALUES (?,?,?,?)", (session["id"], json.dumps(session), datetime.now(timezone.utc).isoformat(), g.user["id"]))
        return response(session), 201

    @app.get("/api/sessions/<session_id>")
    def get_session(session_id):
        return response(load(session_id))

    @app.delete("/api/sessions/<session_id>")
    def delete_session(session_id):
        session = load(session_id)
        with db():
            result = db().execute("DELETE FROM sessions WHERE id=? AND version=?", (session_id, g.version))
            if result.rowcount != 1:
                raise APIError("Session changed. Reload before deleting.", 409)
            db().execute("DELETE FROM facilitator_events WHERE session_id=?", (session_id,))
        if session.get("_recording_file"):
            (recordings / session["_recording_file"]).unlink(missing_ok=True)
        return "", 204

    @app.put("/api/sessions/<session_id>/preparation")
    def update_preparation(session_id):
        session = load(session_id)
        prep = preparation(body().get("preparation"))
        if prep != session["preparation"]:
            if locked(session):
                raise APIError("Preparation is preserved after recording or evaluation. Start a new session to change it.", 409)
            session.update(preparation=prep, clarification_answers=[], questions=[], model_response=None, chosen_plan=None)
            save(session)
        return response(session)

    @app.post("/api/sessions/<session_id>/plan")
    def plan(session_id):
        session = load(session_id)
        if locked(session):
            raise APIError("The conversation plan is preserved after recording or evaluation.", 409)
        rows = items(body().get("clarification_answers", []), "Clarification answers", 30)
        answers = {a["question_id"]: a for a in session["clarification_answers"]}
        question_ids = {q["id"] for q in session["questions"]}
        for row in rows:
            if not isinstance(row, dict):
                raise APIError("Each clarification answer must be an object.")
            qid = string(row.get("question_id"), "Question ID", limit=128)
            if qid not in question_ids:
                raise APIError("Clarification answer references an unknown question.")
            answers[qid] = {"question_id": qid, "answer": string(row.get("answer"), "Clarification answer", limit=4000)}
        session["clarification_answers"] = list(answers.values())
        save(session)
        output = model_call(gemini.generate_plan, model_input(session), session["questions"])
        session["model_response"] = output
        session["chosen_plan"] = None
        if output["status"] == "clarification_required":
            # Preserve prior questions so answers remain interpretable on subsequent requests.
            questions = {q["id"]: q for q in session["questions"]}
            questions.update({q["id"]: q for q in output["questions"]})
            session["questions"] = list(questions.values())
        save(session)
        return response(session)

    @app.put("/api/sessions/<session_id>/chosen-plan")
    def chosen_plan(session_id):
        session = load(session_id)
        value = body().get("plan")
        if locked(session) and value != session["chosen_plan"]:
            raise APIError("The plan used for this conversation is preserved.", 409)
        try:
            gemini.validate_response({"session_id": session_id, "status": "plan_ready", "questions": [], "plan": value}, model_input(session))
        except (ValueError, TypeError, KeyError):
            raise APIError("The plan is invalid. Check its steps and goal references.")
        session["chosen_plan"] = value
        save(session)
        return response(session)

    @app.post("/api/sessions/<session_id>/recording")
    def recording(session_id):
        session = load(session_id)
        if not session["chosen_plan"]:
            raise APIError("Save a plan before recording.", 409)
        if session["recording"]:
            saved = session["recording"]
            if (request.headers.get("X-Recording-Started-At") == saved["started_at"]
                    and request.headers.get("X-Recording-Ended-At") == saved["ended_at"]):
                return response(session)
            raise APIError("This session already has a saved recording.", 409)
        demuxer = RECORDING_FORMATS.get(request.mimetype)
        if not demuxer:
            raise APIError("Unsupported recording format.", 415)
        dates = []
        for name in ("X-Recording-Started-At", "X-Recording-Ended-At"):
            raw = request.headers.get(name, "")
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError()
            except ValueError:
                raise APIError("Recording timestamps must include a timezone.")
            dates.append((raw, parsed))
        if dates[1][1] < dates[0][1]:
            raise APIError("Recording end must follow its start.")
        ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
        ffprobe = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
        if not Path(ffmpeg).is_file() or not Path(ffprobe).is_file():
            raise APIError("MP4 saving requires ffmpeg and ffprobe on the server.", 503)
        with tempfile.TemporaryDirectory(dir=data) as folder:
            source, output = Path(folder) / "capture", Path(folder) / "recording.mp4"
            size = 0
            with source.open("wb") as file:
                while chunk := request.stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > app.config["MAX_CONTENT_LENGTH"]:
                        raise APIError("Recording is too large.", 413)
                    file.write(chunk)
            if not size:
                raise APIError("The recording is empty.")
            try:
                subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe", "-f", demuxer, "-i", str(source), "-map", "0:a:0", "-vn", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(output)], check=True, capture_output=True, timeout=120)
                probe = subprocess.run([ffprobe, "-v", "error", "-show_entries", "stream=codec_type:format=duration", "-of", "json", str(output)], check=True, capture_output=True, timeout=20)
                info = json.loads(probe.stdout)
                duration = float(info["format"]["duration"])
                if not math.isfinite(duration) or duration <= 0 or not any(x.get("codec_type") == "audio" for x in info["streams"]):
                    raise ValueError()
            except (subprocess.SubprocessError, ValueError, KeyError):
                raise APIError("The captured audio could not be saved as playable MP4. Your browser can retry saving.", 422)
            filename = f"{uuid4().hex}.mp4"
            destination = recordings / filename
            os.replace(output, destination)
            session["_recording_file"] = filename
            session["recording"] = {"url": f"/api/sessions/{session_id}/recording", "started_at": dates[0][0], "ended_at": dates[1][0], "duration_seconds": duration}
            if session["summary"]:
                session["summary"]["outdated"] = True
            try:
                save(session)
            except Exception:
                destination.unlink(missing_ok=True)
                raise
        return response(session)

    @app.get("/api/sessions/<session_id>/recording")
    def get_recording(session_id):
        session = load(session_id)
        if not session.get("_recording_file"):
            raise APIError("Recording not found.", 404)
        return send_file(recordings / session["_recording_file"], mimetype="audio/mp4", conditional=True)

    @app.delete("/api/sessions/<session_id>/recording")
    def discard_recording(session_id):
        """Discard the saved recording so the conversation can be recorded again."""
        session = load(session_id)
        if not session["recording"]:
            raise APIError("There is no saved recording to discard.", 404)
        filename = session.pop("_recording_file", None)
        session["recording"] = None
        if session["summary"] and session["summary"].get("includes_audio"):
            session["summary"]["outdated"] = True  # It described audio that no longer exists.
        save(session)
        with db():
            # Prompts belonged to the discarded recording; the written reflection is kept.
            db().execute("DELETE FROM facilitator_events WHERE session_id=?", (session_id,))
        if filename:
            (recordings / filename).unlink(missing_ok=True)
        return response(session)

    @app.put("/api/sessions/<session_id>/follow-up")
    def follow_up(session_id):
        """Whether the planned conversation has happened yet and, if not, what got in the way."""
        session = load(session_id)
        data = body()
        status = data.get("status")
        if status == "happened":
            session["follow_up"] = {"status": "happened", "updated_at": datetime.now(timezone.utc).isoformat()}
        elif status == "not_yet":
            reason = data.get("reason")
            if reason not in FOLLOW_UP_REASONS:
                raise APIError("Choose what got in the way.")
            next_try = data.get("next_try") or None
            if next_try is not None:
                try:
                    next_try = datetime.fromisoformat(next_try).date().isoformat()
                except (TypeError, ValueError):
                    raise APIError("Choose a valid date to try again.")
            session["follow_up"] = {"status": "not_yet", "reason": reason, "next_try": next_try,
                                    "note": string(data.get("note", ""), "Note", False, 500),
                                    "updated_at": datetime.now(timezone.utc).isoformat()}
        else:
            raise APIError("Say whether the conversation has happened yet.")
        save(session)
        return response(session)

    @app.get("/api/insights")
    def insights():
        """Anonymous counts for the NGO, only from people who chose to share, never any of their words."""
        if not auth.can_see_insights(g.user):
            raise APIError("Not found.", 404)
        people = db().execute("SELECT COUNT(*) FROM users WHERE share_insights = 1").fetchone()[0]
        if people < INSIGHTS_MIN_PEOPLE:
            return jsonify(enough=False, minimum_people=INSIGHTS_MIN_PEOPLE)

        def counted(counter):
            return {key: value if value >= INSIGHTS_MIN_COUNT else f"fewer than {INSIGHTS_MIN_COUNT}"
                    for key, value in counter.items() if key}
        shared = db().execute("SELECT s.id, s.body FROM sessions s JOIN users u ON u.id = s.owner_id "
                              "WHERE u.share_insights = 1").fetchall()
        statuses, reasons, assessments = Counter(), Counter(), Counter()
        for _, stored in shared:
            stored = json.loads(stored)
            status = conversation_status(stored)
            statuses[status] += 1
            if status == "not_yet":
                reasons[stored["follow_up"].get("reason")] += 1
            evaluation = stored.get("evaluation") or {}
            if evaluation.get("state") == "completed":
                for row in evaluation.get("goals", []):
                    assessments[row.get("assessment")] += 1
        modes, kinds = Counter(), Counter()
        prompted, safety, seen_modes = set(), set(), set()
        ids = [session_id for session_id, _ in shared]
        if ids:
            marks = ",".join("?" * len(ids))
            for session_id, stored in db().execute(f"SELECT session_id, body FROM facilitator_events WHERE session_id IN ({marks})", ids):
                event = json.loads(stored)
                prompted.add(session_id)
                if event.get("mode") and (session_id, event["mode"]) not in seen_modes:
                    seen_modes.add((session_id, event["mode"]))
                    modes[event["mode"]] += 1
                kind = (event.get("prompt") or {}).get("kind")
                if kind:
                    kinds[kind] += 1
                if kind == "step_away":
                    safety.add(session_id)
        totals = counted(Counter({"conversations": len(shared), "with_prompts": len(prompted), "with_safety_cue": len(safety)}))
        return jsonify(enough=True, people=people, conversations=totals.get("conversations", 0),
                       status=counted(statuses), not_yet_reasons=counted(reasons), goal_assessments=counted(assessments),
                       prompts={"conversations_with_prompts": totals.get("with_prompts", 0),
                                "conversations_with_safety_cue": totals.get("with_safety_cue", 0),
                                "modes": counted(modes), "kinds": counted(kinds)})

    @app.put("/api/sessions/<session_id>/evaluation")
    def evaluation(session_id):
        session = load(session_id)
        value = body()
        goal_ids = {x["id"] for x in session["preparation"]["goals"]}
        rows, seen = [], set()
        for row in items(value.get("goals", []), "Evaluation goals"):
            if not isinstance(row, dict) or not isinstance(row.get("goal_id"), str) or row["goal_id"] not in goal_ids or row["goal_id"] in seen:
                raise APIError("Each evaluation must reference a different original goal.")
            seen.add(row["goal_id"])
            assessment = row.get("assessment", "")
            if assessment not in ("", "achieved", "partly_achieved", "not_achieved"):
                raise APIError("Invalid achievement assessment.")
            rows.append({"goal_id": row["goal_id"], "assessment": assessment,
                         **{key: string(row.get(key, ""), key, False) for key in ("achievement", "explanation", "next_step")}})
        state = value.get("state", "draft")
        if state not in ("draft", "completed"):
            raise APIError("Invalid evaluation state.")
        if state == "completed" and (seen != goal_ids or any(not r["achievement"] or not r["assessment"] for r in rows)):
            raise APIError("Complete an achievement account and assessment for every goal.")
        old = session["evaluation"]
        result = {"goals": rows, "reflection": string(value.get("reflection", ""), "Reflection", False), "state": state, "revision": old["revision"]}
        if result != old:
            result["revision"] += 1
            session["evaluation"] = result
            if session["summary"]:
                session["summary"]["outdated"] = True
            save(session)
        return response(session)

    @app.post("/api/sessions/<session_id>/summary")
    def summary(session_id):
        body()
        session = load(session_id)
        evaluation = session["evaluation"]
        if not evaluation["reflection"] and not any(any(row.get(k) for k in ("achievement", "explanation", "next_step")) for row in evaluation["goals"]):
            raise APIError("Save your written evaluation before requesting a summary.")
        audio = None
        if session["recording"]:
            filename = session.get("_recording_file")
            if not filename or Path(filename).name != filename:
                raise APIError("The saved recording could not be found. Your evaluation is safe.", 409)
            try:
                with (recordings / filename).open("rb") as recording_file:
                    audio = recording_file.read(gemini.MAX_AUDIO_BYTES + 1)
            except OSError:
                raise APIError("The saved recording could not be read. Please restore it before generating an audio summary.", 409)
            if not audio:
                raise APIError("The saved recording is empty. Your evaluation is safe.", 422)
            if len(audio) > gemini.MAX_AUDIO_BYTES:
                raise APIError("Audio summaries currently support recordings up to 12 MiB. Your full recording and evaluation are still saved.", 413)
        text = model_call(gemini.summarize_evaluation, session, audio)
        session["summary"] = {"ai_draft": text, "text": text, "source_revision": evaluation["revision"], "outdated": False, "includes_audio": audio is not None}
        save(session)
        return response(session)

    @app.put("/api/sessions/<session_id>/summary")
    def edit_summary(session_id):
        session = load(session_id)
        if not session["summary"]:
            raise APIError("Generate a summary before editing it.", 409)
        session["summary"]["text"] = string(body().get("text"), "Summary", False, 20000)
        save(session)
        return response(session)

    def prompt_events(session_id, capture):
        rows = db().execute("SELECT body FROM facilitator_events WHERE session_id=? AND capture_id=? ORDER BY id",
                            (session_id, capture)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def compact_conversation(upload):
        """The conversation so far as 16 kHz mono AAC, keeping at most the last CONVERSATION_MAX_SECONDS."""
        demuxer = RECORDING_FORMATS.get(upload.mimetype)
        if not demuxer:
            raise APIError("Unsupported conversation audio format.", 415)
        ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
        ffprobe = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
        if not Path(ffmpeg).is_file() or not Path(ffprobe).is_file():
            raise APIError("Prompts from the whole conversation need ffmpeg and ffprobe on the server.", 503)
        with tempfile.TemporaryDirectory(dir=data) as folder:
            source, output, latest = (Path(folder) / name for name in ("capture", "conversation.m4a", "latest.m4a"))
            upload.save(source)
            if not source.stat().st_size:
                raise APIError("The conversation audio is empty.")
            try:
                subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe", "-f", demuxer, "-i", str(source), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "24k", str(output)], check=True, capture_output=True, timeout=120)
                duration = probe_duration(ffprobe, output)
                if duration > CONVERSATION_MAX_SECONDS:
                    # Keep the most recent part; earlier prompts' notes still carry what came before.
                    subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-ss", f"{duration - CONVERSATION_MAX_SECONDS:.3f}", "-i", str(output), "-c", "copy", str(latest)], check=True, capture_output=True, timeout=60)
                    output, duration = latest, probe_duration(ffprobe, latest)
            except (subprocess.SubprocessError, ValueError, KeyError):
                raise APIError("The conversation so far could not be read. Your recording continues.", 422)
            return output.read_bytes(), duration

    def decode_pcm(audio, demuxer=None):
        """Audio bytes as raw 16 kHz mono 16-bit PCM, for voice matching."""
        ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
        if not Path(ffmpeg).is_file():
            raise APIError("Voice matching needs ffmpeg on the server.", 503)
        with tempfile.TemporaryDirectory(dir=data) as folder:
            source = Path(folder) / "audio"
            source.write_bytes(audio)
            command = [ffmpeg, "-nostdin", "-v", "error", *(["-protocol_whitelist", "file,pipe", "-f", demuxer] if demuxer else []),
                       "-i", str(source), "-vn", "-ac", "1", "-ar", str(voice.RATE), "-f", "s16le", "-c:a", "pcm_s16le", "pipe:1"]
            return subprocess.run(command, check=True, capture_output=True, timeout=120).stdout

    @app.get("/api/voice")
    def voice_status():
        return jsonify(available=voice.enabled(), opted_in=bool(g.user.get("voice_opt_in")),
                       enrolled=bool(g.user.get("voice_embedding")))

    @app.post("/api/voice")
    def voice_sample():
        """Keep a fingerprint of her voice, never the audio, so prompts can tell her from her family."""
        if not voice.enabled():
            raise APIError("Voice matching isn't available on this server.", 503)
        if not g.user.get("voice_opt_in"):
            raise APIError("Turn on voice matching in Account first.", 409)
        if not request.content_length or request.content_length > 10 * 1024 * 1024:
            raise APIError("Keep the voice sample under a minute.", 413)
        demuxer = RECORDING_FORMATS.get(request.mimetype)
        if not demuxer:
            raise APIError("Unsupported audio format.", 415)
        try:
            samples = voice.samples_from_pcm(decode_pcm(request.get_data(), demuxer))
        except (subprocess.SubprocessError, ValueError):
            raise APIError("The voice sample could not be read. Please try again.", 422)
        if len(samples) > 60 * voice.RATE:
            raise APIError("Keep the voice sample under a minute.", 413)
        try:
            embedding, speech_seconds = voice.enrol(samples)
        except ValueError as error:
            raise APIError(f"{error} Read the sentence aloud in a quiet place and try again.", 422)
        with db():
            db().execute("UPDATE users SET voice_embedding=?, voice_enrolled_at=? WHERE id=?",
                         (json.dumps(embedding), datetime.now(timezone.utc).isoformat(), g.user["id"]))
        return jsonify(enrolled=True, speech_seconds=speech_seconds)

    @app.put("/api/voice/opt-in")
    def voice_opt_in():
        """Her choice to try experimental voice matching. Turning it off deletes her voice fingerprint."""
        on = body().get("on")
        if not isinstance(on, bool):
            raise APIError("Choose whether to use voice matching.")
        if on and not voice.enabled():
            raise APIError("Voice matching isn't available on this server.", 503)
        with db():
            if on:
                db().execute("UPDATE users SET voice_opt_in = 1 WHERE id = ?", (g.user["id"],))
            else:
                db().execute("UPDATE users SET voice_opt_in = 0, voice_embedding = NULL, voice_enrolled_at = NULL WHERE id = ?",
                             (g.user["id"],))
        return jsonify(available=voice.enabled(), opted_in=on, enrolled=bool(on and g.user.get("voice_embedding")))

    @app.delete("/api/voice")
    def delete_voice_sample():
        with db():
            db().execute("UPDATE users SET voice_embedding=NULL, voice_enrolled_at=NULL WHERE id=?", (g.user["id"],))
        return jsonify(enrolled=False)

    @app.post("/api/sessions/<session_id>/facilitator")
    def facilitator(session_id):
        """Prepare at most one quiet prompt from the latest minute of a recording in progress."""
        if not request.content_length or request.content_length > FACILITATOR_MAX_UPLOAD + CONVERSATION_MAX_UPLOAD + 65536:
            raise APIError("Prompt audio is missing or too large.", 413)
        session = load(session_id)
        if not session["chosen_plan"]:
            raise APIError("Save a plan before using prompts.", 409)
        if session["recording"]:
            raise APIError("Prompts are available only while the conversation is being recorded.", 409)
        if request.mimetype != "multipart/form-data":
            raise APIError("Send prompt audio as multipart/form-data.", 415)
        try:
            context = json.loads(request.form.get("context", ""))
        except ValueError:
            context = None
        if not isinstance(context, dict):
            raise APIError("Prompt request details are missing or invalid.")
        mode, requested, elapsed = context.get("mode"), context.get("requested"), context.get("elapsed_seconds")
        if mode not in FACILITATOR_MODES:
            raise APIError("Choose when prompts should appear.")
        if not isinstance(requested, bool):
            raise APIError("Say whether this prompt was requested.")
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) or not math.isfinite(elapsed) or not 0 <= elapsed <= 86400:
            raise APIError("The elapsed recording time is invalid.")
        capture = capture_time(context.get("capture_id"))
        language = context.get("language", "en")
        if language not in gemini.PROMPT_LANGUAGES:
            raise APIError("Choose a supported prompt language.")
        upload = request.files.get("audio")
        if not upload or upload.mimetype not in ("audio/wav", "audio/x-wav", "audio/wave"):
            raise APIError("Attach the latest audio as WAV.", 415)
        audio = upload.read(FACILITATOR_MAX_UPLOAD + 1)
        seconds = wav_seconds(audio)
        # When the person asks, the model hears the whole conversation so far, not just the last minute.
        conversation, conversation_seconds = None, None
        if "conversation" in request.files:
            if not requested:
                raise APIError("The conversation so far is sent only when the person asks for a prompt.")
            conversation, conversation_seconds = compact_conversation(request.files["conversation"])
        # Who is speaking when, matched locally against her voice sample. A bonus: prompts never fail because of it.
        speakers = None
        if g.user.get("voice_opt_in") and g.user.get("voice_embedding") and voice.enabled():
            try:
                if conversation is not None:  # The latest minute is the end of the conversation: label it once.
                    speakers = {"conversation": voice.timeline(voice.samples_from_pcm(decode_pcm(conversation)), g.user["voice_embedding"],
                                                               last_seconds=voice.LABEL_SECONDS)}
                else:
                    speakers = {"latest": voice.timeline(voice.samples_from_wav(audio), g.user["voice_embedding"])}
                # A timeline that labels no one adds nothing, and even its presence made the model hesitate over the safety cue.
                speakers = {name: part for name, part in speakers.items()
                            if any(segment["speaker"] != "unclear" for segment in part["segments"])} or None
            except Exception:
                app.logger.warning("Voice matching failed; preparing the prompt without it.")
                speakers = None
        events = prompt_events(session_id, capture)
        previous = events[-1] if events else {}
        recent = [{"kind": event["prompt"]["kind"], "text": event["prompt"]["text"], "elapsed_seconds": event.get("elapsed_seconds")}
                  for event in events if event["prompt"]][-3:]
        trace = {} if context.get("inspect") is True else None  # The optional live view on her screen.
        guidance = model_call(gemini.facilitate, session, audio, {
            "elapsed_seconds": round(elapsed), "notes": previous.get("notes", ""),
            "raised_goal_ids": previous.get("raised_goal_ids", []), "recent_prompts": recent,
            "requested": requested, "audio_seconds": seconds, "conversation_seconds": conversation_seconds,
            "language": language, "speakers": speakers}, conversation, *([trace] if trace is not None else []))
        # Once a goal has come up it stays raised for the rest of this recording.
        raised = set(previous.get("raised_goal_ids", [])) | set(guidance["raised_goal_ids"])
        event = {"mode": mode, "requested": requested, "elapsed_seconds": round(elapsed),
                 "audio_seconds": round(seconds, 1),
                 "conversation_seconds": None if conversation_seconds is None else round(conversation_seconds, 1),
                 "audio_status": guidance["audio_status"],
                 "current_step_id": guidance["current_step_id"],
                 "raised_goal_ids": [goal["id"] for goal in session["preparation"]["goals"] if goal["id"] in raised],
                 "prompt": guidance["prompt"], "language": language, "notes": guidance["notes"],
                 "goal_evidence": guidance.get("goal_evidence", {}),
                 "talk_seconds": {name: part["seconds"] for name, part in speakers.items()} if speakers else None}
        with db():
            db().execute("INSERT INTO facilitator_events (session_id, capture_id, created_at, body) VALUES (?,?,?,?)",
                         (session_id, capture, datetime.now(timezone.utc).isoformat(), json.dumps(event)))
        # The model's running notes are context for the next prompt, not something to read mid-conversation.
        reply = {key: value for key, value in event.items() if key not in ("notes", "goal_evidence", "talk_seconds")}
        if trace is not None:
            reply["inspect"] = trace  # Exactly what went to Gemini and what came back; nothing here is saved.
        return jsonify(reply)

    sock = live.socket_for(app)
    if sock is not None:
        @sock.route("/api/sessions/<session_id>/live")
        def live_prompts(ws, session_id):
            """Instant prompts: relay her 16 kHz audio to Gemini Live and send back text cues (see live.py)."""
            def refuse(reason):
                ws.close(reason=1008, message=reason)
            if not live.enabled():
                return refuse("Instant prompts aren't available on this server.")
            origin = request.headers.get("Origin", "")
            if not origin or urlparse(origin).netloc != request.host:
                return refuse("Instant prompts accept connections only from this site.")
            try:
                session = load(session_id)
                capture = capture_time(request.args.get("capture_id"))
            except APIError:
                return refuse("This conversation can't use instant prompts.")
            language = request.args.get("language", "en")
            if not session["chosen_plan"] or session["recording"] or language not in gemini.PROMPT_LANGUAGES:
                return refuse("Instant prompts work only while recording a planned conversation.")
            step_ids = [step["id"] for step in session["chosen_plan"]["steps"]]
            goal_ids = [goal["id"] for goal in session["preparation"]["goals"]]

            def memory():
                events = prompt_events(session_id, capture)
                recent = [{"kind": event["prompt"]["kind"], "text": event["prompt"]["text"], "elapsed_seconds": event.get("elapsed_seconds")}
                          for event in events if event["prompt"]][-3:]
                return (events[-1] if events else {}), recent

            def on_check(requested, elapsed):
                previous, recent = memory()
                return live.check_text(requested, elapsed, previous.get("notes", ""), previous.get("raised_goal_ids", []), recent)

            def on_call(args, requested, elapsed, mode, unprompted=False):
                # The same contract, validation and cue policy as regular prompts.
                previous, recent = memory()
                guidance = gemini.validate_guidance(args, step_ids, goal_ids, requested=requested, recent_prompts=recent,
                                                    elapsed_seconds=elapsed)
                if unprompted and (guidance["prompt"] or {}).get("kind") != "step_away":
                    # Only a threat may interrupt unasked. Anything else is neither shown nor kept, so a cue she never
                    # saw can't count as recent and hold back the cue she should see at the next check.
                    return None
                raised = set(previous.get("raised_goal_ids", [])) | set(guidance["raised_goal_ids"])
                event = {"mode": mode, "requested": requested, "elapsed_seconds": round(elapsed), "audio_seconds": None,
                         "conversation_seconds": None, "audio_status": guidance["audio_status"],
                         "current_step_id": guidance["current_step_id"],
                         "raised_goal_ids": [goal_id for goal_id in goal_ids if goal_id in raised],
                         "prompt": guidance["prompt"], "language": language, "notes": guidance["notes"],
                         "goal_evidence": guidance.get("goal_evidence", {}), "talk_seconds": None, "live": True}
                with db():
                    db().execute("INSERT INTO facilitator_events (session_id, capture_id, created_at, body) VALUES (?,?,?,?)",
                                 (session_id, capture, datetime.now(timezone.utc).isoformat(), json.dumps(event)))
                return {key: value for key, value in event.items() if key not in ("notes", "goal_evidence", "talk_seconds")}

            live.serve(ws, live.Relay(session, language), on_check, on_call)

    @app.get("/api/sessions/<session_id>/facilitator")
    def facilitator_prompts(session_id):
        """Prompts prepared during the saved recording, for looking back afterwards."""
        session = load(session_id)
        capture = session["recording"]["started_at"] if session["recording"] else None
        events = prompt_events(session_id, capture) if capture else []
        return jsonify(prompts=[{"elapsed_seconds": event["elapsed_seconds"], "requested": event["requested"], **event["prompt"]}
                                for event in events if event["prompt"]])

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--cert", help="Trusted HTTPS certificate for phone microphone access")
    parser.add_argument("--key", help="HTTPS certificate private key")
    args = parser.parse_args()
    if bool(args.cert) != bool(args.key):
        parser.error("Provide --cert and --key together.")
    create_app().run(host=args.host, port=args.port, ssl_context=(args.cert, args.key) if args.cert else None)
