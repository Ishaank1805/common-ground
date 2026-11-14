"""Accounts and sign-in for Common Ground.

Follows the OWASP Authentication, Password Storage and Session Management cheat sheets
and NIST SP 800-63B-4:
- passwords of at least 15 characters (there is no second factor) and up to 256, any
  characters, no composition rules, NFKC-normalised, screened against common and
  context-specific choices;
- scrypt at OWASP's minimum cost (N=2^17, r=8, p=1);
- one generic sign-in error, a per-account lockout that doubles, and per-address limits;
- server-side sessions in HttpOnly, SameSite=Lax cookies (Secure with the __Host- prefix
  behind HTTPS), a fresh random token at every sign-in, stored only as a hash, with a
  30-minute idle and 12-hour absolute timeout and a sign-out that ends it on the server;
- the current password again before changing it, making a new recovery code or
  deleting the account;
- a recovery code shown once at sign-up instead of email, since participants may not
  have an email address.
"""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from uuid import uuid4

from flask import g, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

PASSWORD_MIN, PASSWORD_MAX = 15, 256
PASSWORD_HASH_METHOD = "scrypt:131072:8:1"  # OWASP's scrypt minimum: N=2^17, r=8, p=1 (128 MiB per hash).
USERNAME = re.compile(r"[a-z0-9][a-z0-9._-]{2,31}")
IDLE_SECONDS, ABSOLUTE_SECONDS = 30 * 60, 12 * 60 * 60
LOCK_AFTER, LOCK_BASE_SECONDS, LOCK_MAX_SECONDS = 5, 60, 60 * 60
def insight_viewer(username):
    """NGO staff who may see anonymous insights, named in INSIGHT_VIEWERS (comma-separated usernames)."""
    import os
    viewers = {name.strip().lower() for name in os.environ.get("INSIGHT_VIEWERS", "").split(",") if name.strip()}
    return username.lower() in viewers


def can_see_insights(user):
    """Admins, NGO staff an admin chose in the app, or usernames in the optional INSIGHT_VIEWERS setting."""
    return bool(user.get("is_admin")) or bool(user.get("is_insight_viewer")) or insight_viewer(user["username"])


PUBLIC_PATHS = {"/api/config", "/api/auth/login", "/api/auth/register", "/api/auth/recover", "/api/auth/logout"}
SIGN_IN_FAILED = "Username or password is incorrect. After several tries, sign-in pauses for a few minutes."
RECOVERY_FAILED = "That username and recovery code don’t match."
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # No 0/O or 1/I/L, which are easy to misread.
SEQUENCES = ("0123456789" * 4, "abcdefghijklmnopqrstuvwxyz", "qwertyuiopasdfghjklzxcvbnm",
             "1qaz2wsx3edc4rfv5tgb6yhn7ujm8ik9ol0p")
COMMON = {"password1234567", "password12345678", "password123456789", "123456789qwerty", "qwerty123456789",
          "iloveyou1234567", "welcome12345678", "changeme1234567", "letmein123456789"}


def _now():
    return time.time()


def password_problem(password, username=""):
    """Why a new password is too weak, or None. Length and guessability only; no composition rules."""
    if not isinstance(password, str) or not password:
        return "Enter a password."
    if len(password) < PASSWORD_MIN:
        return f"Use at least {PASSWORD_MIN} characters. A short sentence you’ll remember works well."
    if len(password) > PASSWORD_MAX:
        return f"Use at most {PASSWORD_MAX} characters."
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", password).casefold())
    guessable = (len(set(compact)) < 5 or re.fullmatch(r"(.{1,8})\1+", compact) or compact in COMMON
                 or any(compact in run or compact in run[::-1] for run in SEQUENCES)
                 or "commonground" in compact or (len(username) >= 3 and username in compact))
    if guessable:
        return "That password is easy to guess. Try a short sentence that means something only to you."
    return None


class Limiter:
    """At most `limit` attempts per key in a sliding window. In memory, so run one server process."""

    def __init__(self, limit, window):
        self.limit, self.window, self.hits, self.lock = limit, window, {}, threading.Lock()

    def allow(self, key):
        """Count an attempt; False once the limit is reached."""
        if self.full(key):
            return False
        self.record(key)
        return True

    def full(self, key):
        now = _now()
        with self.lock:
            self.hits[key] = [moment for moment in self.hits.get(key, []) if now - moment < self.window]
            return len(self.hits[key]) >= self.limit

    def record(self, key):
        now = _now()
        with self.lock:
            if len(self.hits) > 10000:
                self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < self.window}
            self.hits.setdefault(key, []).append(now)


def init_auth(app, *, database, db, body, error, delete_user_data):
    """Add accounts. Afterwards every /api/ route outside PUBLIC_PATHS needs a signed-in person (g.user)."""
    app.config.setdefault("PASSWORD_HASH_METHOD", PASSWORD_HASH_METHOD)
    app.config.setdefault("SECURE_COOKIES", os.environ.get("SECURE_COOKIES") == "1")
    secure = bool(app.config["SECURE_COOKIES"])
    cookie = "__Host-cg_session" if secure else "cg_session"
    hashing = threading.BoundedSemaphore(2)  # Each scrypt hash takes 128 MiB; cap how many run at once.
    dummies = {}
    attempts = Limiter(30, 15 * 60)  # Sign-in, recovery and password checks per network address.
    signups = Limiter(10, 60 * 60)

    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, recovery_hash TEXT NOT NULL, profile TEXT NOT NULL, created_at TEXT NOT NULL, failed_logins INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0)")
        conn.execute("CREATE TABLE IF NOT EXISTS auth_sessions (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at REAL NOT NULL, last_seen REAL NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS auth_sessions_user ON auth_sessions (user_id)")
        user_columns = {column[1] for column in conn.execute("PRAGMA table_info(users)")}
        for column in ("voice_embedding", "voice_enrolled_at"):  # Optional voice sample: a fingerprint, never audio.
            if column not in user_columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")
        if "share_insights" not in user_columns:  # Opt-in only: anonymous counts for the NGO.
            conn.execute("ALTER TABLE users ADD COLUMN share_insights INTEGER NOT NULL DEFAULT 0")
        for column in ("voice_opt_in", "is_admin", "is_insight_viewer"):  # Choices made in the app, not the server.
            if column not in user_columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
        if not conn.execute("SELECT 1 FROM users WHERE is_admin = 1").fetchone():  # An existing install's first account runs it.
            conn.execute("UPDATE users SET is_admin = 1 WHERE rowid = (SELECT MIN(rowid) FROM users)")
        if "owner_id" not in {column[1] for column in conn.execute("PRAGMA table_info(sessions)")}:
            conn.execute("ALTER TABLE sessions ADD COLUMN owner_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS sessions_owner ON sessions (owner_id, updated_at)")

    def hash_password(password):
        with hashing:
            return generate_password_hash(unicodedata.normalize("NFKC", password), method=app.config["PASSWORD_HASH_METHOD"])

    def password_matches(stored, password):
        with hashing:
            return check_password_hash(stored, unicodedata.normalize("NFKC", password))

    def dummy_hash():
        # Unknown usernames still pay for one hash, so response times don't reveal which accounts exist.
        method = app.config["PASSWORD_HASH_METHOD"]
        if method not in dummies:
            dummies[method] = hash_password(secrets.token_urlsafe(16))
        return dummies[method]

    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def code_digest(code):
        return digest(re.sub(r"[\s-]", "", str(code)).upper())

    def new_recovery_code():
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(20))  # About 99 bits.
        return "-".join(raw[index:index + 4] for index in range(0, 20, 4)), code_digest(raw)

    def user_by(column, value):
        keys = ("id", "username", "password_hash", "recovery_hash", "profile", "failed_logins", "locked_until", "voice_embedding", "share_insights", "voice_opt_in", "is_admin",
            "is_insight_viewer")
        row = db().execute(f"SELECT {', '.join(keys)} FROM users WHERE {column}=?", (value,)).fetchone()
        if not row:
            return None
        user = dict(zip(keys, row))
        user["profile"] = json.loads(user["profile"])
        user["voice_embedding"] = json.loads(user["voice_embedding"]) if user["voice_embedding"] else None
        return user

    def shown(user):
        return {"username": user["username"], **user["profile"], "voice_sample": bool(user.get("voice_embedding")),
                "share_insights": bool(user.get("share_insights")), "insight_viewer": can_see_insights(user),
                "admin": bool(user.get("is_admin")), "voice_matching": bool(user.get("voice_opt_in"))}

    def username_from(data):
        return str(data.get("username") or "").strip().lower()

    def profile_from(data):
        values = [data.get(key) if data.get(key) is not None else "" for key in ("name", "pronouns", "gender")]
        if not all(isinstance(value, str) for value in values):
            raise error("Name, pronouns and gender must be text.")
        name, pronouns, gender = (value.strip() for value in values)
        if not name or len(name) > 60:
            raise error("Enter your name, up to 60 characters.")
        if not pronouns or len(pronouns) > 30:
            raise error("Enter your pronouns, up to 30 characters, for example she/her.")
        if len(gender) > 30:
            raise error("Gender can be up to 30 characters.")
        return {"name": name, "pronouns": pronouns, "gender": gender or None}

    def limit(limiter, message):
        if not limiter.allow(request.remote_addr or "unknown"):
            raise error(message, 429)

    def start_session(result, user_id):
        # A new random token at every sign-in (no session fixation); only its hash is stored.
        token = secrets.token_urlsafe(32)
        now = _now()
        with db():
            db().execute("INSERT INTO auth_sessions (token_hash, user_id, created_at, last_seen) VALUES (?,?,?,?)",
                         (digest(token), user_id, now, now))
        # No expiry: a browser-session cookie. The server's idle and absolute timeouts end the session.
        result.set_cookie(cookie, token, httponly=True, secure=secure, samesite="Lax", path="/")
        return result

    def end_current_session():
        token = request.cookies.get(cookie)
        if token:
            with db():
                db().execute("DELETE FROM auth_sessions WHERE token_hash=?", (digest(token),))

    def signed_out(result):
        result.delete_cookie(cookie, path="/", secure=secure, httponly=True, samesite="Lax")
        return result

    def require_password(data, field="password"):
        limit(attempts, "Too many attempts from this device. Wait a few minutes and try again.")
        password = data.get(field)
        if not isinstance(password, str) or len(password) > PASSWORD_MAX or not password_matches(g.user["password_hash"], password):
            raise error("Your current password is incorrect.", 403)

    @app.before_request
    def authenticate():
        g.user = None
        if not request.path.startswith("/api/") or request.path in PUBLIC_PATHS:
            return
        token = request.cookies.get(cookie)
        row = db().execute("SELECT user_id, created_at, last_seen FROM auth_sessions WHERE token_hash=?",
                           (digest(token),)).fetchone() if token else None
        now = _now()
        if not row or now - row[2] > IDLE_SECONDS or now - row[1] > ABSOLUTE_SECONDS:
            if row:
                with db():
                    db().execute("DELETE FROM auth_sessions WHERE token_hash=?", (digest(token),))
            raise error("Please sign in to continue.", 401)
        if now - row[2] > 60:  # Activity is written at most once a minute.
            with db():
                db().execute("UPDATE auth_sessions SET last_seen=? WHERE token_hash=?", (now, digest(token)))
        g.user = user_by("id", row[0])
        if not g.user:
            raise error("Please sign in to continue.", 401)

    @app.post("/api/auth/register")
    def register():
        # Only accounts actually created count, so trying a few passwords never blocks sign-up.
        address = request.remote_addr or "unknown"
        if signups.full(address):
            raise error("Too many new accounts from this device. Try again later.", 429)
        data = body()
        username = username_from(data)
        if not USERNAME.fullmatch(username):
            raise error("Choose a username of 3–32 letters, numbers, dots, dashes or underscores, starting with a letter or number.")
        profile = profile_from(data)
        problem = password_problem(data.get("password"), username)
        if problem:
            raise error(problem)
        code, code_hash = new_recovery_code()
        user_id, password_hash = uuid4().hex, hash_password(data["password"])
        try:
            with db():
                first = db().execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
                db().execute("INSERT INTO users (id, username, password_hash, recovery_hash, profile, created_at) VALUES (?,?,?,?,?,?)",
                             (user_id, username, password_hash, code_hash, json.dumps(profile), datetime.now(timezone.utc).isoformat()))
                if first:  # A one-person install keeps the conversations made before accounts existed.
                    db().execute("UPDATE sessions SET owner_id=? WHERE owner_id IS NULL", (user_id,))
        except sqlite3.IntegrityError:
            raise error("That username is taken. Try another.", 409)
        signups.record(address)
        admin = first_admin(username)
        result = jsonify(user={"username": username, **profile, "voice_sample": False, "share_insights": False,
                                       "admin": admin, "voice_matching": False,
                                       "insight_viewer": admin or insight_viewer(username)}, recovery_code=code)
        result.status_code = 201
        return start_session(result, user_id)

    @app.post("/api/auth/login")
    def login():
        limit(attempts, "Too many sign-in attempts from this device. Wait a few minutes and try again.")
        data = body()
        username, password = username_from(data), data.get("password")
        if not isinstance(password, str) or not password or len(password) > PASSWORD_MAX:
            raise error(SIGN_IN_FAILED, 401)
        user = user_by("username", username) if USERNAME.fullmatch(username) else None
        now = _now()
        valid = password_matches(user["password_hash"] if user else dummy_hash(), password)
        if not user or not valid or user["locked_until"] > now:
            if user and user["locked_until"] <= now:
                failures = user["failed_logins"] + 1
                locked = now + min(LOCK_MAX_SECONDS, LOCK_BASE_SECONDS * 2 ** (failures - LOCK_AFTER)) if failures >= LOCK_AFTER else 0
                with db():
                    db().execute("UPDATE users SET failed_logins=?, locked_until=? WHERE id=?", (failures, locked, user["id"]))
                app.logger.warning("Failed sign-in for account %s (%d in a row).", user["id"][:8], failures)
                if locked:
                    app.logger.warning("Account %s paused for %d s after repeated failures.", user["id"][:8], locked - now)
            elif not user:
                app.logger.warning("Failed sign-in for an unknown username.")
            raise error(SIGN_IN_FAILED, 401)
        with db():
            db().execute("UPDATE users SET failed_logins=0, locked_until=0 WHERE id=?", (user["id"],))
        end_current_session()
        return start_session(jsonify(user=shown(user)), user["id"])

    @app.post("/api/auth/logout")
    def logout():
        end_current_session()
        return signed_out(app.make_response(("", 204)))

    @app.get("/api/auth/me")
    def me():
        return jsonify(user=shown(g.user))

    @app.put("/api/auth/profile")
    def update_profile():
        profile = profile_from(body())
        with db():
            db().execute("UPDATE users SET profile=? WHERE id=?", (json.dumps(profile), g.user["id"]))
        return jsonify(user={**shown(g.user), **profile})

    def first_admin(username):
        """The first account on an installation runs it: it can give NGO staff access to insights."""
        with db():
            if not db().execute("SELECT 1 FROM users WHERE is_admin = 1").fetchone():
                db().execute("UPDATE users SET is_admin = 1 WHERE username = ?", (username,))
        return bool(db().execute("SELECT is_admin FROM users WHERE username = ?", (username,)).fetchone()[0])

    def team_list():
        rows = db().execute("SELECT username, is_admin, is_insight_viewer FROM users "
                            "WHERE is_admin = 1 OR is_insight_viewer = 1 ORDER BY username").fetchall()
        return jsonify(team=[{"username": name, "admin": bool(admin), "insight_viewer": bool(viewer)} for name, admin, viewer in rows])

    @app.get("/api/admin/team")
    def team():
        """Who runs this installation and who at the NGO sees anonymous insights. Admins only."""
        if not g.user.get("is_admin"):
            raise error("Not found.", 404)
        return team_list()

    @app.put("/api/admin/team")
    def set_team_role():
        """Give or remove a role by username. There is always at least one admin."""
        if not g.user.get("is_admin"):
            raise error("Not found.", 404)
        data = body()
        username, role, on = str(data.get("username", "")).strip(), data.get("role"), data.get("on")
        if role not in ("admin", "insight_viewer") or not isinstance(on, bool) or not username:
            raise error("Choose a username, a role, and whether to give or remove it.")
        row = db().execute("SELECT id, is_admin FROM users WHERE lower(username) = lower(?)", (username,)).fetchone()
        if not row:
            raise error("There is no account with that username yet.", 404)
        if role == "admin" and not on and row[1] and db().execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0] <= 1:
            raise error("There must always be at least one admin.", 409)
        with db():
            db().execute(f"UPDATE users SET {'is_admin' if role == 'admin' else 'is_insight_viewer'} = ? WHERE id = ?", (int(on), row[0]))
        return team_list()

    @app.put("/api/auth/insights")
    def share_insights():
        """Her choice to share anonymous counts with the NGO. Off unless she turns it on."""
        share = body().get("share")
        if not isinstance(share, bool):
            raise error("Choose whether to share anonymous insights.")
        with db():
            db().execute("UPDATE users SET share_insights=? WHERE id=?", (int(share), g.user["id"]))
        return jsonify(user={**shown(g.user), "share_insights": share})

    @app.put("/api/auth/password")
    def change_password():
        data = body()
        require_password(data, "current_password")
        problem = password_problem(data.get("new_password"), g.user["username"])
        if problem:
            raise error(problem)
        password_hash = hash_password(data["new_password"])
        with db():
            db().execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, g.user["id"]))
            db().execute("DELETE FROM auth_sessions WHERE user_id=?", (g.user["id"],))  # Signed out everywhere...
        return start_session(jsonify(user=shown(g.user)), g.user["id"])  # ...then back in on this device.

    @app.post("/api/auth/recovery-code")
    def regenerate_recovery_code():
        require_password(body())
        code, code_hash = new_recovery_code()
        with db():
            db().execute("UPDATE users SET recovery_hash=? WHERE id=?", (code_hash, g.user["id"]))
        return jsonify(recovery_code=code)

    @app.post("/api/auth/recover")
    def recover():
        limit(attempts, "Too many attempts from this device. Wait a few minutes and try again.")
        data = body()
        username = username_from(data)
        problem = password_problem(data.get("new_password"), username)
        if problem:
            raise error(problem)
        user = user_by("username", username) if USERNAME.fullmatch(username) else None
        supplied = code_digest(data.get("recovery_code") or "")
        if not user or not hmac.compare_digest(supplied, user["recovery_hash"]):
            app.logger.warning("Failed account recovery.")
            raise error(RECOVERY_FAILED, 401)
        code, code_hash = new_recovery_code()  # Each code works once.
        password_hash = hash_password(data["new_password"])
        with db():
            db().execute("UPDATE users SET password_hash=?, recovery_hash=?, failed_logins=0, locked_until=0 WHERE id=?",
                         (password_hash, code_hash, user["id"]))
            db().execute("DELETE FROM auth_sessions WHERE user_id=?", (user["id"],))
        app.logger.warning("Account %s recovered with its recovery code; all its sessions ended.", user["id"][:8])
        return start_session(jsonify(user=shown(user), recovery_code=code), user["id"])

    @app.delete("/api/auth/account")
    def delete_account():
        require_password(body())
        with db():
            files = delete_user_data(db(), g.user["id"])
            db().execute("DELETE FROM auth_sessions WHERE user_id=?", (g.user["id"],))
            db().execute("DELETE FROM users WHERE id=?", (g.user["id"],))
        for path in files:
            path.unlink(missing_ok=True)
        app.logger.warning("Account %s deleted with all its conversations.", g.user["id"][:8])
        return signed_out(app.make_response(("", 204)))
