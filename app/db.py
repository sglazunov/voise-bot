"""PostgreSQL backend for the app's structured state.

When `DATABASE_URL` is set, every module that used to read/write a JSON file
under DATA_DIR delegates its load/save here instead. The app logic is unchanged:
each module still works with the same dicts / dataclasses — only the persistence
primitives differ. Media files (uploads, results, recordings) always stay on
disk/cloud; only records live in the DB.

Schema is normalised: one column per scalar field. The few genuinely
document-shaped fields keep a JSONB column (job.analysis / participants /
docx_providers, and the flexible per-user settings bag) — normalising those into
dozens of volatile columns would be brittle and buys nothing.

Collections that the callers treat as one blob (users, sessions, jobs, …) are
loaded/saved whole; `*_save` runs DELETE-missing + UPSERT inside one transaction,
so it's as atomic as the old temp-file replace.
"""
from __future__ import annotations

import threading
from typing import Any

from . import config

_pool = None
_lock = threading.Lock()


def enabled() -> bool:
    return bool(config.DATABASE_URL)


def _pool_obj():
    """Lazily-opened connection pool (psycopg 3). Creates the schema the first
    time it opens, so callers never race a missing table."""
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                from psycopg_pool import ConnectionPool
                p = ConnectionPool(conninfo=config.DATABASE_URL,
                                   min_size=1, max_size=10)
                p.wait(timeout=30)
                with p.connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(_SCHEMA)
                _pool = p
    return _pool


def _conn():
    return _pool_obj().connection()  # context manager; commits on clean exit


def _cur(conn):
    from psycopg.rows import dict_row
    return conn.cursor(row_factory=dict_row)


def _json(value):
    from psycopg.types.json import Json
    return Json(value)


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
JOB_SCALAR_COLS = [
    "id", "owner", "filename", "audio_path", "language", "diarize", "model",
    "initial_prompt", "glossary", "analyze", "provider", "analysis_instructions",
    "analysis_prompt", "capture_screen", "identify_speakers",
    "deliver_protocol_cloud", "deliver_weeek_task", "context_hint",
    "delete_audio_when_done", "status", "progress", "created_at", "started_at",
    "finished_at", "error", "duration", "speakers", "diarization_error",
    "speaker_error", "screen_error", "protocol_cloud_url", "delivery_error",
    "screen_segments", "analysis_error",
]
JOB_JSON_COLS = ["video_participants", "analysis", "docx_providers"]
JOB_COLS = JOB_SCALAR_COLS + JOB_JSON_COLS

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username        TEXT PRIMARY KEY,
    pw              TEXT NOT NULL,
    phone           TEXT,
    created_at      DOUBLE PRECISION,
    is_admin        BOOLEAN DEFAULT FALSE,
    team            TEXT,
    invite_code     TEXT,
    invite_code_at  DOUBLE PRECISION,
    is_super        BOOLEAN DEFAULT FALSE
);
-- Installs created before the team model: add the columns in place. Without
-- these, team/invite_code were silently dropped on every save — the invite code
-- appeared to change on every page load and could never be used to join.
ALTER TABLE users ADD COLUMN IF NOT EXISTS team           TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS invite_code    TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS invite_code_at DOUBLE PRECISION;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_super       BOOLEAN DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_users_team ON users(team);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    exp         DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(username);
CREATE TABLE IF NOT EXISTS recovery_codes (
    username    TEXT PRIMARY KEY,
    code_hash   TEXT NOT NULL,
    salt        TEXT NOT NULL,
    phone       TEXT,
    created     DOUBLE PRECISION,
    expires     DOUBLE PRECISION,
    attempts    INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
    id                     TEXT PRIMARY KEY,
    owner                  TEXT,
    filename               TEXT,
    audio_path             TEXT,
    language               TEXT,
    diarize                BOOLEAN,
    model                  TEXT,
    initial_prompt         TEXT,
    glossary               TEXT,
    provider               TEXT,
    analysis_instructions  TEXT,
    analysis_prompt        TEXT,
    capture_screen         BOOLEAN,
    identify_speakers      BOOLEAN,
    deliver_protocol_cloud BOOLEAN,
    deliver_weeek_task     TEXT,
    context_hint           TEXT,
    delete_audio_when_done BOOLEAN,
    status                 TEXT,
    progress               DOUBLE PRECISION,
    created_at             DOUBLE PRECISION,
    started_at             DOUBLE PRECISION,
    finished_at            DOUBLE PRECISION,
    error                  TEXT,
    duration               DOUBLE PRECISION,
    speakers               INTEGER,
    diarization_error      TEXT,
    speaker_error          TEXT,
    screen_error           TEXT,
    protocol_cloud_url     TEXT,
    delivery_error         TEXT,
    screen_segments        INTEGER,
    analysis_error         TEXT,
    "analyze"              BOOLEAN,
    video_participants     JSONB,
    analysis               JSONB,
    docx_providers         JSONB
);
CREATE INDEX IF NOT EXISTS idx_jobs_owner ON jobs(owner);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
CREATE TABLE IF NOT EXISTS user_settings (
    username    TEXT PRIMARY KEY,
    data        JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS user_creds (
    username    TEXT,
    provider    TEXT,
    idx         INTEGER,
    enc_key     TEXT,
    enc_extra   TEXT,
    PRIMARY KEY (username, provider, idx)
);
CREATE TABLE IF NOT EXISTS meetings (
    username     TEXT,
    meeting_key  TEXT,
    state        TEXT,
    detail       TEXT,
    job_id       TEXT,
    cloud_url    TEXT,
    do_protocol  BOOLEAN,
    saved_at     DOUBLE PRECISION,
    PRIMARY KEY (username, meeting_key)
);
-- One row per finished meeting/recording. Job rows are purged by retention
-- (VTX_RETENTION_HOURS), so the business numbers are recorded here to survive it.
CREATE TABLE IF NOT EXISTS meeting_stats (
    id            TEXT PRIMARY KEY,      -- job id
    team          TEXT NOT NULL,
    at            DOUBLE PRECISION,      -- when it finished
    title         TEXT,
    duration_sec  DOUBLE PRECISION,
    speakers      INTEGER,
    tasks         INTEGER,
    decisions     INTEGER,
    participants  INTEGER,
    has_protocol  BOOLEAN DEFAULT FALSE,
    ok            BOOLEAN DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_meeting_stats_team_at ON meeting_stats(team, at);
CREATE TABLE IF NOT EXISTS ai_context (
    username     TEXT PRIMARY KEY,
    global_text  TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ai_context_projects (
    username     TEXT,
    idx          INTEGER,
    name         TEXT,
    text         TEXT,
    PRIMARY KEY (username, idx)
);
"""


def init_schema() -> None:
    """Open the pool (which creates the schema). Safe to call on every startup."""
    _pool_obj()


# --------------------------------------------------------------------------- #
# users  {username: {pw, created_at, phone, is_admin}}
# --------------------------------------------------------------------------- #
def users_load() -> dict:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("SELECT username, pw, phone, created_at, is_admin, team, "
                    "invite_code, invite_code_at, is_super FROM users")
        out = {}
        for r in cur.fetchall():
            rec = {"pw": r["pw"], "phone": r.get("phone") or "",
                   "created_at": r.get("created_at"),
                   "is_admin": bool(r.get("is_admin"))}
            # Team fields are optional — keep them absent (not None) so the
            # file/DB shapes match and legacy fallbacks behave the same.
            if r.get("team"):
                rec["team"] = r["team"]
            if r.get("invite_code"):
                rec["invite_code"] = r["invite_code"]
            if r.get("invite_code_at") is not None:
                rec["invite_code_at"] = r["invite_code_at"]
            if r.get("is_super"):
                rec["is_super"] = True
            out[r["username"]] = rec
        return out


def users_save(users: dict) -> None:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("SELECT username FROM users")
        existing = {r["username"] for r in cur.fetchall()}
        for name in existing - set(users):
            cur.execute("DELETE FROM users WHERE username=%s", (name,))
        for name, rec in users.items():
            cur.execute(
                """INSERT INTO users (username, pw, phone, created_at, is_admin,
                                      team, invite_code, invite_code_at, is_super)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (username) DO UPDATE SET
                     pw=EXCLUDED.pw, phone=EXCLUDED.phone,
                     created_at=EXCLUDED.created_at, is_admin=EXCLUDED.is_admin,
                     team=EXCLUDED.team, invite_code=EXCLUDED.invite_code,
                     invite_code_at=EXCLUDED.invite_code_at,
                     is_super=EXCLUDED.is_super""",
                (name, rec.get("pw", ""), rec.get("phone") or None,
                 rec.get("created_at"), bool(rec.get("is_admin")),
                 rec.get("team"), rec.get("invite_code"),
                 rec.get("invite_code_at"), bool(rec.get("is_super"))))


# --------------------------------------------------------------------------- #
# sessions  {token_hash: {user, exp}}
# --------------------------------------------------------------------------- #
def sessions_load() -> dict:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("SELECT token_hash, username, exp FROM sessions")
        return {r["token_hash"]: {"user": r["username"], "exp": r["exp"]}
                for r in cur.fetchall()}


def sessions_save(sessions: dict) -> None:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("SELECT token_hash FROM sessions")
        existing = {r["token_hash"] for r in cur.fetchall()}
        for th in existing - set(sessions):
            cur.execute("DELETE FROM sessions WHERE token_hash=%s", (th,))
        for th, s in sessions.items():
            cur.execute(
                """INSERT INTO sessions (token_hash, username, exp) VALUES (%s,%s,%s)
                   ON CONFLICT (token_hash) DO UPDATE SET
                     username=EXCLUDED.username, exp=EXCLUDED.exp""",
                (th, s.get("user"), s.get("exp")))


# --------------------------------------------------------------------------- #
# recovery_codes  {username: {hash, salt, phone, created, expires, attempts}}
# --------------------------------------------------------------------------- #
def recovery_load() -> dict:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("SELECT username, code_hash, salt, phone, created, expires, attempts "
                    "FROM recovery_codes")
        return {r["username"]: {"hash": r["code_hash"], "salt": r["salt"],
                                "phone": r.get("phone") or "", "created": r.get("created"),
                                "expires": r.get("expires"), "attempts": r.get("attempts", 0)}
                for r in cur.fetchall()}


def recovery_save(rec: dict) -> None:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("SELECT username FROM recovery_codes")
        existing = {r["username"] for r in cur.fetchall()}
        for name in existing - set(rec):
            cur.execute("DELETE FROM recovery_codes WHERE username=%s", (name,))
        for name, e in rec.items():
            cur.execute(
                """INSERT INTO recovery_codes
                     (username, code_hash, salt, phone, created, expires, attempts)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (username) DO UPDATE SET
                     code_hash=EXCLUDED.code_hash, salt=EXCLUDED.salt,
                     phone=EXCLUDED.phone, created=EXCLUDED.created,
                     expires=EXCLUDED.expires, attempts=EXCLUDED.attempts""",
                (name, e.get("hash"), e.get("salt"), e.get("phone") or None,
                 e.get("created"), e.get("expires"), int(e.get("attempts", 0))))


# --------------------------------------------------------------------------- #
# jobs  (list of dicts matching asdict(Job))
# --------------------------------------------------------------------------- #
# "analyze" is a reserved word in PostgreSQL, so every job column is quoted.
_JOB_COLS_Q = ", ".join(f'"{c}"' for c in JOB_COLS)


def jobs_load() -> list:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute(f"SELECT {_JOB_COLS_Q} FROM jobs ORDER BY created_at")
        out = []
        for r in cur.fetchall():
            d = {c: r.get(c) for c in JOB_COLS}
            # normalise JSON defaults to match the dataclass field defaults
            d["video_participants"] = d.get("video_participants") or []
            d["docx_providers"] = d.get("docx_providers") or []
            out.append(d)
        return out


def jobs_save(rows: list) -> None:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("SELECT id FROM jobs")
        existing = {r["id"] for r in cur.fetchall()}
        keep = {d["id"] for d in rows}
        for jid in existing - keep:
            cur.execute("DELETE FROM jobs WHERE id=%s", (jid,))
        placeholders = ", ".join(["%s"] * len(JOB_COLS))
        updates = ", ".join(f'"{c}"=EXCLUDED."{c}"' for c in JOB_COLS if c != "id")
        sql = (f"INSERT INTO jobs ({_JOB_COLS_Q}) VALUES ({placeholders}) "
               f"ON CONFLICT (id) DO UPDATE SET {updates}")
        for d in rows:
            vals = []
            for c in JOB_COLS:
                v = d.get(c)
                vals.append(_json(v) if c in JOB_JSON_COLS else v)
            cur.execute(sql, vals)


# --------------------------------------------------------------------------- #
# user_settings  (per user: one JSONB bag, secrets already encrypted)
# --------------------------------------------------------------------------- #
def settings_load(user: str) -> dict | None:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("SELECT data FROM user_settings WHERE username=%s", (user,))
        row = cur.fetchone()
        return row["data"] if row else None


def settings_save(user: str, data: dict) -> None:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute(
            """INSERT INTO user_settings (username, data) VALUES (%s,%s)
               ON CONFLICT (username) DO UPDATE SET data=EXCLUDED.data""",
            (user, _json(data)))


# --------------------------------------------------------------------------- #
# user_creds  {provider: [{key, extra}, ...]}  (values already encrypted)
# --------------------------------------------------------------------------- #
def creds_load(user: str) -> dict:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("SELECT provider, idx, enc_key, enc_extra FROM user_creds "
                    "WHERE username=%s ORDER BY provider, idx", (user,))
        out: dict[str, list] = {}
        for r in cur.fetchall():
            out.setdefault(r["provider"], []).append(
                {"key": r.get("enc_key") or "", "extra": r.get("enc_extra") or ""})
        return out


def creds_save(user: str, raw: dict) -> None:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("DELETE FROM user_creds WHERE username=%s", (user,))
        for provider, entries in raw.items():
            for i, e in enumerate(entries or []):
                cur.execute(
                    "INSERT INTO user_creds (username, provider, idx, enc_key, enc_extra) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (user, provider, i, e.get("key") or "", e.get("extra") or ""))


# --------------------------------------------------------------------------- #
# meetings  (per user: {meeting_key: {state, detail, job_id, cloud_url,
#            do_protocol, saved_at}})
# --------------------------------------------------------------------------- #
def meetings_load(user: str) -> dict:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("SELECT meeting_key, state, detail, job_id, cloud_url, "
                    "do_protocol, saved_at FROM meetings WHERE username=%s", (user,))
        return {r["meeting_key"]: {"state": r.get("state"), "detail": r.get("detail"),
                                   "job_id": r.get("job_id"), "cloud_url": r.get("cloud_url"),
                                   "do_protocol": bool(r.get("do_protocol")),
                                   "saved_at": r.get("saved_at")}
                for r in cur.fetchall()}


def meetings_save(user: str, snaps: dict) -> None:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("SELECT meeting_key FROM meetings WHERE username=%s", (user,))
        existing = {r["meeting_key"] for r in cur.fetchall()}
        for key in existing - set(snaps):
            cur.execute("DELETE FROM meetings WHERE username=%s AND meeting_key=%s",
                        (user, key))
        for key, s in snaps.items():
            cur.execute(
                """INSERT INTO meetings
                     (username, meeting_key, state, detail, job_id, cloud_url,
                      do_protocol, saved_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (username, meeting_key) DO UPDATE SET
                     state=EXCLUDED.state, detail=EXCLUDED.detail,
                     job_id=EXCLUDED.job_id, cloud_url=EXCLUDED.cloud_url,
                     do_protocol=EXCLUDED.do_protocol, saved_at=EXCLUDED.saved_at""",
                (user, key, s.get("state"), s.get("detail"), s.get("job_id"),
                 s.get("cloud_url"), bool(s.get("do_protocol")), s.get("saved_at")))


# --------------------------------------------------------------------------- #
# ai_context  {global: str, projects: [{name, text}, ...]}
# --------------------------------------------------------------------------- #
def aicontext_load(user: str) -> dict | None:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("SELECT global_text FROM ai_context WHERE username=%s", (user,))
        row = cur.fetchone()
        if row is None:
            return None
        cur.execute("SELECT name, text FROM ai_context_projects WHERE username=%s "
                    "ORDER BY idx", (user,))
        projects = [{"name": r["name"], "text": r.get("text") or ""}
                    for r in cur.fetchall()]
        return {"global": row.get("global_text") or "", "projects": projects}


def aicontext_save(user: str, data: dict) -> None:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute(
            """INSERT INTO ai_context (username, global_text) VALUES (%s,%s)
               ON CONFLICT (username) DO UPDATE SET global_text=EXCLUDED.global_text""",
            (user, data.get("global", "")))
        cur.execute("DELETE FROM ai_context_projects WHERE username=%s", (user,))
        for i, p in enumerate(data.get("projects") or []):
            cur.execute("INSERT INTO ai_context_projects (username, idx, name, text) "
                        "VALUES (%s,%s,%s,%s)",
                        (user, i, p.get("name", ""), p.get("text", "")))


# --------------------------------------------------------------------------- #
# Account deletion — wipe every per-user row (users/sessions/recovery are
# handled by security's own save paths; here we clear the rest in one txn).
# --------------------------------------------------------------------------- #
def delete_user_data(user: str) -> None:
    with _conn() as conn, _cur(conn) as cur:
        for table in ("user_settings", "user_creds", "meetings",
                      "ai_context_projects", "ai_context"):
            cur.execute(f"DELETE FROM {table} WHERE username=%s", (user,))
        cur.execute("DELETE FROM meeting_stats WHERE team=%s", (user,))


# --------------------------------------------------------------------------- #
# meeting_stats — outcome of each finished meeting (for the Overview metrics)
# --------------------------------------------------------------------------- #
_STAT_COLS = ["id", "team", "at", "title", "duration_sec", "speakers",
              "tasks", "decisions", "participants", "has_protocol", "ok"]


def stats_add(row: dict) -> None:
    with _conn() as conn, _cur(conn) as cur:
        cols = ", ".join(_STAT_COLS)
        ph = ", ".join(["%s"] * len(_STAT_COLS))
        upd = ", ".join(f"{c}=EXCLUDED.{c}" for c in _STAT_COLS if c != "id")
        cur.execute(f"INSERT INTO meeting_stats ({cols}) VALUES ({ph}) "
                    f"ON CONFLICT (id) DO UPDATE SET {upd}",
                    [row.get(c) for c in _STAT_COLS])


def stats_load(team: str, since: float) -> list[dict]:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute(f"SELECT {', '.join(_STAT_COLS)} FROM meeting_stats "
                    "WHERE team=%s AND at >= %s ORDER BY at", (team, since))
        return [{c: r.get(c) for c in _STAT_COLS} for r in cur.fetchall()]
