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

from . import config, db, logs

log = logs.get("vtx.security")

_USERS_FILE = config.DATA_DIR / "users.json"
_SESSIONS_FILE = config.DATA_DIR / "sessions.json"
_KEY_FILE = config.DATA_DIR / "secret.key"
_LOCK = threading.RLock()

PBKDF2_ITERS = 200_000
MIN_PASSWORD_LEN = 6
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


def pseudonym(username: str, scope: str = "") -> str:
    """Устойчивый псевдоним человека для аналитики (docs/ТЗ-МЕТРИКИ.md И65).

    Аналитической витрине нужен признак «это разные люди» и «это тот же
    человек, что вчера» — и НЕ нужен логин. Псевдоним считается от мастер-ключа
    сервера, поэтому по нему нельзя восстановить имя, а `scope` (обычно команда)
    делает псевдонимы одного и того же человека в разных командах несвязуемыми.

    ⚠️ Меняется вместе с `VTX_SECRET_KEY`: после смены ключа прежние псевдонимы
    перестают совпадать с новыми, и «уникальные читатели» за период, охвативший
    смену, будут завышены.
    """
    msg = f"{scope}\x00{normalize_username(username)}".encode("utf-8")
    return hmac.new(_master_key(), msg, hashlib.sha256).hexdigest()[:16]


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
    if db.enabled():
        return db.users_load()
    if not _USERS_FILE.exists():
        return {}
    try:
        return json.loads(_USERS_FILE.read_text(encoding="utf-8")) or {}
    except (ValueError, OSError):
        return {}


def _save_users(users: dict) -> None:
    if db.enabled():
        db.users_save(users)
    else:
        _atomic_write_json(_USERS_FILE, users)


def _get_user(username: str) -> dict:
    """Одна учётка по имени. На Postgres — точечный SELECT: раньше ради проверки
    «есть ли такой» или «в какой он команде» читалась ВСЯ таблица users, а
    team_of дёргается на каждой загрузке настроек."""
    name = normalize_username(username)
    if not name:
        return {}
    if db.enabled():
        return db.user_get(name) or {}
    return _load_users().get(name) or {}


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def user_exists(username: str) -> bool:
    return bool(_get_user(username))


def list_users() -> list[str]:
    return list(_load_users().keys())


def user_dir(username: str) -> Path:
    """Private per-user data directory (jobs/results/recordings/settings)."""
    d = config.DATA_DIR / "users" / normalize_username(username)
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- #
# Phone number (registration requirement + password recovery)
# --------------------------------------------------------------------------- #
def normalize_phone(raw: str | None) -> str:
    """Digits-only phone, «8XXXXXXXXXX» → «7XXXXXXXXXX». "" if implausible."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits if 10 <= len(digits) <= 15 else ""


def masked_phone(username: str) -> str:
    """Phone for display: all but the last 2 digits hidden («+7•••••••••45»)."""
    rec = _get_user(username)
    p = rec.get("phone", "")
    return f"+{p[0]}{'•' * (len(p) - 3)}{p[-2:]}" if len(p) >= 10 else ""


def verify_phone(username: str, phone: str) -> bool:
    """Constant-time check that `phone` matches the user's registered one."""
    rec = _get_user(username)
    stored = rec.get("phone", "")
    given = normalize_phone(phone)
    return bool(stored) and bool(given) and hmac.compare_digest(stored, given)


def _phone_owner(users: dict, phone: str, exclude: str | None = None) -> str | None:
    """Login that already owns `phone` (normalised), or None. `exclude` skips one
    user (their own current number when changing it). Phones are UNIQUE: one
    number belongs to at most one account."""
    if not phone:
        return None
    for uname, rec in users.items():
        if uname != exclude and (rec or {}).get("phone") == phone:
            return uname
    return None


def phone_in_use(phone: str, exclude: str | None = None) -> bool:
    """Whether `phone` is already registered to some (other) account."""
    return _phone_owner(_load_users(), normalize_phone(phone),
                        normalize_username(exclude) if exclude else None) is not None


def set_phone(username: str, password: str, new_phone: str) -> dict:
    """Change the phone; requires the CURRENT password (so a stolen session
    can't silently re-point recovery to the attacker's number)."""
    username = normalize_username(username)
    if not verify_user(username, password):
        return {"ok": False, "error": "Текущий пароль неверный."}
    phone = normalize_phone(new_phone)
    if not phone:
        return {"ok": False, "error": "Укажите корректный номер телефона (10–15 цифр)."}
    with _LOCK:
        users = _load_users()
        if _phone_owner(users, phone, exclude=username):
            return {"ok": False, "error": "Этот номер уже привязан к другому аккаунту."}
        users[username]["phone"] = phone
        _save_users(users)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Telegram для восстановления пароля (chat_id бота, см. app/telegram.py)
# --------------------------------------------------------------------------- #
def telegram_of(username: str) -> dict:
    """{chat_id, name} привязанного Telegram или {} — если не привязан."""
    rec = _get_user(username)
    chat = str(rec.get("telegram_chat_id") or "")
    if not chat:
        return {}
    return {"chat_id": chat, "name": str(rec.get("telegram_name") or "")}


def set_telegram(username: str, chat_id: str, name: str = "") -> bool:
    """Записать chat_id в учётку (зовётся ботом после «/start <код>»). Пароль
    проверен раньше — при ВЫДАЧЕ кода привязки (`main.profile_telegram_link`).
    Один chat_id — один аккаунт: у другого логина с тем же чатом привязка
    снимается, иначе коды двух аккаунтов уходили бы в один Telegram."""
    username = normalize_username(username)
    chat_id = str(chat_id or "").strip()
    if not username or not chat_id:
        return False
    with _LOCK:
        users = _load_users()
        if username not in users:
            return False
        for uname, rec in users.items():
            if uname != username and str((rec or {}).get("telegram_chat_id") or "") == chat_id:
                rec.pop("telegram_chat_id", None)
                rec.pop("telegram_name", None)
        users[username]["telegram_chat_id"] = chat_id
        users[username]["telegram_name"] = (name or "")[:64]
        _save_users(users)
    return True


def clear_telegram(username: str, password: str) -> dict:
    """Отвязать Telegram; требует ТЕКУЩИЙ пароль (по той же причине, что и
    смена телефона: угнанная сессия не должна лишать владельца канала)."""
    username = normalize_username(username)
    if not verify_user(username, password):
        return {"ok": False, "error": "Текущий пароль неверный."}
    with _LOCK:
        users = _load_users()
        if username not in users:
            return {"ok": False, "error": "Аккаунт не найден."}
        users[username].pop("telegram_chat_id", None)
        users[username].pop("telegram_name", None)
        _save_users(users)
    return {"ok": True}


def _set_password(username: str, new: str) -> None:
    with _LOCK:
        users = _load_users()
        users[username]["pw"] = hash_password(new)
        _save_users(users)


# --------------------------------------------------------------------------- #
# Password recovery by SMS one-time code (two steps: request → verify)
# --------------------------------------------------------------------------- #
# A short numeric code is generated, hashed (salted PBKDF2) and stored with an
# expiry + an attempt counter. The plaintext lives only in the SMS to the user's
# registered phone. Brute force is bounded by: short TTL, per-code attempt cap,
# and the IP/username login throttle applied at the API layer.
_RECOVERY_FILE = config.DATA_DIR / "recovery.json"
RECOVERY_CODE_TTL = int(os.getenv("VTX_RECOVERY_CODE_TTL_SEC", "600"))     # 10 мин
RECOVERY_MAX_ATTEMPTS = int(os.getenv("VTX_RECOVERY_MAX_ATTEMPTS", "5"))
RECOVERY_RESEND_SEC = int(os.getenv("VTX_RECOVERY_RESEND_SEC", "60"))       # anti-spam
_RECOVERY_ITERS = 60_000


def _load_recovery() -> dict:
    if db.enabled():
        return db.recovery_load()
    if not _RECOVERY_FILE.exists():
        return {}
    try:
        return json.loads(_RECOVERY_FILE.read_text(encoding="utf-8")) or {}
    except (ValueError, OSError):
        return {}


def _save_recovery(rec: dict) -> None:
    if db.enabled():
        db.recovery_save(rec)
    else:
        _atomic_write_json(_RECOVERY_FILE, rec)


def _hash_code(code: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", code.encode("utf-8"),
                             bytes.fromhex(salt), _RECOVERY_ITERS)
    return dk.hex()


def generate_recovery_code(username: str, phone: str) -> dict:
    """Step 1: if login + registered phone match, mint a fresh 6-digit code and
    store it (hashed). Returns {ok, code, phone} on match — the caller sends the
    code by SMS and must otherwise stay silent (no account enumeration).

    Returns {ok: False, retry_after} while a just-issued code is still within the
    resend cooldown, so repeated requests can't spam SMS."""
    username = normalize_username(username)
    if not verify_phone(username, phone):
        return {"ok": False, "error": "mismatch"}
    now = time.time()
    with _LOCK:
        rec = _load_recovery()
        prev = rec.get(username)
        if prev and prev.get("expires", 0) > now:
            elapsed = now - prev.get("created", 0)
            if elapsed < RECOVERY_RESEND_SEC:
                return {"ok": False, "error": "cooldown",
                        "retry_after": int(RECOVERY_RESEND_SEC - elapsed) + 1}
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_hex(8)
        rec[username] = {"hash": _hash_code(code, salt), "salt": salt,
                         "phone": normalize_phone(phone), "created": now,
                         "expires": now + RECOVERY_CODE_TTL, "attempts": 0}
        _save_recovery(rec)
    return {"ok": True, "code": code, "phone": normalize_phone(phone)}


def confirm_recovery_code(username: str, phone: str, code: str,
                          new_password: str) -> dict:
    """Step 2: validate the code (matching login + phone, not expired, within the
    attempt cap) and, on success, set the new password and revoke every session."""
    username = normalize_username(username)
    if len(new_password or "") < MIN_PASSWORD_LEN:
        return {"ok": False, "error": f"Пароль не короче {MIN_PASSWORD_LEN} символов."}
    given = re.sub(r"\D", "", code or "")
    with _LOCK:
        rec = _load_recovery()
        entry = rec.get(username)
        if not entry or entry.get("expires", 0) < time.time():
            rec.pop(username, None)
            _save_recovery(rec)
            return {"ok": False, "error": "Код не найден или истёк. Запросите новый."}
        entry["attempts"] = entry.get("attempts", 0) + 1
        if entry["attempts"] > RECOVERY_MAX_ATTEMPTS:
            rec.pop(username, None)
            _save_recovery(rec)
            return {"ok": False, "error": "Слишком много попыток. Запросите новый код."}
        matches = (bool(given)
                   and verify_phone(username, phone)
                   and hmac.compare_digest(_hash_code(given, entry["salt"]),
                                           entry["hash"]))
        if not matches:
            left = RECOVERY_MAX_ATTEMPTS - entry["attempts"]
            _save_recovery(rec)   # persist the used attempt
            tail = f" Осталось попыток: {left}." if left > 0 else ""
            return {"ok": False, "error": f"Неверный код.{tail}"}
        rec.pop(username, None)
        _save_recovery(rec)
    _set_password(username, new_password)
    destroy_user_sessions(username)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Teams (multi-tenant): each account belongs to a TEAM identified by its
# team-admin's login. All shared data (settings, tokens, LLM keys, AI context,
# meetings, jobs) is stored/encrypted under the team-admin's login, so members
# transparently work on the admin's configuration. Registering WITHOUT a code
# creates a new team (you become its admin, with a personal invite code);
# registering WITH a valid code joins that admin's team.
# --------------------------------------------------------------------------- #
# Код приглашения живёт МЕСЯЦ, потом протухает и профиль админа выдаёт новый.
# Код НЕ одноразовый: пока он жив, по нему может войти сколько угодно людей.
#
# Месяц — осознанный компромисс в пользу удобства: код раздают новым сотрудникам
# не каждый день, а суточный приходилось выпрашивать заново при каждом найме.
# Цена: утёкший код открывает доступ к настройкам, токенам и API-ключам команды
# на месяц вперёд. Если код ушёл не туда — сократить срок можно переменной
# VTX_INVITE_CODE_TTL_SEC, но погасить уже выданный код нечем, кроме как
# дождаться протухания.
INVITE_CODE_TTL = int(os.getenv("VTX_INVITE_CODE_TTL_SEC", str(30 * 24 * 3600)))


def _gen_invite_code(users: dict) -> str:
    existing = {(r or {}).get("invite_code") for r in users.values()}
    while True:
        c = secrets.token_hex(4)   # 8 hex chars, e.g. "a3f90c21"
        if c not in existing:
            return c


def _code_alive(rec: dict) -> bool:
    return bool(rec.get("invite_code")) and \
        (time.time() - (rec.get("invite_code_at") or 0)) <= INVITE_CODE_TTL


def _admin_by_code(users: dict, code: str | None) -> str | None:
    code = (code or "").strip().lower()
    if not code:
        return None
    for uname, rec in users.items():
        if not rec.get("is_admin") or not _code_alive(rec):
            continue
        if hmac.compare_digest(str(rec.get("invite_code", "")).lower(), code):
            return uname
    return None


def team_of(username: str) -> str:
    """The team-admin login this account belongs to (its own login for an admin,
    or for legacy accounts without a team)."""
    username = normalize_username(username)
    rec = _get_user(username)
    return rec.get("team") or username


def team_members(admin: str) -> list[str]:
    admin = normalize_username(admin)
    return [u for u, r in _load_users().items() if (r.get("team") or u) == admin]


def list_teams() -> list[str]:
    """Distinct team-admin logins (one automation workspace per team)."""
    users = _load_users()
    return sorted({(r.get("team") or u) for u, r in users.items()})


def invite_code_of(username: str) -> str | None:
    """The team's invite code — only admins have one. Новый выдаётся по запросу,
    когда прежний стал старше INVITE_CODE_TTL (по умолчанию месяц)."""
    username = normalize_username(username)
    with _LOCK:
        users = _load_users()
        rec = users.get(username)
        if not rec or not rec.get("is_admin"):
            return None
        if not _code_alive(rec):
            rec["invite_code"] = _gen_invite_code(users)
            rec["invite_code_at"] = time.time()
            _save_users(users)
        return rec["invite_code"]


def invite_code_expires_at(username: str) -> float | None:
    """When the current code stops working (unix ts), for the UI countdown."""
    rec = _get_user(username)
    if not rec.get("is_admin") or not rec.get("invite_code"):
        return None
    return (rec.get("invite_code_at") or 0) + INVITE_CODE_TTL


def team_member_list(admin: str) -> list[dict]:
    """Logins in this workspace (for the admin's profile)."""
    admin = normalize_username(admin)
    out = []
    for u, r in _load_users().items():
        if (r.get("team") or u) != admin:
            continue
        out.append({"username": u, "is_admin": bool(r.get("is_admin")),
                    "created_at": r.get("created_at")})
    return sorted(out, key=lambda x: x.get("created_at") or 0)


def remove_from_team(admin: str, member: str) -> dict:
    """Kick a member out of the admin's workspace. They keep their login but get
    their own EMPTY workspace, and every session of theirs is revoked at once."""
    admin = normalize_username(admin)
    member = normalize_username(member)
    if admin == member:
        return {"ok": False, "error": "Нельзя исключить самого себя."}
    with _LOCK:
        users = _load_users()
        a, m = users.get(admin), users.get(member)
        if not a or not a.get("is_admin"):
            return {"ok": False, "error": "Только админ команды может исключать участников."}
        if not m:
            return {"ok": False, "error": "Пользователь не найден."}
        if (m.get("team") or member) != admin:
            return {"ok": False, "error": "Этот пользователь не в вашей команде."}
        m["team"] = member          # own, empty workspace from now on
        m["is_admin"] = True
        m.pop("invite_code", None)
        m.pop("invite_code_at", None)
        _save_users(users)
    destroy_user_sessions(member)    # log them out immediately
    return {"ok": True}


def admin_by_invite_code(code: str | None) -> str | None:
    return _admin_by_code(_load_users(), code)


def create_user(username: str, password: str, code: str | None = None,
                phone: str | None = None) -> dict:
    username = normalize_username(username)
    if not _USERNAME_RE.match(username):
        return {"ok": False, "error": "Логин: 3–32 символа, латиница/цифры/._-, "
                "начинается с буквы или цифры."}
    if len(password or "") < MIN_PASSWORD_LEN:
        return {"ok": False, "error": f"Пароль не короче {MIN_PASSWORD_LEN} символов."}
    norm_phone = normalize_phone(phone)
    if not norm_phone:
        return {"ok": False, "error": "Укажите номер телефона (10–15 цифр) — "
                "он нужен для восстановления пароля."}
    with _LOCK:
        users = _load_users()
        if username in users:
            return {"ok": False, "error": "Такой логин уже существует."}
        if _phone_owner(users, norm_phone):
            return {"ok": False, "error": "Этот номер телефона уже привязан к другому аккаунту."}
        code = (code or "").strip()
        if code:
            admin = _admin_by_code(users, code)
            if not admin:
                return {"ok": False, "error": "Неверный код приглашения."}
            team, is_admin, invite = admin, False, None
        else:
            team, is_admin, invite = username, True, _gen_invite_code(users)
        rec = {"pw": hash_password(password), "created_at": time.time(),
               "phone": norm_phone, "is_admin": is_admin, "team": team}
        if invite:
            rec["invite_code"] = invite
            rec["invite_code_at"] = time.time()   # starts the daily rotation
        users[username] = rec
        _save_users(users)
    user_dir(username)  # create their private dir up-front
    return {"ok": True, "username": username, "is_admin": is_admin, "team": team}


def verify_user(username: str, password: str) -> bool:
    username = normalize_username(username)
    users = _load_users()
    rec = users.get(username)
    if not rec:
        # Spend similar time so presence/absence isn't trivially timeable.
        hash_password(password)
        return False
    return verify_password(password, rec.get("pw", ""))


def change_password(username: str, new: str, old: str | None = None,
                    phone: str | None = None) -> dict:
    """Change the password from the profile. The user proves it's them with
    EITHER the current password OR the registered phone number."""
    username = normalize_username(username)
    if len(new or "") < MIN_PASSWORD_LEN:
        return {"ok": False, "error": f"Пароль не короче {MIN_PASSWORD_LEN} символов."}
    if old:
        if not verify_user(username, old):
            return {"ok": False, "error": "Текущий пароль неверный."}
    elif phone:
        if not verify_phone(username, phone):
            return {"ok": False, "error": "Номер телефона не совпадает."}
    else:
        return {"ok": False, "error": "Подтвердите личность: текущий пароль или телефон."}
    _set_password(username, new)
    return {"ok": True}


def delete_account(username: str, password: str) -> dict:
    """Permanently delete an account: the login, phone, sessions, recovery code
    and ALL of the user's data (settings, tokens, LLM keys, AI context, meeting
    states, and their files on disk). Requires the current password."""
    username = normalize_username(username)
    if not verify_user(username, password):
        return {"ok": False, "error": "Пароль неверный."}
    with _LOCK:
        users = _load_users()
        if username not in users:
            return {"ok": False, "error": "Аккаунт не найден."}
        # A team-admin can't delete the workspace out from under its members
        # (all shared data lives under the admin's login). They must remove the
        # members first. A member deleting themselves only removes their login.
        if users[username].get("is_admin"):
            members = [u for u in users
                       if u != username and (users[u].get("team") or u) == username]
            if members:
                return {"ok": False, "error": f"В вашей команде есть участники "
                        f"({len(members)}). Сначала удалите их аккаунты."}
        users.pop(username, None)
        _save_users(users)
        rec = _load_recovery()
        if rec.pop(username, None) is not None:
            _save_recovery(rec)
    destroy_user_sessions(username)
    _delete_user_data(username)
    return {"ok": True}


def _delete_user_data(username: str) -> None:
    """Remove the user's per-user data (DB rows when on Postgres) and their files
    on disk (recordings/results/uploads/settings)."""
    username = normalize_username(username)
    if db.enabled():
        try:
            db.delete_user_data(username)
        except Exception:
            log.error("Данные пользователя %s не удалены из базы — файлы будут "
                      "стёрты, а строки останутся", username, exc_info=True)
    import shutil
    shutil.rmtree(config.DATA_DIR / "users" / username, ignore_errors=True)


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
    """Только файловый режим: на Postgres сессии читаются по одной."""
    if not _SESSIONS_FILE.exists():
        return {}
    try:
        return json.loads(_SESSIONS_FILE.read_text(encoding="utf-8")) or {}
    except (ValueError, OSError):
        return {}


def _save_sessions(sessions: dict) -> None:
    """Только файловый режим: на Postgres сессии пишутся по одной."""
    _atomic_write_json(_SESSIONS_FILE, sessions)


def _token_key(token: str) -> str:
    """Sessions are stored under a HASH of the token, so a leaked/backup-copied
    sessions.json cannot be replayed to hijack a session — the raw token exists
    only in the user's cookie."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# Работа с сессиями на Postgres идёт ТОЧЕЧНО: одна строка по ключу. Раньше
# каждый из этих вызовов — а session_user выполняется на КАЖДОМ HTTP-запросе —
# читал всю таблицу сессий в память и при любом изменении переписывал её
# целиком. Файловый режим (без БД) так и остался «прочитать всё → записать
# всё»: там это один маленький JSON и другого способа нет.
def create_session(username: str) -> str:
    username = normalize_username(username)
    token = secrets.token_urlsafe(32)
    exp = time.time() + SESSION_TTL
    if db.enabled():
        db.session_put(_token_key(token), username, exp)
        db.sessions_prune(time.time())
        return token
    with _LOCK:
        sessions = _load_sessions()
        sessions[_token_key(token)] = {"user": username, "exp": exp}
        _prune(sessions)
        _save_sessions(sessions)
    return token


def session_user(token: str | None) -> str | None:
    """Return the username for a valid, unexpired session token, else None."""
    if not token:
        return None
    key = _token_key(token)
    if db.enabled():
        s = db.session_get(key)
        if not s:
            return None
        if s.get("exp", 0) < time.time():
            db.session_delete(key)
            return None
        return s.get("user")
    with _LOCK:
        sessions = _load_sessions()
        s = sessions.get(key)
        if not s:
            return None
        if s.get("exp", 0) < time.time():
            sessions.pop(key, None)
            _save_sessions(sessions)
            return None
        return s.get("user")


def destroy_session(token: str | None) -> None:
    if not token:
        return
    if db.enabled():
        db.session_delete(_token_key(token))
        return
    with _LOCK:
        sessions = _load_sessions()
        if sessions.pop(_token_key(token), None) is not None:
            _save_sessions(sessions)


def destroy_user_sessions(username: str) -> None:
    """Revoke EVERY session of a user (called after a password recovery, so a
    possibly-compromised old session dies with the old password)."""
    username = normalize_username(username)
    if db.enabled():
        db.sessions_delete_user(username)
        return
    with _LOCK:
        sessions = _load_sessions()
        stale = [k for k, s in sessions.items() if s.get("user") == username]
        for k in stale:
            sessions.pop(k, None)
        if stale:
            _save_sessions(sessions)


def is_admin(username: str | None) -> bool:
    """Whether this login is a TEAM admin (owns its workspace). NOTE: with the
    multi-tenant model almost everyone is a team admin, so this must NOT gate
    server-wide operations — use is_super_admin for those."""
    if not username:
        return False
    rec = _get_user(username)
    return bool(rec.get("is_admin"))


def is_super_admin(username: str | None) -> bool:
    """The SERVER founder — the earliest-registered account. Only they may run
    GLOBAL server operations (package installs, server-wide HF token) that affect
    every team on the shared host. Distinct from a team admin. The founder is
    marked with a persistent `is_super` flag, back-filled once for legacy data."""
    if not username:
        return False
    username = normalize_username(username)
    with _LOCK:
        users = _load_users()
        if not users:
            return False
        supers = [u for u, r in users.items() if r.get("is_super")]
        if not supers:  # back-fill: the earliest account is the founder
            founder = min(users, key=lambda u: users[u].get("created_at") or 0)
            users[founder]["is_super"] = True
            _save_users(users)
            supers = [founder]
        return username in supers


def _prune(sessions: dict) -> None:
    now = time.time()
    for t in [t for t, s in sessions.items() if s.get("exp", 0) < now]:
        sessions.pop(t, None)
