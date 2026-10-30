"""Smoke-набор на ЖИВОМ Postgres (сводное ревью 30.10, P1 CI).

Весь остальной набор идёт в файловом режиме: `conftest.py` нарочно убирает
`DATABASE_URL`, чтобы прогон в боевом контейнере не стёр живые данные. Из-за
этого путь Postgres не проверялся ничем, кроме статического разбора текста
схемы (`test_db_schema.py`), — а на проде работает именно он.

Адрес базы — `VTX_TEST_DATABASE_URL` (не `DATABASE_URL`: его conftest гасит
по-прежнему). Без него набор пропускается, но в GitHub Actions пропуск —
ошибка: иначе опечатка в имени переменной молча выключила бы проверку.

Всё делается в отдельной схеме `vtx_smoke`, которую набор сам пересоздаёт:
`jobs_save` удаляет строки, которых нет в переданном списке, — на чужой
dev-базе это стёрло бы все задачи.
"""
import os
from dataclasses import asdict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pytest

from app import config, db
from app.jobs import Job, store

_URL = os.getenv("VTX_TEST_DATABASE_URL", "").strip()
_SCHEMA_NAME = "vtx_smoke"

if not _URL:
    if os.getenv("GITHUB_ACTIONS") == "true":
        raise RuntimeError("VTX_TEST_DATABASE_URL не задан: в CI smoke-набор "
                           "Postgres обязан выполняться, а не пропускаться")
    pytest.skip("VTX_TEST_DATABASE_URL не задан — живой Postgres не проверяем",
                allow_module_level=True)

# Не importorskip: адрес задан — значит, проверку ждут, и без драйвера это
# ошибка окружения, а не повод тихо пропустить набор.
import psycopg  # noqa: E402


def _with_search_path(url: str, schema: str) -> str:
    """Тот же адрес, но с search_path на свою схему: `_SCHEMA` пишет имена
    таблиц без схемы, и они создаются в первой из search_path."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "options"]
    query.append(("options", f"-csearch_path={schema}"))
    return urlunsplit(parts._replace(query=urlencode(query)))


@pytest.fixture
def pg(monkeypatch):
    """Чистая схема + пул db на неё; после теста пул закрывается."""
    with psycopg.connect(_URL, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA_NAME} CASCADE")
        conn.execute(f"CREATE SCHEMA {_SCHEMA_NAME}")
    monkeypatch.setattr(config, "DATABASE_URL", _with_search_path(_URL, _SCHEMA_NAME))
    monkeypatch.setattr(db, "_pool", None)
    assert db.enabled()
    yield
    if db._pool is not None:
        db._pool.close()


def _reopen_pool() -> None:
    """Как перезапуск приложения: новый пул, схема применяется ещё раз."""
    db._pool.close()
    db._pool = None
    db.init_schema()


def _columns(table: str) -> set[str]:
    with db._conn() as conn, db._cur(conn) as cur:
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name=%s", (_SCHEMA_NAME, table))
        return {r["column_name"] for r in cur.fetchall()}


def _job(job_id: str, owner: str = "alice", **kw) -> Job:
    return Job(id=job_id, filename=f"{job_id}.mp3", audio_path=f"/tmp/{job_id}.mp3",
               language="ru", diarize=False, owner=owner, **kw)


def _rows() -> dict[str, dict]:
    return {d["id"]: d for d in db.jobs_load()}


def test_схема_разворачивается_на_чистой_базе_и_повторно(pg):
    db.init_schema()
    _reopen_pool()   # второй прогон _SCHEMA — как каждый старт контейнера
    for table in ("users", "sessions", "jobs", "meetings", "user_settings",
                  "user_creds", "meeting_stats"):
        assert _columns(table), f"таблица {table} не создалась"
    missing = set(db.JOB_COLS) - _columns("jobs")
    assert not missing, f"колонок задачи нет в живой таблице jobs: {missing}"
    missing = set(db._STAT_COLS) - _columns("meeting_stats")
    assert not missing, f"колонок метрик нет в живой таблице: {missing}"


def test_job_upsert_вставляет_и_обновляет_одну_строку(pg):
    job = _job("j1", analysis={"summary": "итоги"}, docx_providers=["gigachat"],
               video_participants=[{"name": "Анна"}])
    db.job_upsert(asdict(job))
    got = _rows()["j1"]
    assert got == asdict(job), "задача после записи и чтения из Postgres изменилась"

    job.status, job.progress = "done", 1.0
    job.analysis = {"summary": "новые итоги"}
    db.job_upsert(asdict(job))
    rows = _rows()
    assert list(rows) == ["j1"]
    assert rows["j1"]["status"] == "done"
    assert rows["j1"]["progress"] == 1.0
    assert rows["j1"]["analysis"] == {"summary": "новые итоги"}


def test_jobs_save_удаляет_отсутствующие_и_обновляет_оставшиеся(pg):
    a, b, c = _job("a"), _job("b"), _job("c")
    db.jobs_save([asdict(a), asdict(b), asdict(c)])
    assert set(_rows()) == {"a", "b", "c"}

    c.status = "done"
    db.jobs_save([asdict(a), asdict(c)])
    rows = _rows()
    assert set(rows) == {"a", "c"}, "jobs_save не удалил задачу, которой нет в списке"
    assert rows["c"]["status"] == "done"

    db.jobs_save([])
    assert _rows() == {}


def test_удаление_аккаунта_стирает_задачи_команды_в_базе(pg):
    """P0-2 на живой базе: `delete_owner` → `_save()` без аргумента →
    `jobs_save` удаляет строки владельца; чужие задачи остаются и
    поднимаются после перезапуска."""
    for job in (_job("a1", owner="alice"), _job("a2", owner="alice"),
                _job("b1", owner="bob", status="done")):
        store._jobs[job.id] = job
        store._save(job)
    assert set(_rows()) == {"a1", "a2", "b1"}

    assert store.delete_owner("alice") == 2
    _reopen_pool()
    rows = _rows()
    assert set(rows) == {"b1"}
    assert rows["b1"]["owner"] == "bob"
