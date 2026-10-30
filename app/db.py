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
    "deliver_protocol_cloud", "deliver_weeek_task", "context_hint", "user_notes",
    "preset",
    "delete_audio_when_done", "status", "progress", "created_at", "started_at",
    "finished_at", "error", "duration", "speakers", "diarization_error",
    "speaker_error", "screen_error", "protocol_cloud_url", "delivery_error",
    "screen_segments", "analysis_error", "transcribe_sec", "stop_reason",
    "live_path",
]
JOB_JSON_COLS = ["video_participants", "analysis", "docx_providers", "weeek_tasks",
                 "llm_usage"]
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
    is_super        BOOLEAN DEFAULT FALSE,
    telegram_chat_id TEXT,
    telegram_name   TEXT
);
-- Installs created before the team model: add the columns in place. Without
-- these, team/invite_code were silently dropped on every save — the invite code
-- appeared to change on every page load and could never be used to join.
ALTER TABLE users ADD COLUMN IF NOT EXISTS team           TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS invite_code    TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS invite_code_at DOUBLE PRECISION;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_super       BOOLEAN DEFAULT FALSE;
-- Telegram для кодов восстановления пароля (app/telegram.py).
ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_chat_id TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_name    TEXT;
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
    user_notes             TEXT,
    preset                 TEXT,
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
    transcribe_sec         DOUBLE PRECISION,
    "analyze"              BOOLEAN,
    video_participants     JSONB,
    analysis               JSONB,
    docx_providers         JSONB
);
CREATE INDEX IF NOT EXISTS idx_jobs_owner ON jobs(owner);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS user_notes TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS preset TEXT;
-- Чистое время РАСПОЗНАВАНИЯ. finished_at-started_at для этого не годится:
-- пересборка протокола сдвигает конец, а начало остаётся от первого прогона,
-- и час встречи выглядел как семь часов работы.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS transcribe_sec DOUBLE PRECISION;
-- Черновики задач для Weeek из протокола (см. app/weeek_tasks.py).
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS weeek_tasks JSONB;
-- Расход модели на задачу (см. app/usage.py): токены входа/выхода, сколько
-- взято из кэша, разбивка по моделям. Копится за все прогоны, включая
-- пересборки, — деньги тратятся за каждый.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS llm_usage JSONB;
-- Чем кончилась запись, из которой взялась задача (silence, max_duration,
-- chat_stop…). Рекордер возвращал это всегда, но никто не читал.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS stop_reason TEXT;
-- Живая расшифровка встречи (куски, распознанные по ходу записи) — файл
-- рядом с записью; задача дораспознаёт только хвост после неё.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS live_path TEXT;
CREATE TABLE IF NOT EXISTS search_docs (
    job_id     TEXT PRIMARY KEY,
    username   TEXT,
    title      TEXT,
    body       TEXT,
    created_at DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_search_user ON search_docs(username);
CREATE INDEX IF NOT EXISTS idx_search_tsv ON search_docs
    USING GIN (to_tsvector('russian', coalesce(title,'') || ' ' || coalesce(body,'')));
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
    added_at    DOUBLE PRECISION,
    PRIMARY KEY (username, provider, idx)
);
-- Когда ключ добавили. Нужно для срока жизни: бесплатный ключ бывает выдан на
-- полгода, и по истечении протоколы начинают молча собираться запасным
-- движком. Установки, созданные до появления колонки, добирают её здесь.
ALTER TABLE user_creds ADD COLUMN IF NOT EXISTS added_at DOUBLE PRECISION;
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
-- Дозаливка колонок идёт СТРОГО после CREATE соответствующей таблицы: на
-- чистой базе ALTER выше по тексту падает с UndefinedTable, и приложение
-- уходит в бесконечный перезапуск. На базе разработчика таблица уже
-- существовала, поэтому ошибка проявлялась только при установке с нуля.
ALTER TABLE meetings ADD COLUMN IF NOT EXISTS out_path TEXT;
ALTER TABLE meetings ADD COLUMN IF NOT EXISTS live_notes TEXT;
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
-- Расход модели на встречу. Живёт ЗДЕСЬ, а не только в jobs: строки задач
-- стирает суточный ретеншн, а расход команды нужен за месяцы. Цена (usd)
-- пишется, только если цена модели задана (VTX_MODEL_PRICES), иначе NULL —
-- пустое место честнее, чем «0 ₽ за встречу».
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS tokens_in BIGINT;
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS tokens_cached BIGINT;
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS tokens_out BIGINT;
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS llm_calls INTEGER;
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS usd DOUBLE PRECISION;
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS engines TEXT;
-- Качество протокола. Всё это УЖЕ считается на каждой встрече (verification.
-- stats, _quality, _dropped_topics, _dropped, _fallback, _edited) и жило ровно
-- сутки внутри jobs.analysis, после чего стиралось ретеншном вместе с задачей.
-- Здесь оно переживает ретеншн: без этого нельзя ответить, становится продукт
-- лучше или хуже. Подробности — docs/ТЗ-МЕТРИКИ.md §3.
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS protocol_ok BOOLEAN;
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS engine TEXT;
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS fallback BOOLEAN;
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS preset TEXT;
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS transcribe_sec DOUBLE PRECISION;
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS verify_checked INTEGER;
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS verify_confirmed INTEGER;
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS verify_mode TEXT;
-- Версия правил проверки: доля подтверждённых сравнима только внутри одной.
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS verify_version TEXT;
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS topics INTEGER;
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS empty_topics INTEGER;
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS dropped_topics INTEGER;
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS dropped_items INTEGER;
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS summary_is_toc BOOLEAN;
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS edited BOOLEAN;
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS tasks_with_owner INTEGER;
-- Расход по каждой модели отдельно: раньше by_model схлопывался в строку имён
-- без чисел, и «сколько стоит Gemini против Groq» посчитать было нечем.
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS tokens_by_model JSONB;
-- Исходы встречи (docs/ТЗ-МЕТРИКИ.md §14.2). До этого метрика знала только те
-- встречи, по которым СОЗДАЛАСЬ задача распознавания: пропущенная, отфильтрованная
-- и сорвавшаяся запись не оставляли следа вообще, а «явка бота» (И36) требует
-- знаменателя из ЗАПЛАНИРОВАННЫХ встреч. Поэтому строки теперь двух видов:
--   kind='job'     — задача распознавания (всё, что было раньше; NULL у старых строк);
--   kind='meeting' — исход встречи у планировщика (missed|skipped|rec_error|recorded).
-- Считать их вместе нельзя: у записанной встречи есть и та, и другая строка.
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS kind TEXT;
-- Почему остановилась запись: silence|max_duration|chat_stop|call_ended|
-- left_call|nobody_joined|thinned_out|stopped|error. Рекордер возвращал это с
-- самого начала, а планировщик не читал — четырёхчасовые записи пустой комнаты
-- разбирались руками именно поэтому.
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS stop_reason TEXT;
-- Запись не уехала в облако (текст причины). Видео не должно жить на сервере.
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS upload_error TEXT;
-- Человеческая причина пропуска/отказа — то, что видит пользователь в карточке.
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS detail TEXT;
-- На сколько секунд бот опоздал в звонок. «Явка» — это не только «пришёл или
-- нет»: бот, вошедший к середине, теряет начало, где обычно и ставят задачи.
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS join_delay_sec DOUBLE PRECISION;
-- Были периоды без звука (пустые минуты платятся трижды — И32).
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS silent BOOLEAN;
-- Отчёт клиенту (И58): вопросы без ответа и задачи прошлой встречи без упоминания.
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS unanswered INTEGER;
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS carried_stale INTEGER;
-- Связь строки встречи со строкой задачи: без неё себестоимость записи и
-- себестоимость модели одной встречи не складываются (И27).
ALTER TABLE meeting_stats ADD COLUMN IF NOT EXISTS job_id TEXT;
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
CREATE TABLE IF NOT EXISTS meeting_series (
    username    TEXT PRIMARY KEY,
    data        JSONB NOT NULL
);
-- Продуктовые события: что сделал ЧЕЛОВЕК (docs/ТЗ-МЕТРИКИ.md §4).
-- Человек обозначен ПСЕВДОНИМОМ (security.pseudonym), логина здесь нет и
-- содержимого разговора — тоже: только вид действия, встреча и время.
-- Первичный ключ несёт дедупликацию «встреча + человек + сутки» (И7): иначе
-- «доля прочитанных протоколов» считала бы не читателей, а нажатия.
CREATE TABLE IF NOT EXISTS product_events (
    id       TEXT PRIMARY KEY,
    team     TEXT NOT NULL,
    kind     TEXT NOT NULL,
    job_id   TEXT,
    actor    TEXT,
    source   TEXT,
    at       DOUBLE PRECISION,
    extra    JSONB
);
CREATE INDEX IF NOT EXISTS idx_product_events_team_at ON product_events(team, at);
-- Куски встреч для поиска и вопросов ПО ВСЕМ встречам команды
-- (app/meeting_index.py). ⚠️ ПЕРЕЖИВАЮТ ретеншн: search_docs стирается вместе
-- с задачей через сутки, а память о встречах нужна за месяцы — как
-- meeting_stats. Удаляются только с аккаунтом команды (delete_user_data) или
-- при переиндексации того же документа (chunks_replace).
CREATE TABLE IF NOT EXISTS meeting_chunks (
    id           TEXT PRIMARY KEY,      -- <doc_id>:<kind><idx>
    doc_id       TEXT NOT NULL,         -- job id или archive:<хэш пути>
    team         TEXT NOT NULL,
    series_key   TEXT,
    title        TEXT,
    meeting_date TEXT,                  -- дд.мм.гггг, как в meeting_series
    meeting_ts   DOUBLE PRECISION,      -- для сортировки по дате
    kind         TEXT NOT NULL,         -- transcript | protocol
    idx          INTEGER NOT NULL,
    ts           TEXT,                  -- первая метка времени окна
    text         TEXT NOT NULL,
    is_draft     BOOLEAN DEFAULT FALSE,
    source       TEXT,                  -- job:<id> или путь файла в облаке
    created_at   DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_meeting_chunks_doc ON meeting_chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_meeting_chunks_team_ts ON meeting_chunks(team, meeting_ts);
CREATE INDEX IF NOT EXISTS idx_meeting_chunks_tsv ON meeting_chunks
    USING GIN (to_tsvector('russian', text));
"""


def init_schema() -> None:
    """Open the pool (which creates the schema). Safe to call on every startup."""
    _pool_obj()


# --------------------------------------------------------------------------- #
# users  {username: {pw, created_at, phone, is_admin}}
# --------------------------------------------------------------------------- #
def _telegram_into(rec: dict, row: dict) -> None:
    # Как и поля команды: отсутствуют, а не None, чтобы файл и БД совпадали.
    if row.get("telegram_chat_id"):
        rec["telegram_chat_id"] = str(row["telegram_chat_id"])
        if row.get("telegram_name"):
            rec["telegram_name"] = row["telegram_name"]


def users_load() -> dict:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("SELECT username, pw, phone, created_at, is_admin, team, "
                    "invite_code, invite_code_at, is_super, telegram_chat_id, "
                    "telegram_name FROM users")
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
            _telegram_into(rec, r)
            out[r["username"]] = rec
        return out


def user_get(username: str) -> dict | None:
    """Одна учётка по имени — вместо чтения всей таблицы users."""
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("SELECT username, pw, phone, created_at, is_admin, team, "
                    "invite_code, invite_code_at, is_super, telegram_chat_id, "
                    "telegram_name FROM users "
                    "WHERE username=%s", (username,))
        r = cur.fetchone()
        if not r:
            return None
        rec = {"pw": r["pw"], "phone": r.get("phone") or "",
               "created_at": r.get("created_at"),
               "is_admin": bool(r.get("is_admin"))}
        if r.get("team"):
            rec["team"] = r["team"]
        if r.get("invite_code"):
            rec["invite_code"] = r["invite_code"]
        if r.get("invite_code_at") is not None:
            rec["invite_code_at"] = r["invite_code_at"]
        if r.get("is_super"):
            rec["is_super"] = True
        _telegram_into(rec, r)
        return rec


def users_save(users: dict) -> None:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("SELECT username FROM users")
        existing = {r["username"] for r in cur.fetchall()}
        for name in existing - set(users):
            cur.execute("DELETE FROM users WHERE username=%s", (name,))
        for name, rec in users.items():
            cur.execute(
                """INSERT INTO users (username, pw, phone, created_at, is_admin,
                                      team, invite_code, invite_code_at, is_super,
                                      telegram_chat_id, telegram_name)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (username) DO UPDATE SET
                     pw=EXCLUDED.pw, phone=EXCLUDED.phone,
                     created_at=EXCLUDED.created_at, is_admin=EXCLUDED.is_admin,
                     team=EXCLUDED.team, invite_code=EXCLUDED.invite_code,
                     invite_code_at=EXCLUDED.invite_code_at,
                     is_super=EXCLUDED.is_super,
                     telegram_chat_id=EXCLUDED.telegram_chat_id,
                     telegram_name=EXCLUDED.telegram_name""",
                (name, rec.get("pw", ""), rec.get("phone") or None,
                 rec.get("created_at"), bool(rec.get("is_admin")),
                 rec.get("team"), rec.get("invite_code"),
                 rec.get("invite_code_at"), bool(rec.get("is_super")),
                 rec.get("telegram_chat_id") or None,
                 rec.get("telegram_name") or None))


# --------------------------------------------------------------------------- #
# sessions  {token_hash: {user, exp}}
# --------------------------------------------------------------------------- #


def session_get(token_hash: str) -> dict | None:
    """Одна сессия по ключу. Раньше ради этого на КАЖДОМ запросе читалась вся
    таблица сессий."""
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("SELECT username, exp FROM sessions WHERE token_hash=%s",
                    (token_hash,))
        r = cur.fetchone()
        return {"user": r["username"], "exp": r["exp"]} if r else None


def session_put(token_hash: str, username: str, exp: float) -> None:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute(
            """INSERT INTO sessions (token_hash, username, exp) VALUES (%s,%s,%s)
               ON CONFLICT (token_hash) DO UPDATE SET
                 username=EXCLUDED.username, exp=EXCLUDED.exp""",
            (token_hash, username, exp))


def session_delete(token_hash: str) -> None:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("DELETE FROM sessions WHERE token_hash=%s", (token_hash,))


def sessions_delete_user(username: str) -> None:
    """Погасить все сессии пользователя одним запросом."""
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("DELETE FROM sessions WHERE username=%s", (username,))


def sessions_prune(now: float) -> None:
    """Убрать протухшие. Раньше это делалось перезаписью всей таблицы."""
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("DELETE FROM sessions WHERE exp < %s", (now,))




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


def job_upsert(d: dict) -> None:
    """Сохранить ОДНУ задачу. Раньше каждое изменение статуса или процента
    переписывало всю таблицу задач целиком."""
    placeholders = ", ".join(["%s"] * len(JOB_COLS))
    updates = ", ".join(f'"{c}"=EXCLUDED."{c}"' for c in JOB_COLS if c != "id")
    sql = (f"INSERT INTO jobs ({_JOB_COLS_Q}) VALUES ({placeholders}) "
           f"ON CONFLICT (id) DO UPDATE SET {updates}")
    vals = [_json(d.get(c)) if c in JOB_JSON_COLS else d.get(c) for c in JOB_COLS]
    with _conn() as conn, _cur(conn) as cur:
        cur.execute(sql, vals)


def job_delete(job_id: str) -> None:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("DELETE FROM jobs WHERE id=%s", (job_id,))


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
        cur.execute("SELECT provider, idx, enc_key, enc_extra, added_at "
                    "FROM user_creds WHERE username=%s ORDER BY provider, idx",
                    (user,))
        out: dict[str, list] = {}
        for r in cur.fetchall():
            out.setdefault(r["provider"], []).append(
                {"key": r.get("enc_key") or "", "extra": r.get("enc_extra") or "",
                 "at": float(r.get("added_at") or 0)})
        return out


def creds_save(user: str, raw: dict) -> None:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("DELETE FROM user_creds WHERE username=%s", (user,))
        for provider, entries in raw.items():
            for i, e in enumerate(entries or []):
                cur.execute(
                    "INSERT INTO user_creds (username, provider, idx, enc_key, "
                    "enc_extra, added_at) VALUES (%s,%s,%s,%s,%s,%s)",
                    (user, provider, i, e.get("key") or "", e.get("extra") or "",
                     float(e.get("at") or 0) or None))


# --------------------------------------------------------------------------- #
# meetings  (per user: {meeting_key: {state, detail, job_id, cloud_url,
#            do_protocol, saved_at}})
# --------------------------------------------------------------------------- #
def meetings_load(user: str) -> dict:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("SELECT meeting_key, state, detail, job_id, cloud_url, "
                    "out_path, live_notes, do_protocol, saved_at "
                    "FROM meetings WHERE username=%s",
                    (user,))
        return {r["meeting_key"]: {"state": r.get("state"), "detail": r.get("detail"),
                                   "job_id": r.get("job_id"), "cloud_url": r.get("cloud_url"),
                                   "out_path": r.get("out_path"),
                                   "live_notes": r.get("live_notes"),
                                   "do_protocol": bool(r.get("do_protocol")),
                                   "saved_at": r.get("saved_at")}
                for r in cur.fetchall()}




def meeting_upsert(user: str, key: str, snap: dict) -> None:
    """Сохранить ОДИН снапшот встречи. Раньше на его месте была запись всех
    встреч команды разом, удалявшая ключи, которых нет в переданном словаре: два
    потока, сохранявшие разные встречи, стирали работу друг друга."""
    with _conn() as conn, _cur(conn) as cur:
        cur.execute(
            """INSERT INTO meetings
                 (username, meeting_key, state, detail, job_id, cloud_url,
                  out_path, live_notes, do_protocol, saved_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (username, meeting_key) DO UPDATE SET
                 state=EXCLUDED.state, detail=EXCLUDED.detail,
                 job_id=EXCLUDED.job_id, cloud_url=EXCLUDED.cloud_url,
                 out_path=EXCLUDED.out_path,
                 live_notes=EXCLUDED.live_notes,
                 do_protocol=EXCLUDED.do_protocol, saved_at=EXCLUDED.saved_at""",
            (user, key, snap.get("state"), snap.get("detail"), snap.get("job_id"),
             snap.get("cloud_url"), snap.get("out_path"), snap.get("live_notes"),
             bool(snap.get("do_protocol")), snap.get("saved_at")))


def meetings_trim(user: str, keep: int = 200) -> None:
    """Оставить только `keep` последних снапшотов команды. В файловом режиме
    обрезка делается при каждой записи; на Postgres строка пишется точечно, так
    что чистку зовём отдельно — при завершении встречи."""
    with _conn() as conn, _cur(conn) as cur:
        cur.execute(
            """DELETE FROM meetings WHERE username=%s AND meeting_key NOT IN (
                   SELECT meeting_key FROM meetings WHERE username=%s
                   ORDER BY saved_at DESC NULLS LAST LIMIT %s)""",
            (user, user, keep))


def meeting_delete(user: str, key: str) -> None:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("DELETE FROM meetings WHERE username=%s AND meeting_key=%s",
                    (user, key))


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
# meeting_series  {series_key: {title, context, preset, priority, last, history}}
# — память серий встреч команды одним JSON-документом (см. app/meeting_series.py)
# --------------------------------------------------------------------------- #
def series_load(user: str) -> dict | None:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("SELECT data FROM meeting_series WHERE username=%s", (user,))
        row = cur.fetchone()
        return row["data"] if row else None


def series_save(user: str, data: dict) -> None:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute(
            """INSERT INTO meeting_series (username, data) VALUES (%s,%s)
               ON CONFLICT (username) DO UPDATE SET data=EXCLUDED.data""",
            (user, _json(data)))


# --------------------------------------------------------------------------- #
# Account deletion — wipe every per-user row (users/sessions/recovery are
# handled by security's own save paths; here we clear the rest in one txn).
# --------------------------------------------------------------------------- #
def delete_user_data(user: str) -> None:
    with _conn() as conn, _cur(conn) as cur:
        for table in ("user_settings", "user_creds", "meetings",
                      "ai_context_projects", "ai_context", "search_docs",
                      "meeting_series"):
            cur.execute(f"DELETE FROM {table} WHERE username=%s", (user,))
        cur.execute("DELETE FROM meeting_stats WHERE team=%s", (user,))
        # И68: полное удаление по требованию — журнал событий тоже данные команды.
        cur.execute("DELETE FROM product_events WHERE team=%s", (user,))
        # Память о встречах переживает ретеншн, но не удаление команды.
        cur.execute("DELETE FROM meeting_chunks WHERE team=%s", (user,))


# --------------------------------------------------------------------------- #
# meeting_chunks — куски встреч для вопросов по всем встречам (meeting_index)
# --------------------------------------------------------------------------- #
_CHUNK_COLS = ["id", "doc_id", "team", "series_key", "title", "meeting_date",
               "meeting_ts", "kind", "idx", "ts", "text", "is_draft", "source",
               "created_at"]


def chunks_replace(doc_id: str, rows: list[dict]) -> int:
    """Заменить куски документа: прежние удаляются, новые пишутся одной
    транзакцией. Пустой список — просто удаление."""
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("DELETE FROM meeting_chunks WHERE doc_id=%s", (doc_id,))
        if rows:
            cols = ", ".join(_CHUNK_COLS)
            ph = ", ".join(["%s"] * len(_CHUNK_COLS))
            cur.executemany(
                f"INSERT INTO meeting_chunks ({cols}) VALUES ({ph})",
                [[r.get(c) for c in _CHUNK_COLS] for r in rows])
        return len(rows)


def chunks_delete_doc(doc_id: str) -> None:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("DELETE FROM meeting_chunks WHERE doc_id=%s", (doc_id,))


def chunks_docs(team: str) -> list[dict]:
    """Документы команды в индексе: [{doc_id, series_key, meeting_date,
    title, source, is_draft, chunks}] — для дедупа в индексаторе и сводки."""
    with _conn() as conn, _cur(conn) as cur:
        cur.execute(
            """SELECT doc_id, min(series_key) AS series_key,
                      min(meeting_date) AS meeting_date, min(title) AS title,
                      min(source) AS source, bool_or(is_draft) AS is_draft,
                      count(*) AS chunks, max(meeting_ts) AS meeting_ts
               FROM meeting_chunks WHERE team=%s
               GROUP BY doc_id ORDER BY max(meeting_ts) DESC NULLS LAST""",
            (team,))
        return [dict(r) for r in cur.fetchall()]


def chunks_search(team: str, q: str, limit: int = 24,
                  series_key: str | None = None,
                  kinds: tuple[str, ...] | None = None) -> list[dict]:
    """Полнотекстовый поиск по кускам команды: websearch-синтаксис, русская
    морфология, ранжирование ts_rank_cd. Фильтр по команде ОБЯЗАТЕЛЕН."""
    sql = """SELECT id, doc_id, series_key, title, meeting_date, meeting_ts,
                    kind, idx, ts, text, is_draft, source,
                    ts_rank_cd(to_tsvector('russian', text),
                               websearch_to_tsquery('russian', %s)) AS rank
             FROM meeting_chunks
             WHERE team=%s
               AND to_tsvector('russian', text) @@ websearch_to_tsquery('russian', %s)"""
    params: list = [q, team, q]
    if series_key:
        sql += " AND series_key=%s"
        params.append(series_key)
    if kinds:
        sql += " AND kind = ANY(%s)"
        params.append(list(kinds))
    sql += " ORDER BY rank DESC, meeting_ts DESC NULLS LAST, idx LIMIT %s"
    params.append(int(limit))
    with _conn() as conn, _cur(conn) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def chunks_series_recent(team: str, series_key: str, limit: int = 3,
                         kind: str = "protocol",
                         exclude_doc: str | None = None,
                         before_ts: float | None = None) -> list[dict]:
    """Куски протоколов ПОСЛЕДНИХ встреч серии (не старше `before_ts`, без
    `exclude_doc`), по документам в порядке «новые сверху», куски по idx."""
    sql = """SELECT doc_id, max(meeting_ts) AS meeting_ts
             FROM meeting_chunks
             WHERE team=%s AND series_key=%s AND kind=%s"""
    params: list = [team, series_key, kind]
    if exclude_doc:
        sql += " AND doc_id<>%s"
        params.append(exclude_doc)
    if before_ts is not None:
        sql += " AND meeting_ts < %s"
        params.append(float(before_ts))
    sql += " GROUP BY doc_id ORDER BY max(meeting_ts) DESC NULLS LAST LIMIT %s"
    params.append(int(limit))
    with _conn() as conn, _cur(conn) as cur:
        cur.execute(sql, params)
        docs = [r["doc_id"] for r in cur.fetchall()]
        if not docs:
            return []
        cur.execute(
            """SELECT doc_id, title, meeting_date, meeting_ts, kind, idx, ts,
                      text, is_draft
               FROM meeting_chunks
               WHERE team=%s AND kind=%s AND doc_id = ANY(%s)
               ORDER BY meeting_ts DESC NULLS LAST, doc_id, idx""",
            (team, kind, docs))
        return [dict(r) for r in cur.fetchall()]


def chunks_count(team: str) -> dict:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute(
            """SELECT count(DISTINCT doc_id) AS docs, count(*) AS chunks,
                      min(meeting_ts) AS first_ts, max(meeting_ts) AS last_ts
               FROM meeting_chunks WHERE team=%s""", (team,))
        r = cur.fetchone() or {}
        return {"docs": int(r.get("docs") or 0), "chunks": int(r.get("chunks") or 0),
                "first_ts": r.get("first_ts"), "last_ts": r.get("last_ts")}


# --------------------------------------------------------------------------- #
# meeting_chunks.embedding — векторы кусков (pgvector), фаза 1 памяти встреч
# --------------------------------------------------------------------------- #
# ⚠️ Векторная часть НАРОЧНО не в _SCHEMA: общая схема выполняется одним
# cur.execute при открытии пула, и на образе без pgvector (postgres:16-alpine)
# «CREATE EXTENSION vector» уронил бы ВСЮ схему — то есть приложение целиком.
# Здесь же неудача означает только «эмбеддинги выключены»: предупреждение
# один раз на процесс, память встреч продолжает работать полнотекстом.
_vector_state = {"tried": False, "ready": False, "dim": 0}
_vector_lock = threading.Lock()


def vector_ready(dim: int) -> bool:
    """Расширение, колонка `embedding vector(dim)` и индекс hnsw — один раз
    на процесс. False — pgvector недоступен, база выключена или колонка уже
    создана с ДРУГОЙ размерностью (сменили провайдера или VTX_EMBED_DIM:
    тогда колонку надо снести и пересчитать — `scripts/embed_backfill.py`)."""
    dim = int(dim or 0)
    if not enabled() or dim <= 0:
        return False
    with _vector_lock:
        if _vector_state["tried"]:
            return _vector_state["ready"] and _vector_state["dim"] == dim
        _vector_state["tried"] = True
        _vector_state["dim"] = dim
        from . import logs
        log = logs.get("vtx.db")
        try:
            with _conn() as conn, _cur(conn) as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cur.execute("ALTER TABLE meeting_chunks "
                            f"ADD COLUMN IF NOT EXISTS embedding vector({dim})")
                # typmod у типа vector — это и есть размерность колонки.
                cur.execute(
                    """SELECT atttypmod AS dim FROM pg_attribute
                       WHERE attrelid = 'meeting_chunks'::regclass
                         AND attname = 'embedding' AND NOT attisdropped""")
                r = cur.fetchone() or {}
                have = int(r.get("dim") or 0)
                if have > 0 and have != dim:
                    log.warning("Колонка meeting_chunks.embedding создана на %d "
                                "измерений, а провайдер эмбеддингов даёт %d — "
                                "векторный поиск выключен. Снести колонку "
                                "(ALTER TABLE meeting_chunks DROP COLUMN embedding) "
                                "и пересчитать: scripts/embed_backfill.py", have, dim)
                    return False
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_meeting_chunks_embedding "
                    "ON meeting_chunks USING hnsw (embedding vector_cosine_ops)")
            _vector_state["ready"] = True
        except Exception:  # noqa: BLE001 — нет pgvector/прав: только выключить
            log.warning("pgvector недоступен — эмбеддинги памяти встреч выключены, "
                        "поиск идёт полнотекстом (образ базы должен быть "
                        "pgvector/pgvector:pg16)", exc_info=True)
            _vector_state["ready"] = False
        return _vector_state["ready"]


def _vec_literal(vec) -> str:
    """Вектор строкой «[0.1,0.2,…]» — так pgvector принимает параметр через
    `%s::vector`, без адаптера типов в драйвере."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def chunks_set_embeddings(pairs: list[tuple[str, list]]) -> int:
    """Записать векторы кускам по id. Пары с пустым вектором пропускаются."""
    rows = [(_vec_literal(v), i) for i, v in pairs if v]
    if not rows:
        return 0
    with _conn() as conn, _cur(conn) as cur:
        cur.executemany("UPDATE meeting_chunks SET embedding=%s::vector WHERE id=%s",
                        rows)
    return len(rows)


def chunks_without_embedding(team: str, limit: int = 200) -> list[dict]:
    """Куски команды без вектора — свежие встречи первыми: [{id, text}]."""
    with _conn() as conn, _cur(conn) as cur:
        cur.execute(
            """SELECT id, text FROM meeting_chunks
               WHERE team=%s AND embedding IS NULL
               ORDER BY meeting_ts DESC NULLS LAST, doc_id, idx LIMIT %s""",
            (team, int(limit)))
        return [dict(r) for r in cur.fetchall()]


def chunks_embedding_stats(team: str) -> dict:
    """Сколько кусков команды с вектором и без (для плана досчёта)."""
    with _conn() as conn, _cur(conn) as cur:
        cur.execute(
            """SELECT count(*) AS chunks,
                      count(embedding) AS embedded
               FROM meeting_chunks WHERE team=%s""", (team,))
        r = cur.fetchone() or {}
        return {"chunks": int(r.get("chunks") or 0),
                "embedded": int(r.get("embedded") or 0)}


def chunks_vector_search(team: str, vec: list, limit: int = 24,
                         kinds: tuple[str, ...] | None = None) -> list[dict]:
    """Ближайшие по косинусу куски команды: строки как у chunks_search плюс
    `dist` (0 — совпадение). Фильтр по команде ОБЯЗАТЕЛЕН; куски без
    вектора (ещё не досчитанные) не участвуют. ORDER BY повторяет выражение
    из SELECT, а не псевдоним: по нему планировщик берёт индекс hnsw."""
    lit = _vec_literal(vec)
    sql = """SELECT id, doc_id, series_key, title, meeting_date, meeting_ts,
                    kind, idx, ts, text, is_draft, source,
                    embedding <=> %s::vector AS dist
             FROM meeting_chunks
             WHERE team=%s AND embedding IS NOT NULL"""
    params: list = [lit, team]
    if kinds:
        sql += " AND kind = ANY(%s)"
        params.append(list(kinds))
    sql += " ORDER BY embedding <=> %s::vector LIMIT %s"
    params += [lit, int(limit)]
    with _conn() as conn, _cur(conn) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# search_docs — full-text index over the team's transcripts + protocols (Д14)
# --------------------------------------------------------------------------- #
_EVENT_COLS = ["id", "team", "kind", "job_id", "actor", "source", "at", "extra"]


def event_add(row: dict) -> bool:
    """Записать событие. False — такое уже было (дедуп по первичному ключу).

    ⚠️ `DO NOTHING`, а не `DO UPDATE`: на первом событии стоит «время до
    первого открытия» (И11), и перезапись сдвигала бы его на последнее.
    """
    with _conn() as conn, _cur(conn) as cur:
        cols = ", ".join(_EVENT_COLS)
        ph = ", ".join(["%s"] * len(_EVENT_COLS))
        cur.execute(f"INSERT INTO product_events ({cols}) VALUES ({ph}) "
                    "ON CONFLICT (id) DO NOTHING",
                    [_json(row.get(c)) if c == "extra" else row.get(c)
                     for c in _EVENT_COLS])
        return bool(cur.rowcount)


def events_add_many(rows: list[dict]) -> int:
    """Пачка событий одним запросом: вызовов модели на встрече — десятки."""
    if not rows:
        return 0
    with _conn() as conn, _cur(conn) as cur:
        cols = ", ".join(_EVENT_COLS)
        ph = ", ".join(["%s"] * len(_EVENT_COLS))
        cur.executemany(
            f"INSERT INTO product_events ({cols}) VALUES ({ph}) "
            "ON CONFLICT (id) DO NOTHING",
            [[_json(r.get(c)) if c == "extra" else r.get(c)
              for c in _EVENT_COLS] for r in rows])
        return len(rows)


def events_load(team: str, since: float) -> list[dict]:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute(f"SELECT {', '.join(_EVENT_COLS)} FROM product_events "
                    "WHERE team=%s AND at >= %s ORDER BY at", (team, since))
        return [{c: r.get(c) for c in _EVENT_COLS} for r in cur.fetchall()]


def events_delete(team: str) -> None:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("DELETE FROM product_events WHERE team=%s", (team,))


def search_save(job_id: str, team: str, title: str, body: str,
                created_at: float) -> None:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute(
            """INSERT INTO search_docs (job_id, username, title, body, created_at)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (job_id) DO UPDATE SET
                 title=EXCLUDED.title, body=EXCLUDED.body""",
            (job_id, team, title, body, created_at))


def search_delete(job_id: str) -> None:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("DELETE FROM search_docs WHERE job_id=%s", (job_id,))


def search_ids(team: str) -> set[str]:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute("SELECT job_id FROM search_docs WHERE username=%s", (team,))
        return {r["job_id"] for r in cur.fetchall()}


def search_query(team: str, q: str, limit: int = 20) -> list[dict]:
    """websearch syntax («теги -старое», фразы в кавычках), russian stemming,
    highlighted snippet via ts_headline."""
    with _conn() as conn, _cur(conn) as cur:
        cur.execute(
            """SELECT job_id, title, created_at,
                      ts_headline('russian', body,
                                  websearch_to_tsquery('russian', %s),
                                  'MaxWords=20, MinWords=8, MaxFragments=1,
                                   StartSel=<<, StopSel=>>') AS snippet
               FROM search_docs
               WHERE username=%s
                 AND to_tsvector('russian', coalesce(title,'') || ' ' || coalesce(body,''))
                     @@ websearch_to_tsquery('russian', %s)
               ORDER BY created_at DESC LIMIT %s""",
            (q, team, q, limit))
        return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# meeting_stats — outcome of each finished meeting (for the Overview metrics)
# --------------------------------------------------------------------------- #
_STAT_COLS = ["id", "team", "at", "title", "duration_sec", "speakers",
              "tasks", "decisions", "participants", "has_protocol", "ok",
              "tokens_in", "tokens_cached", "tokens_out", "llm_calls", "usd",
              "engines",
              "status", "protocol_ok", "engine", "fallback", "preset",
              "transcribe_sec", "verify_checked", "verify_confirmed",
              "verify_mode", "verify_version", "topics", "empty_topics",
              "dropped_topics", "dropped_items", "summary_is_toc", "edited",
              "tasks_with_owner", "tokens_by_model",
              "kind", "stop_reason", "upload_error", "detail",
              "join_delay_sec", "silent", "unanswered", "carried_stale", "job_id"]
# Колонки, которые в файловом режиме и в Postgres лежат как JSON.
_STAT_JSON_COLS = {"tokens_by_model"}


def stats_add(row: dict) -> None:
    with _conn() as conn, _cur(conn) as cur:
        cols = ", ".join(_STAT_COLS)
        ph = ", ".join(["%s"] * len(_STAT_COLS))
        upd = ", ".join(f"{c}=EXCLUDED.{c}" for c in _STAT_COLS if c != "id")
        cur.execute(f"INSERT INTO meeting_stats ({cols}) VALUES ({ph}) "
                    f"ON CONFLICT (id) DO UPDATE SET {upd}",
                    [_json(row.get(c)) if c in _STAT_JSON_COLS else row.get(c)
                     for c in _STAT_COLS])


def stats_load(team: str, since: float) -> list[dict]:
    with _conn() as conn, _cur(conn) as cur:
        cur.execute(f"SELECT {', '.join(_STAT_COLS)} FROM meeting_stats "
                    "WHERE team=%s AND at >= %s ORDER BY at", (team, since))
        return [{c: r.get(c) for c in _STAT_COLS} for r in cur.fetchall()]
