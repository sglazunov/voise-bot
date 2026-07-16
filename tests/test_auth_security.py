"""Authentication & security test cases for the public deployment.

Covers the guarantees promised to the user:
  * the whole site is gated by login (no anonymous access);
  * an opaque session token authenticates requests;
  * each login's data and tokens are stored under that login, encrypted at rest,
    and unreadable by any other login.

Run:  pytest -q   (from the repo root)
"""
from pathlib import Path

from app import config, security
from conftest import register, login


# --------------------------------------------------------------------------- #
# A. The auth gate
# --------------------------------------------------------------------------- #
class TestGate:
    def test_api_requires_auth(self, client):
        # No session -> 401 on a protected API route.
        assert client.get("/api/jobs").status_code == 401

    def test_html_redirects_to_login(self, client):
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login"

    def test_public_paths_open(self, client):
        for path in ("/login", "/register", "/healthz"):
            assert client.get(path).status_code == 200

    def test_settings_endpoint_blocked_anonymously(self, client):
        assert client.get("/api/automation/settings").status_code == 401


# --------------------------------------------------------------------------- #
# B. Registration & login
# --------------------------------------------------------------------------- #
class TestRegistration:
    def test_first_user_can_register_and_gets_session(self, client):
        r = register(client)
        assert r.status_code == 200 and r.json()["ok"]
        assert security.SESSION_COOKIE in r.cookies
        # The session cookie must be HttpOnly (not readable by JS).
        set_cookie = r.headers.get("set-cookie", "")
        assert "httponly" in set_cookie.lower()

    def test_short_password_rejected(self, client):
        assert register(client, password="short").status_code == 400

    def test_invalid_username_rejected(self, client):
        assert register(client, username="ab").status_code == 400          # too short
        assert register(client, username="bad name!").status_code == 400   # bad chars

    def test_duplicate_username_rejected(self, client):
        assert register(client, "carol").status_code == 200
        assert register(client, "carol").status_code == 400

    def test_register_without_code_creates_own_team_admin(self, client):
        # No invite code -> you're the admin of your OWN team (own workspace).
        assert register(client, "alice").status_code == 200
        assert security.is_admin("alice") is True
        assert security.team_of("alice") == "alice"
        assert security.invite_code_of("alice")             # admins get a code

    def test_register_with_valid_code_joins_that_team(self, client):
        register(client, "admin")
        code = security.invite_code_of("admin")
        from starlette.testclient import TestClient
        from app.main import app
        member = TestClient(app)
        assert register(member, "member", phone="+79995556677", code=code).status_code == 200
        assert security.is_admin("member") is False         # a member, not an admin
        assert security.team_of("member") == "admin"        # joined admin's team
        assert security.invite_code_of("member") is None    # members have no code

    def test_register_with_bad_code_rejected(self, client):
        register(client, "admin")
        from starlette.testclient import TestClient
        from app.main import app
        c2 = TestClient(app)
        assert register(c2, "bob", phone="+79995556677", code="deadbeef").status_code == 400


class TestLogin:
    def test_login_success(self, client):
        register(client)
        client.cookies.clear()
        r = login(client)
        assert r.status_code == 200 and security.SESSION_COOKIE in r.cookies

    def test_login_wrong_password(self, client):
        register(client)
        assert login(client, password="nope-nope-nope").status_code == 401

    def test_login_unknown_user(self, client):
        assert login(client, username="ghost").status_code == 401

    def test_logout_invalidates_session(self, client):
        register(client)
        assert client.get("/api/jobs").status_code == 200      # authed
        client.post("/api/auth/logout")
        client.cookies.clear()
        assert client.get("/api/jobs").status_code == 401      # gone

    def test_forged_session_token_rejected(self, client):
        client.cookies.set(security.SESSION_COOKIE, "not-a-real-token")
        assert client.get("/api/jobs").status_code == 401


# --------------------------------------------------------------------------- #
# C. Secret storage: encryption at rest + redaction
# --------------------------------------------------------------------------- #
class TestSecrets:
    def test_token_saved_then_redacted_not_leaked(self, client):
        register(client)
        client.post("/api/automation/settings", json={"weeek_token": "TOP-SECRET-123"})
        got = client.get("/api/automation/settings").json()
        # The UI gets a presence flag, never the plaintext token.
        assert got["weeek_token"] is True
        assert "TOP-SECRET-123" not in str(got)

    def test_token_encrypted_on_disk(self, client):
        register(client)
        client.post("/api/automation/settings", json={"weeek_token": "TOP-SECRET-123"})
        f = config.DATA_DIR / "users" / "alice" / "automation.json"
        raw = f.read_text(encoding="utf-8")
        assert "TOP-SECRET-123" not in raw          # stored ciphertext only

    def test_password_is_hashed_not_stored(self, client):
        register(client, password="password123")
        raw = (config.DATA_DIR / "users.json").read_text(encoding="utf-8")
        assert "password123" not in raw
        assert "pbkdf2_sha256$" in raw


# --------------------------------------------------------------------------- #
# D. Cross-user isolation
# --------------------------------------------------------------------------- #
class TestIsolation:
    def _new_user_client(self, base_client, username):
        from starlette.testclient import TestClient
        from app.main import app
        c = TestClient(app)
        # bypass the post-first-user registration lock by using the code path:
        import os
        os.environ.pop("VTX_REGISTRATION_CODE", None)
        os.environ["VTX_ALLOW_OPEN_REGISTRATION"] = "1"
        # Phones are unique now, so each user needs a distinct number.
        suffix = str(sum(ord(ch) for ch in username)).rjust(7, "0")[-7:]
        register(c, username, phone="+7999" + suffix)
        return c

    def test_one_user_cannot_read_anothers_token(self, client, monkeypatch):
        monkeypatch.setenv("VTX_ALLOW_OPEN_REGISTRATION", "1")
        register(client, "alice")
        client.post("/api/automation/settings", json={"weeek_token": "ALICE-TOKEN"})

        bob = self._new_user_client(client, "bob")
        got = bob.get("/api/automation/settings").json()
        assert got["weeek_token"] is False                    # bob has no token
        # And bob's decrypted settings never expose alice's secret.
        from app.automation import settings as s
        assert s.load("bob").get("weeek_token", "") == ""
        assert s.load("alice")["weeek_token"] == "ALICE-TOKEN"

    def test_job_isolation(self, client, monkeypatch):
        monkeypatch.setenv("VTX_ALLOW_OPEN_REGISTRATION", "1")
        register(client, "alice")
        from app.jobs import store
        job = store.create("a.mp3", "/tmp/a.mp3", "ru", False, owner="alice")

        # alice sees it…
        assert any(j["id"] == job.id for j in client.get("/api/jobs").json())
        assert client.get(f"/api/jobs/{job.id}").status_code == 200

        # …bob does not.
        bob = self._new_user_client(client, "bob")
        assert all(j["id"] != job.id for j in bob.get("/api/jobs").json())
        assert bob.get(f"/api/jobs/{job.id}").status_code == 404
        assert bob.get(f"/api/jobs/{job.id}/result").status_code == 404


# --------------------------------------------------------------------------- #
# D2. Teams — a member who joined by code shares the admin's workspace
# --------------------------------------------------------------------------- #
class TestTeams:
    def _member_client(self, admin_username):
        from starlette.testclient import TestClient
        from app.main import app
        code = security.invite_code_of(admin_username)
        c = TestClient(app)
        assert register(c, "member", phone="+79995556677", code=code).status_code == 200
        return c

    def test_member_sees_admins_secrets_and_settings(self, client):
        register(client, "admin")
        # admin configures a secret token
        client.post("/api/automation/settings", json={"weeek_token": "TEAM-TOKEN", "poll_interval_sec": 77})
        member = self._member_client("admin")
        got = member.get("/api/automation/settings").json()
        assert got["weeek_token"] is True          # present (redacted) for the member too
        assert got["poll_interval_sec"] == 77
        # and decrypted server-side it's the SAME token the admin set
        from app.automation import settings as s
        assert s.load("member")["weeek_token"] == "TEAM-TOKEN"

    def test_member_shares_admins_llm_keys(self, client):
        register(client, "admin")
        from app import user_creds
        user_creds.add("admin", "groq", "SHARED-KEY")
        member = self._member_client("admin")
        keys = user_creds.load("member").get("groq", [])
        assert [e["key"] for e in keys] == ["SHARED-KEY"]

    def test_admin_with_members_cannot_delete_account(self, client):
        register(client, "admin")
        self._member_client("admin")
        r = client.post("/api/profile/delete", json={"password": "password123"})
        assert r.status_code == 400            # blocked: team has members
        assert security.user_exists("admin")


# --------------------------------------------------------------------------- #
# E. Crypto primitives (unit level)
# --------------------------------------------------------------------------- #
class TestCryptoPrimitives:
    def test_password_hash_roundtrip_and_uniqueness(self):
        h1 = security.hash_password("hunter2-strong")
        h2 = security.hash_password("hunter2-strong")
        assert h1 != h2                                   # random per-call salt
        assert security.verify_password("hunter2-strong", h1)
        assert not security.verify_password("wrong", h1)

    def test_secret_encryption_is_per_user(self):
        a = security.encrypt_secret("alice", "shared-plaintext")
        b = security.encrypt_secret("bob", "shared-plaintext")
        assert a != b                                     # different per-user key
        assert security.decrypt_secret("alice", a) == "shared-plaintext"
        # bob's key cannot decrypt alice's ciphertext.
        assert security.decrypt_secret("bob", a) == ""

    def test_empty_secret_stays_empty(self):
        assert security.encrypt_secret("alice", "") == ""
        assert security.decrypt_secret("alice", "") == ""


class TestPhoneRecovery:
    """Password recovery by SMS one-time code, plus the profile phone/password flows.

    Tests run with no SMS gateway configured, so `sms` uses its console provider
    which records every message in `sms.sent_messages` — that's how we read the
    code a real user would receive by SMS.
    """

    @staticmethod
    def _last_code():
        from app import sms
        import re
        assert sms.sent_messages, "no SMS was sent"
        m = re.search(r"\b(\d{6})\b", sms.sent_messages[-1]["text"])
        assert m, f"no 6-digit code in {sms.sent_messages[-1]['text']!r}"
        return m.group(1)

    @staticmethod
    def _request(client, username="alice", phone="+79990000000"):
        return client.post("/api/auth/recover/request",
                           json={"username": username, "phone": phone})

    def _recover(self, client, new_password, username="alice", phone="+79990000000"):
        assert self._request(client, username, phone).status_code == 200
        return client.post("/api/auth/recover/verify", json={
            "username": username, "phone": phone,
            "code": self._last_code(), "new_password": new_password})

    def test_registration_requires_phone(self, client):
        assert register(client, phone="").status_code == 400
        assert register(client, phone="12345").status_code == 400   # implausible
        assert register(client, phone="+7 999 000-00-00").status_code == 200

    def test_duplicate_phone_rejected_at_registration(self, client, monkeypatch):
        monkeypatch.setenv("VTX_ALLOW_OPEN_REGISTRATION", "1")
        assert register(client, "alice", phone="+79991112233").status_code == 200
        from starlette.testclient import TestClient
        from app.main import app
        c2 = TestClient(app)
        # same number (different formatting) -> rejected
        assert register(c2, "bob", phone="8 (999) 111-22-33").status_code == 400
        # a different number is fine
        assert register(c2, "bob", phone="+79995556677").status_code == 200

    def test_change_phone_to_taken_number_rejected(self, client, monkeypatch):
        monkeypatch.setenv("VTX_ALLOW_OPEN_REGISTRATION", "1")
        register(client, "alice", phone="+79991112233")
        from starlette.testclient import TestClient
        from app.main import app
        bob = TestClient(app)
        register(bob, "bob", phone="+79995556677")
        # bob can't take alice's number...
        assert bob.post("/api/profile/phone",
                        json={"password": "password123", "phone": "+79991112233"}).status_code == 400
        # ...but keeping his own is fine (self-exclusion)
        assert bob.post("/api/profile/phone",
                        json={"password": "password123", "phone": "+7 999 555-66-77"}).status_code == 200

    def test_recover_with_correct_code_resets_password(self, client):
        register(client, "alice", phone="8 (999) 000-00-00")  # 8XXX == +7XXX
        client.post("/api/auth/logout")
        assert self._recover(client, "brand-new-pass1").status_code == 200
        assert login(client, "alice", "password123").status_code == 401  # old dead
        assert login(client, "alice", "brand-new-pass1").status_code == 200

    def test_request_is_generic_and_sends_no_code_for_wrong_phone(self, client):
        from app import sms
        register(client, "alice")
        client.post("/api/auth/logout")
        before = len(sms.sent_messages)
        # Wrong phone → still a 200 generic answer (no account enumeration)…
        assert self._request(client, phone="+79995554433").status_code == 200
        # …but no SMS is actually sent.
        assert len(sms.sent_messages) == before

    def test_verify_with_wrong_code_rejected(self, client):
        register(client, "alice")
        client.post("/api/auth/logout")
        assert self._request(client).status_code == 200
        r = client.post("/api/auth/recover/verify", json={
            "username": "alice", "phone": "+79990000000",
            "code": "000000", "new_password": "brand-new-pass1"})
        assert r.status_code == 400
        assert login(client, "alice", "password123").status_code == 200  # unchanged

    def test_verify_without_request_rejected(self, client):
        register(client, "alice")
        client.post("/api/auth/logout")
        r = client.post("/api/auth/recover/verify", json={
            "username": "alice", "phone": "+79990000000",
            "code": "123456", "new_password": "brand-new-pass1"})
        assert r.status_code == 400   # no code was ever issued

    def test_code_burns_after_attempt_cap(self, client):
        register(client, "alice")
        client.post("/api/auth/logout")
        assert self._request(client).status_code == 200
        for _ in range(security.RECOVERY_MAX_ATTEMPTS):
            client.post("/api/auth/recover/verify", json={
                "username": "alice", "phone": "+79990000000",
                "code": "000000", "new_password": "brand-new-pass1"})
        # Even the CORRECT code now fails — the code was invalidated.
        r = client.post("/api/auth/recover/verify", json={
            "username": "alice", "phone": "+79990000000",
            "code": self._last_code(), "new_password": "brand-new-pass1"})
        assert r.status_code == 400

    def test_recover_revokes_existing_sessions(self, client):
        register(client, "alice")                       # logged in via cookie
        assert client.get("/api/auth/me").status_code == 200
        from starlette.testclient import TestClient
        from app.main import app
        client2 = TestClient(app)
        assert self._recover(client2, "brand-new-pass1").status_code == 200
        # the old session cookie must be dead after the reset
        assert client.get("/api/auth/me").status_code == 401

    def test_profile_change_phone_needs_password(self, client):
        register(client, "alice")
        bad = client.post("/api/profile/phone",
                          json={"password": "wrong", "phone": "+79991112233"})
        assert bad.status_code == 400
        ok = client.post("/api/profile/phone",
                         json={"password": "password123", "phone": "+79991112233"})
        assert ok.status_code == 200
        client.post("/api/auth/logout")
        # recovery now works only with the NEW phone: the old one sends no code.
        from app import sms
        before = len(sms.sent_messages)
        assert self._request(client, phone="+79990000000").status_code == 200
        assert len(sms.sent_messages) == before          # nothing sent to old phone
        assert self._recover(client, "brand-new-pass1",
                             phone="+79991112233").status_code == 200

    def test_profile_change_password_by_phone(self, client):
        register(client, "alice")
        r = client.post("/api/profile/password", json={
            "new_password": "changed-by-phone1", "phone": "+79990000000"})
        assert r.status_code == 200
        client.post("/api/auth/logout")
        assert login(client, "alice", "changed-by-phone1").status_code == 200

    def test_profile_masks_phone(self, client):
        register(client, "alice", phone="+79991234567")
        d = client.get("/api/profile").json()
        assert d["phone_masked"].endswith("67")
        assert "9991234" not in d["phone_masked"]   # middle digits hidden
