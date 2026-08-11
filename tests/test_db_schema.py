"""Схема БД должна разворачиваться на ЧИСТОЙ базе.

Боевой случай: первая установка на новый сервер уходила в бесконечный
перезапуск контейнера (32 рестарта) с ошибкой

    psycopg.errors.UndefinedTable: relation "meetings" does not exist

Причина: `ALTER TABLE meetings ADD COLUMN …` стоял в тексте схемы ВЫШЕ, чем
`CREATE TABLE IF NOT EXISTS meetings`. На базе, где таблица уже существовала
с прошлых версий, всё работало — поэтому дефект и дожил до установки с нуля.

Тест статический: настоящий Postgres в юнит-наборе не поднимается (conftest
специально убирает DATABASE_URL), поэтому проверяем порядок операторов по
тексту схемы. Именно эта проверка ловит исходный баг.
"""
import re

from app import db

_CREATE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)")
# ALTER TABLE <t> …  и  CREATE INDEX … ON <t>
_REF_RE = re.compile(r"(?:ALTER TABLE|CREATE INDEX[^;]*?\sON)\s+(\w+)")


def _first_create_positions() -> dict[str, int]:
    pos: dict[str, int] = {}
    for m in _CREATE_RE.finditer(db._SCHEMA):
        pos.setdefault(m.group(1), m.start())
    return pos


def test_таблицы_создаются_раньше_обращений_к_ним():
    created = _first_create_positions()
    late = []
    for m in _REF_RE.finditer(db._SCHEMA):
        table = m.group(1)
        if table not in created:
            late.append(f"{table}: обращение есть, CREATE TABLE нет")
        elif m.start() < created[table]:
            late.append(f"{table}: {m.group(0)!r} стоит раньше CREATE TABLE")
    assert not late, "на чистой базе схема упадёт — " + "; ".join(late)


def test_схема_не_пустая_и_содержит_ключевые_таблицы():
    """Страховка от того, что регулярка выше молча ничего не нашла."""
    created = _first_create_positions()
    for table in ("jobs", "meetings", "user_settings", "user_creds"):
        assert table in created, f"в схеме нет таблицы {table}"


def test_чистое_время_распознавания_хранится():
    """finished_at-started_at измеряет ВСЮ жизнь задачи: пересборка протокола
    сдвигает конец, начало остаётся от первого прогона. На бою часовая встреча
    после пяти пересборок показала 438 минут «обработки» — по таким числам о
    скорости судить нельзя."""
    from app import db
    from app.jobs import Job
    assert "transcribe_sec" in db.JOB_SCALAR_COLS
    assert hasattr(Job(filename="x", audio_path="y", language="ru",
                       diarize=False), "transcribe_sec")
