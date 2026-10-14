"""Local, single-user conversation preparation app."""
import argparse
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from uuid import uuid4

from flask import Flask, g, jsonify, request, send_file
from werkzeug.exceptions import HTTPException
import gemini

PROFILE = {"name": "Ananth", "pronouns": "he/him", "gender": None}


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


def create_app(test_config=None):
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
        row = db().execute("SELECT body, version FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            raise APIError("Session not found.", 404)
        g.version = row[1]
        return json.loads(row[0])

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
        return jsonify(profile=PROFILE, model=gemini.MODEL,
                       model_configured=bool(os.environ.get("GEMINI_API_KEY")))

    @app.get("/api/sessions")
    def list_sessions():
        rows = db().execute("SELECT id,body,updated_at FROM sessions ORDER BY updated_at DESC").fetchall()
        return jsonify([{"id": r[0], "topic": json.loads(r[1])["preparation"]["topic"], "updated_at": r[2]} for r in rows])

    @app.post("/api/sessions")
    def create_session():
        prep = preparation(body().get("preparation"))
        session = {"id": uuid4().hex, "user": PROFILE, "preparation": prep,
                   "clarification_answers": [], "questions": [], "model_response": None,
                   "chosen_plan": None, "recording": None,
                   "evaluation": {"goals": [], "reflection": "", "state": "draft", "revision": 0}, "summary": None}
        with db():
            db().execute("INSERT INTO sessions (id,body,updated_at) VALUES (?,?,?)", (session["id"], json.dumps(session), datetime.now(timezone.utc).isoformat()))
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
        formats = {"audio/webm": "matroska", "video/webm": "matroska", "audio/mp4": "mov", "video/mp4": "mov", "audio/ogg": "ogg", "audio/wav": "wav"}
        demuxer = formats.get(request.mimetype)
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
