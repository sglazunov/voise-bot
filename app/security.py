"""Authentication, sessions, and per-user secret encryption.

This is the trust boundary for the public deployment. Goals:
  * a login/password gate in front of the whole app (no anonymous access);
  * an opaque session token (cookie) that authenticates each request;
  * every user's data and secrets (Weeek/cloud tokens) stored under their login,
    encrypted at rest, and unreadable by any other login.

Threat model & trade-offs (see docs/БЕЗОПАСНОСТЬ.md, exercised by
tests/test_auth_security.py):
  * Passwords are never stored — only PBKDF2-HMAC-SHA256 hashes with per-user salt.
  * Secrets are encrypted at rest with a SERVER master key (env VTX_SECRET_KEY,
    else a generated data/secret.key). A per-user key is derived from it via HKDF,
    so each user's blob needs the master key AND that username to decrypt. The
    server can decrypt on demand because the 24/7 automation must use the tokens
    while the user is offline — this is an access-control + encryption-at-rest
    model, not zero-knowledge.
  * Isolation is enforced in the app layer: the session resolves exactly one user,
    and every per-user read/write is scoped to user_dir(username).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import config

_USERS_FILE = config.DATA_DIR / "users.json"
_SESSIONS_FILE = config.DATA_DIR / "sessions.json"
_KEY_FILE = config.DATA_DIR / "secret.key"
_LOCK = threading.RLock()

PBKDF2_ITERS = 200_000
MIN_PASSWORD_LEN = 8
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]{2,31}$")
SESSION_TTL = int(os.getenv("VTX_SESSION_TTL_HOURS", "168")) * 3600  # default 7 days
SESSION_COOKIE = "vtx_session"


# --------------------------------------------------------------------------- #
# Master key + per-user encryption
# --------------------------------------------------------------------------- #
def _master_key() -> bytes:
    """32-byte server master key: from VTX_SECRET_KEY, else a persisted random one."""
    env = os.getenv("VTX_SECRET_KEY")
    if env:
        return hashlib.sha256(env.encode("utf-8")).digest()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        if _KEY_FILE.exists():
            return bytes.fromhex(_KEY_FILE.read_text().strip())
        key = secrets.token_bytes(32)
        _KEY_FILE.write_text(key.hex())
        _chmod_600(_KEY_FILE)
        return key


def _user_fernet(username: str) -> Fernet:
    derived = HKDF(algorithm=hashes.SHA256(), length=32,
                   salt=b"vtx-user-secret-v1",
                   info=username.encode("utf-8")).derive(_master_key())
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_secret(username: str, plaintext: str) -> str:
    """Encrypt a secret for `username`. Empty string stays empty."""
    if not plaintext:
        return ""
    return _user_fernet(username).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(username: str, token: str) -> str:
    """Decrypt a secret for `username`. Returns "" if it can't (wrong user/key)."""
    if not token:
        return ""
    try:
        return _user_fernet(username).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return ""


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERS)
    return f"pbkdf2_sha256${PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, dk_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, AttributeError):
        return False


# --------------------------------------------------------------------------- #
# Users store
# --------------------------------------------------------------------------- #
def _chmod_600(p: Path) -> None:
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    _chmod_600(path)


def _load_users() -> dict:
    if not _USERS_FILE.exists():
        return {}
    try:
        return json.loads(_USERS_FILE.read_text(encoding="utf-8")) or {}
    except (ValueError, OSError):
        return {}


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def user_exists(username: str) -> bool:
    return normalize_username(username) in _load_users()


def list_users() -> list[str]:
    return list(_load_users().keys())


def user_dir(username: str) -> Path:
    """Private per-user data directory (jobs/results/recordings/settings)."""
    d = config.DATA_DIR / "users" / normalize_username(username)
    d.mkdir(parents=True, exist_ok=True)
    return d


def registration_allowed(code: str | None) -> tuple[bool, str]:
    """Policy: the first user bootstraps freely; afterwards a registration code
    (VTX_REGISTRATION_CODE) is required, unless VTX_ALLOW_OPEN_REGISTRATION=1."""
    with _LOCK:
        if not _load_users():
            return True, ""
    required = os.getenv("VTX_REGISTRATION_CODE", "")
    if required:
        if code and hmac.compare_digest(str(code), required):
            return True, ""
        return False, "Неверный код регистрации."
    if os.getenv("VTX_ALLOW_OPEN_REGISTRATION") == "1":
        return True, ""
    return False, "Регистрация закрыта администратором."


def create_user(username: str, password: str, code: str | None = None) -> dict:
    username = normalize_username(username)
    if not _USERNAME_RE.match(username):
        return {"ok": False, "error": "Логин: 3–32 символа, латиница/цифры/._-, "
                "начинается с буквы или цифры."}
    if len(password or "") < MIN_PASSWORD_LEN:
        return {"ok": False, "error": f"Пароль не короче {MIN_PASSWORD_LEN} символов."}
    ok, why = registration_allowed(code)
    if not ok:
        return {"ok": False, "error": why}
    with _LOCK:
        users = _load_users()
        if username in users:
            return {"ok": False, "error": "Такой логин уже существует."}
        users[username] = {"pw": hash_password(password), "created_at": time.time(),
                           "is_admin": not bool(users)}  # first user = admin
        _atomic_write_json(_USERS_FILE, users)
    user_dir(username)  # create their private dir up-front
    return {"ok": True, "username": username}


def verify_user(username: str, password: str) -> bool:
    username = normalize_username(username)
    users = _load_users()
    rec = users.get(username)
    if not rec:
        # Spend similar time so presence/absence isn't trivially timeable.
        hash_password(password)
        return False
    return verify_password(password, rec.get("pw", ""))


def change_password(username: str, old: str, new: str) -> dict:
    username = normalize_username(username)
    if not verify_user(username, old):
        return {"ok": False, "error": "Текущий пароль неверный."}
    if len(new or "") < MIN_PASSWORD_LEN:
        return {"ok": False, "error": f"Пароль не короче {MIN_PASSWORD_LEN} символов."}
    with _LOCK:
        users = _load_users()
        users[username]["pw"] = hash_password(new)
        _atomic_write_json(_USERS_FILE, users)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Brute-force protection (login / registration code)
# --------------------------------------------------------------------------- #
# Sliding window per key (client IP and IP+username): after N failures within
# the window, further attempts are rejected until it cools down. In-memory —
# resets on restart, which is fine for its purpose.
_FAILED: dict[str, list[float]] = {}
BRUTE_MAX_ATTEMPTS = int(os.getenv("VTX_LOGIN_MAX_ATTEMPTS", "8"))
BRUTE_WINDOW_SEC = int(os.getenv("VTX_LOGIN_WINDOW_SEC", "900"))  # 15 мин


def throttle_check(*keys: str) -> int:
    """Seconds the caller must still wait, or 0 if the attempt is allowed."""
    now = time.time()
    with _LOCK:
        worst = 0
        for k in keys:
            hits = [t for t in _FAILED.get(k, []) if now - t < BRUTE_WINDOW_SEC]
            _FAILED[k] = hits
            if len(hits) >= BRUTE_MAX_ATTEMPTS:
                worst = max(worst, int(BRUTE_WINDOW_SEC - (now - hits[0])) + 1)
        return worst


def throttle_fail(*keys: str) -> None:
    """Record a failed attempt for each key."""
    now = time.time()
    with _LOCK:
        for k in keys:
            _FAILED.setdefault(k, []).append(now)


def throttle_clear(*keys: str) -> None:
    """Forget failures (after a successful login)."""
    with _LOCK:
        for k in keys:
            _FAILED.pop(k, None)


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
def _load_sessions() -> dict:
    if not _SESSIONS_FILE.exists():
        return {}
    try:
        return json.loads(_SESSIONS_FILE.read_text(encoding="utf-8")) or {}
    except (ValueError, OSError):
        return {}


def _token_key(token: str) -> str:
    """Sessions are stored under a HASH of the token, so a leaked/backup-copied
    sessions.json cannot be replayed to hijack a session — the raw token exists
    only in the user's cookie."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(username: str) -> str:
    username = normalize_username(username)
    token = secrets.token_urlsafe(32)
    with _LOCK:
        sessions = _load_sessions()
        sessions[_token_key(token)] = {"user": username, "exp": time.time() + SESSION_TTL}
        _prune(sessions)
        _atomic_write_json(_SESSIONS_FILE, sessions)
    return token


def session_user(token: str | None) -> str | None:
    """Return the username for a valid, unexpired session token, else None."""
    if not token:
        return None
    key = _token_key(token)
    with _LOCK:
        sessions = _load_sessions()
        s = sessions.get(key)
        if not s:
            return None
        if s.get("exp", 0) < time.time():
            sessions.pop(key, None)
            _atomic_write_json(_SESSIONS_FILE, sessions)
            return None
        return s.get("user")


def destroy_session(token: str | None) -> None:
    if not token:
        return
    with _LOCK:
        sessions = _load_sessions()
        if sessions.pop(_token_key(token), None) is not None:
            _atomic_write_json(_SESSIONS_FILE, sessions)


def is_admin(username: str | None) -> bool:
    """Whether this login is the administrator (the first registered user)."""
    if not username:
        return False
    rec = _load_users().get(normalize_username(username))
    return bool(rec and rec.get("is_admin"))


def _prune(sessions: dict) -> None:
    now = time.time()
    for t in [t for t, s in sessions.items() if s.get("exp", 0) < now]:
        sessions.pop(t, None)
