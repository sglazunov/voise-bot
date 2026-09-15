"""Продуктовые события: что сделал ЧЕЛОВЕК (docs/ТЗ-МЕТРИКИ.md §4).

Зачем отдельный журнал. Производство протоколов — это ПРЕДЛОЖЕНИЕ: бот ходит
на встречи сам по расписанию, и «сколько встреч обработано» растёт от календаря
клиента, а не от полезности продукта. Ценность измеряется потреблением, а
потребление до сих пор не фиксировалось нигде: единственным следом действия
человека была строка uvicorn в stdout контейнера — без пользователя и без
исхода. Поэтому §5 ТЗ («метрики ценности») не существовал вовсе.

⚠️ **Событие ставит человек, а не опрос из SPA и не фоновая задача** (И7).
`source` обязателен и разделяет `human` / `system` / `bot`: без него
автоматические действия неотличимы от человеческих, и метрика ценности
превращается в метрику трафика. Записывать событие из обработчика, который
дёргается опросом (`GET /api/jobs`, `/partial`, `status`), НЕЛЬЗЯ.

⚠️ **Приватность** (И60—И65). В событии нет ни строчки разговора и ни одного
логина: человек обозначен псевдонимом (`security.pseudonym`, считается от
мастер-ключа сервера и от команды), содержимое — только счётчики и флаги.
Поимённое «кто прочитал» наружу не выходит: сводка отдаёт агрегаты.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from . import db, logs, security

_LOG = logs.get("vtx.events")

# --- виды событий ---------------------------------------------------------- #
OPENED = "protocol_opened"          # человек открыл протокол встречи
EXPORTED = "protocol_exported"      # скачал Word
EDITED = "protocol_edited"          # правил протокол руками
ASKED = "question_asked"            # задал вопрос по встрече
REGEN = "topic_regenerated"         # перегенерировал тему
REANALYZED = "protocol_reanalyzed"  # пересобрал протокол
TASK_CREATED = "task_created"       # отправил задачу в трекер
# Служебные (source=system): не ценность, а экономика и надёжность.
LLM_CALL = "llm_call"               # один вызов модели: стадия, токены, цена
BOT_JOINED = "bot_joined"           # бот вошёл на встречу и с какой задержкой

# И12: «протокол породил действие». Открытие сюда НЕ входит — прочитали ≠
# пригодилось, в этом весь смысл метрики.
ACTIONS = frozenset({EXPORTED, EDITED, ASKED, REGEN, REANALYZED, TASK_CREATED})

# --- источники ------------------------------------------------------------- #
HUMAN = "human"
SYSTEM = "system"
BOT = "bot"

_MAX_ROWS = 20000     # файловый режим: журнал не должен расти бесконечно


def _path(team: str):
    return security.user_dir(team) / "events.json"


def _file_load(team: str) -> list[dict]:
    try:
        return json.loads(_path(team).read_text(encoding="utf-8")) or []
    except (OSError, ValueError):
        return []


def _file_save(team: str, rows: list[dict]) -> None:
    p = _path(team)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows[-_MAX_ROWS:], ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _day(at: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(at))


def record(kind: str, *, user: str, job_id: str = "", source: str = HUMAN,
           at: float | None = None, extra: dict | None = None,
           once_a_day: bool = True) -> bool:
    """Записать событие. Возвращает True, если строка появилась.

    `once_a_day` — дедупликация по «встреча + человек + сутки» (И7). Открытие
    протокола без неё считало бы не читателей, а нажатия: человек возвращается
    к документу по три раза за совещание, и «доля прочитанных» превратилась бы
    в метрику кликов. Первое событие остаётся неизменным — на нём стоит
    «время до первого открытия» (И11).
    """
    try:
        team = security.team_of(user or "")
        if not team:
            return False
        at = float(at if at is not None else time.time())
        actor = security.pseudonym(user, team)
        eid = (f"{kind}:{job_id}:{actor}:{_day(at)}" if once_a_day
               else f"{kind}:{job_id}:{actor}:{uuid.uuid4().hex[:8]}")
        row = {"id": eid, "team": team, "kind": kind, "job_id": job_id or "",
               "actor": actor, "source": source, "at": at,
               "extra": extra or None}
        if db.enabled():
            return db.event_add(row)
        rows = _file_load(team)
        if any(r.get("id") == eid for r in rows):
            return False
        rows.append(row)
        _file_save(team, rows)
        return True
    except Exception:   # noqa: BLE001 — аналитика не должна ронять запрос
        _LOG.warning("Событие %s (job %s) не записано", kind, job_id,
                     exc_info=True)
        return False


def record_many(rows: list[dict], *, user: str, job_id: str = "",
                kind: str = LLM_CALL, source: str = SYSTEM) -> int:
    """Записать пачку однотипных служебных событий ОДНОЙ операцией.

    Вызовов модели на часовой встрече — десятки. Писать их по одному значило бы
    десятки перезаписей файла журнала (и столько же запросов к базе) внутри
    задачи, которая и так считает протокол.
    """
    if not rows:
        return 0
    try:
        team = security.team_of(user or "")
        if not team:
            return 0
        actor = security.pseudonym(user, team)
        out = []
        for i, r in enumerate(rows):
            at = float(r.get("at") or time.time())
            out.append({"id": f"{kind}:{job_id}:{uuid.uuid4().hex[:12]}",
                        "team": team, "kind": kind, "job_id": job_id or "",
                        # ⚠️ У служебного события actor — не «кто читал», а «чья
                        # команда потратила». В метрики ценности такие события
                        # не входят: их отсекает `human_rows`.
                        "actor": actor, "source": source, "at": at,
                        "extra": {k: v for k, v in r.items() if k != "at"}})
        if db.enabled():
            return db.events_add_many(out)
        rows_all = _file_load(team)
        rows_all.extend(out)
        _file_save(team, rows_all)
        return len(out)
    except Exception:   # noqa: BLE001
        _LOG.warning("Пачка событий %s (job %s) не записана", kind, job_id,
                     exc_info=True)
        return 0


def load(team: str, since: float) -> list[dict]:
    if db.enabled():
        return db.events_load(team, since)
    return [r for r in _file_load(team) if (r.get("at") or 0) >= since]


def human_rows(rows: list[dict]) -> list[dict]:
    """Только действия человека. Служебные и ботовы события в метрики ценности
    не входят — иначе они измеряют объём работы сервиса, а не пользу."""
    return [r for r in rows if (r.get("source") or HUMAN) == HUMAN]


def by_job(rows: list[dict], kinds: Any = None) -> dict[str, list[dict]]:
    """Сгруппировать события по встрече (job_id), необязательно фильтруя по виду."""
    out: dict[str, list[dict]] = {}
    for r in rows:
        if kinds is not None and r.get("kind") not in kinds:
            continue
        jid = r.get("job_id") or ""
        if jid:
            out.setdefault(jid, []).append(r)
    return out
