"""Accounts, sign-in and per-person privacy. Run with: python3 -m unittest tests.test_auth"""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from werkzeug.security import check_password_hash, generate_password_hash

import auth
from app import create_app
from tests.support import FAST_HASH
from tests.test_facilitator import PREPARATION

PASSWORD = "my sister's blue bicycle"
NEW_PASSWORD = "a brand new long passphrase"
CODE = re.compile(rf"(?:[{auth.CODE_ALPHABET}]{{4}}-){{4}}[{auth.CODE_ALPHABET}]{{4}}")


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "sessions.sqlite3"
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name, "PASSWORD_HASH_METHOD": FAST_HASH})
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def device(self):
        return self.app.test_client()

    def register(self, client=None, username="asha", password=PASSWORD):
        return (client or self.client).post("/api/auth/register", json={
            "username": username, "password": password, "name": "Asha", "pronouns": "she/her"})

    def login(self, client=None, username="asha", password=PASSWORD):
        return (client or self.client).post("/api/auth/login", json={"username": username, "password": password})

    def rows(self, query, parameters=()):
        with closing(sqlite3.connect(self.database)) as conn:
            return conn.execute(query, parameters).fetchall()

    def test_every_api_route_needs_sign_in_except_public_ones(self):
        for method, path in (("get", "/api/sessions"), ("post", "/api/sessions"), ("get", "/api/auth/me"),
                             ("get", "/api/sessions/abc"), ("put", "/api/auth/profile")):
            with self.subTest(path=path):
                self.assertEqual(getattr(self.client, method)(path, json={}).status_code, 401)
        config = self.client.get("/api/config")
        self.assertEqual(config.status_code, 200)
        self.assertNotIn("profile", config.get_json())  # No more fixed "Ananth" profile.
        with self.client.get("/") as page:  # Closes the served file.
            self.assertEqual(page.status_code, 200)

    def test_passwords_follow_nist_length_and_blocklist_rules(self):
        weak = ["fourteen chars", "x" * 257, "aaaaaaaaaaaaaaaaaaaa", "passwordpassword", "123456789012345",
                "qwertyuiopasdfgh", "Common Ground rocks", "password1234567"]
        for index, password in enumerate(weak):
            with self.subTest(password=password[:20]):
                self.assertEqual(self.register(username=f"user{index}", password=password).status_code, 400)
        self.assertIn("15", self.register(username="neha", password="fourteen chars").get_json()["error"])
        self.assertEqual(self.register(username="asha", password="asha likes long walks").status_code, 400)
        # Any characters, no composition rules: a Hindi passphrase with spaces and no digits is fine.
        accepted = self.register(username="meera", password="मेरी बहन की नीली साइकिल")
        self.assertEqual(accepted.status_code, 201, accepted.get_json())

    def test_sign_up_signs_in_and_shows_a_recovery_code_once(self):
        created = self.register()
        self.assertEqual(created.status_code, 201, created.get_json())
        self.assertEqual(created.get_json()["user"], {"username": "asha", "name": "Asha", "pronouns": "she/her", "gender": None, "voice_sample": False,
                                                "share_insights": False, "insight_viewer": True,
                                                "admin": True, "voice_matching": False})
        code = created.get_json()["recovery_code"]
        self.assertTrue(CODE.fullmatch(code), code)
        self.assertEqual(self.client.get("/api/auth/me").get_json()["user"]["username"], "asha")
        cookie = created.headers["Set-Cookie"]
        for attribute in ("HttpOnly", "SameSite=Lax", "Path=/"):
            self.assertIn(attribute, cookie)
        for persistent in ("Expires", "Max-Age"):  # A browser-session cookie: no "keep me signed in" on shared phones.
            self.assertNotIn(persistent, cookie)
        (stored, recovery), = self.rows("SELECT password_hash, recovery_hash FROM users")
        self.assertNotIn(PASSWORD, stored)
        self.assertTrue(check_password_hash(stored, PASSWORD))
        self.assertNotIn(code.replace("-", ""), recovery)
        token = self.client.get_cookie("cg_session").value
        self.assertNotIn((token,), self.rows("SELECT token_hash FROM auth_sessions"))  # Session tokens are stored hashed.
        self.assertEqual(self.register(self.device(), username="ASHA").status_code, 409)  # Usernames ignore case.
        self.assertEqual(self.register(self.device(), username="a b").status_code, 400)

    def test_the_default_hash_is_owasp_scrypt(self):
        self.assertEqual(auth.PASSWORD_HASH_METHOD, "scrypt:131072:8:1")
        self.assertEqual(create_app({"TESTING": True, "DATA_DIR": self.temp.name}).config["PASSWORD_HASH_METHOD"],
                         auth.PASSWORD_HASH_METHOD)
        hashed = generate_password_hash(PASSWORD, method=auth.PASSWORD_HASH_METHOD)  # This Werkzeug accepts it.
        self.assertTrue(hashed.startswith("scrypt:131072:8:1$"))
        self.assertTrue(check_password_hash(hashed, PASSWORD))

    def test_sign_in_failures_are_generic_and_pause_the_account(self):
        self.register()
        self.client.post("/api/auth/logout")
        unknown = self.login(username="nobody")
        self.assertEqual(unknown.status_code, 401)
        for _ in range(auth.LOCK_AFTER):
            wrong = self.login(password="not the right passphrase")
            self.assertEqual((wrong.status_code, wrong.get_json()), (unknown.status_code, unknown.get_json()))
        start = time.time()
        with patch("auth._now", return_value=start + 30):
            paused = self.login()  # Even the right password is refused while paused, with the same message.
        self.assertEqual((paused.status_code, paused.get_json()), (unknown.status_code, unknown.get_json()))
        with patch("auth._now", return_value=start + auth.LOCK_BASE_SECONDS + 5):
            self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.rows("SELECT failed_logins, locked_until FROM users"), [(0, 0)])

    def test_sessions_end_at_sign_out_and_after_idle_or_absolute_timeouts(self):
        self.register()
        copied = self.client.get_cookie("cg_session").value
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 204)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        self.client.set_cookie("cg_session", copied)  # A copied cookie stops working once she signs out.
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        self.login()
        start = time.time()
        with patch("auth._now", return_value=start + auth.IDLE_SECONDS + 120):
            self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        self.login()
        start = time.time()
        for minutes in range(20, 13 * 60, 20):  # Active every 20 minutes...
            with patch("auth._now", return_value=start + minutes * 60):
                status = self.client.get("/api/auth/me").status_code
            if minutes * 60 <= auth.ABSOLUTE_SECONDS - 60:
                self.assertEqual(status, 200, minutes)
        self.assertEqual(status, 401)  # ...but never past the absolute limit.

    def test_people_only_reach_their_own_conversations(self):
        self.register()
        mine = self.client.post("/api/sessions", json={"preparation": PREPARATION}).get_json()
        self.assertEqual(mine["user"], {"name": "Asha", "pronouns": "she/her", "gender": None})
        other = self.device()
        self.register(other, username="bina")
        self.assertEqual(other.get("/api/sessions").get_json(), [])
        url = f"/api/sessions/{mine['id']}"
        for method, path, data in (("get", url, None), ("delete", url, None), ("get", url + "/recording", None),
                                   ("get", url + "/facilitator", None), ("put", url + "/preparation", {"preparation": PREPARATION})):
            with self.subTest(method=method, path=path):
                self.assertEqual(getattr(other, method)(path, json=data).status_code, 404)
        self.assertEqual([item["id"] for item in self.client.get("/api/sessions").get_json()], [mine["id"]])

    def test_the_first_account_keeps_conversations_from_before_accounts(self):
        legacy = {"id": "legacy1", "preparation": {"topic": "Before accounts"}, "recording": None}
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("INSERT INTO sessions (id, body, updated_at) VALUES (?,?,?)",
                         ("legacy1", json.dumps(legacy), "2026-11-01T00:00:00+00:00"))
        self.register()
        self.assertEqual([item["topic"] for item in self.client.get("/api/sessions").get_json()], ["Before accounts"])
        other = self.device()
        self.register(other, username="bina")
        self.assertEqual(other.get("/api/sessions").get_json(), [])

    def test_sensitive_changes_need_the_current_password(self):
        self.register()
        laptop = self.device()
        self.login(laptop)
        profile = self.client.put("/api/auth/profile", json={"name": "Asha R", "pronouns": "she/her", "gender": ""})
        self.assertEqual(profile.get_json()["user"]["name"], "Asha R")
        self.assertEqual(self.client.post("/api/sessions", json={"preparation": PREPARATION}).get_json()["user"]["name"], "Asha R")
        change = {"current_password": "wrong but long enough", "new_password": NEW_PASSWORD}
        self.assertEqual(self.client.put("/api/auth/password", json=change).status_code, 403)
        self.assertEqual(self.client.put("/api/auth/password", json={**change, "current_password": PASSWORD, "new_password": "too short"}).status_code, 400)
        self.assertEqual(self.client.put("/api/auth/password", json={**change, "current_password": PASSWORD}).status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)  # This device stays signed in...
        self.assertEqual(laptop.get("/api/auth/me").status_code, 401)  # ...her other devices are signed out.
        self.assertEqual(self.login(self.device()).status_code, 401)
        self.assertEqual(self.login(self.device(), password=NEW_PASSWORD).status_code, 200)
        self.assertEqual(self.client.post("/api/auth/recovery-code", json={"password": PASSWORD}).status_code, 403)
        fresh = self.client.post("/api/auth/recovery-code", json={"password": NEW_PASSWORD})
        self.assertTrue(CODE.fullmatch(fresh.get_json()["recovery_code"]))

    def test_a_recovery_code_resets_the_password_once_even_when_paused(self):
        code = self.register().get_json()["recovery_code"]
        phone = self.device()
        self.login(phone)
        for _ in range(auth.LOCK_AFTER):
            self.login(self.device(), password="not the right passphrase")
        reset = {"username": "asha", "recovery_code": "AAAA-BBBB-CCCC-DDDD-EEEE", "new_password": "the river near grandma's house"}
        wrong = self.client.post("/api/auth/recover", json=reset)
        unknown = self.client.post("/api/auth/recover", json={**reset, "username": "nobody", "recovery_code": code})
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual((wrong.status_code, wrong.get_json()), (unknown.status_code, unknown.get_json()))
        recovered = self.client.post("/api/auth/recover", json={**reset, "recovery_code": code.lower().replace("-", " ")})
        self.assertEqual(recovered.status_code, 200, recovered.get_json())
        self.assertNotEqual(recovered.get_json()["recovery_code"], code)
        self.assertEqual(phone.get("/api/auth/me").status_code, 401)  # Every other device is signed out.
        self.assertEqual(self.login(self.device(), password=reset["new_password"]).status_code, 200)  # And the pause is lifted.
        again = self.client.post("/api/auth/recover", json={**reset, "recovery_code": code, "new_password": "yet another long passphrase"})
        self.assertEqual(again.status_code, 401)  # A recovery code works once.

    def test_deleting_the_account_removes_everything(self):
        self.register()
        created = self.client.post("/api/sessions", json={"preparation": PREPARATION}).get_json()
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("INSERT INTO facilitator_events (session_id, capture_id, created_at, body) VALUES (?,?,?,?)",
                         (created["id"], "capture", "now", "{}"))
        self.assertEqual(self.client.delete("/api/auth/account", json={"password": "not the right passphrase"}).status_code, 403)
        self.assertEqual(self.client.delete("/api/auth/account", json={"password": PASSWORD}).status_code, 204)
        for table in ("users", "auth_sessions", "sessions", "facilitator_events"):
            self.assertEqual(self.rows(f"SELECT COUNT(*) FROM {table}"), [(0,)], table)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_cross_site_sign_in_is_refused(self):
        self.register()
        self.client.post("/api/auth/logout")
        refused = self.client.post("/api/auth/login", json={"username": "asha", "password": PASSWORD},
                                   headers={"Origin": "https://evil.example"})
        self.assertEqual(refused.status_code, 403)


if __name__ == "__main__":
    unittest.main()
