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

    def test_registration_closed_after_first_user(self, client, monkeypatch):
        # First user is the admin (free). Afterwards registration needs a code.
        assert register(client, "admin").status_code == 200
        monkeypatch.delenv("VTX_REGISTRATION_CODE", raising=False)
        monkeypatch.delenv("VTX_ALLOW_OPEN_REGISTRATION", raising=False)
        assert register(client, "intruder").status_code == 400

    def test_registration_code_required_and_checked(self, client, monkeypatch):
        assert register(client, "admin").status_code == 200
        monkeypatch.setenv("VTX_REGISTRATION_CODE", "let-me-in")
        assert register(client, "guest", code="wrong").status_code == 400
        assert register(client, "guest", code="let-me-in").status_code == 200


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
        register(c, username)
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
    """Password recovery by login+phone, plus the profile phone/password flows."""

    def test_registration_requires_phone(self, client):
        assert register(client, phone="").status_code == 400
        assert register(client, phone="12345").status_code == 400   # implausible
        assert register(client, phone="+7 999 000-00-00").status_code == 200

    def test_recover_with_correct_phone_resets_password(self, client):
        register(client, "alice", phone="8 (999) 000-00-00")  # 8XXX == +7XXX
        client.post("/api/auth/logout")
        r = client.post("/api/auth/recover", json={
            "username": "alice", "phone": "+79990000000",
            "new_password": "brand-new-pass1"})
        assert r.status_code == 200
        assert login(client, "alice", "password123").status_code == 401  # old dead
        assert login(client, "alice", "brand-new-pass1").status_code == 200

    def test_recover_with_wrong_phone_rejected(self, client):
        register(client, "alice")
        client.post("/api/auth/logout")
        r = client.post("/api/auth/recover", json={
            "username": "alice", "phone": "+79995554433",
            "new_password": "brand-new-pass1"})
        assert r.status_code == 400
        assert login(client, "alice", "password123").status_code == 200  # unchanged

    def test_recover_revokes_existing_sessions(self, client):
        register(client, "alice")                       # logged in via cookie
        assert client.get("/api/auth/me").status_code == 200
        from starlette.testclient import TestClient
        from app.main import app
        client2 = TestClient(app)
        client2.post("/api/auth/recover", json={
            "username": "alice", "phone": "+79990000000",
            "new_password": "brand-new-pass1"})
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
        # recovery now works only with the NEW phone
        assert client.post("/api/auth/recover", json={
            "username": "alice", "phone": "+79990000000",
            "new_password": "brand-new-pass1"}).status_code == 400
        assert client.post("/api/auth/recover", json={
            "username": "alice", "phone": "+79991112233",
            "new_password": "brand-new-pass1"}).status_code == 200

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
