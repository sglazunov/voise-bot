"""Business metrics for the Overview page.

Job rows are purged by retention (VTX_RETENTION_HOURS, 24h by default), so the
numbers that matter for the business — how many meetings ran, how many hours,
how many tasks/decisions the AI captured — are recorded HERE once, when a job
finishes, and survive that cleanup. One row per finished meeting, per team.

Everything reported is measured. The single estimate (time saved on writing
minutes) is derived with an explicit, stated coefficient — never presented as a
measurement.
"""
from __future__ import annotations

import json
import time
from typing import Any

from . import db, events, logs, security, usage

_LOG = logs.get("vtx.stats")

# How long writing minutes by hand takes, as a share of the meeting itself
# (listening back + typing). Deliberately conservative; shown in the UI as an
# estimate with the assumption spelled out.
MANUAL_MINUTES_COEFF = float(__import__("os").getenv("VTX_MINUTES_COEFF", "0.5"))
_MAX_ROWS = 5000     # file backend: keep the log bounded


def _env_float(name: str) -> float | None:
    """Стоимостная настройка: не задана — значит, компонент НЕ считается и в
    интерфейсе остаётся прочерк. Выдуманное значение хуже пустого места."""
    raw = (__import__("os").getenv(name) or "").strip()
    try:
        return float(raw) if raw else None
    except ValueError:
        _LOG.warning("%s=%r не число — компонент себестоимости не считается", name, raw)
        return None


# Юнит-экономика (docs/ТЗ-МЕТРИКИ.md §7). Все четыре — настройки владельца
# сервиса, в коде умолчаний нет: стоимость машино-часа зависит от хостинга,
# курс — от дня, ставка поддержки — от человека.
COST_CPU_HOUR_USD = _env_float("VTX_COST_CPU_HOUR_USD")    # машино-час сервера
COST_SLOT_HOUR_USD = _env_float("VTX_COST_SLOT_HOUR_USD")  # час слота записи (браузер+ffmpeg)
USD_RUB = _env_float("VTX_USD_RUB")                        # курс для подписки в рублях
SUPPORT_HOUR_RUB = _env_float("VTX_SUPPORT_HOUR_RUB")      # ставка часа владельца
# Встреча длиннее двух часов — «тяжёлый хвост» (И32): именно они съедают маржу.
LONG_MEETING_SEC = 2 * 3600


def _pct(xs: list[float], q: float) -> float | None:
    """Перцентиль по ближайшему рангу. Средних здесь нет намеренно (И29):
    четырёхчасовые записи уже случались дважды, среднее не описывает никого."""
    if not xs:
        return None
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round(q * (len(xs) - 1)))))
    return xs[k]


# Почему остановилась запись — по-русски, одним словарём на весь проект.
# Коды приходят из рекордера (`recorder.record_meeting` → `reason`), а раньше
# никуда не доезжали: и карточка встречи, и метрика молчали о том, чем запись
# кончилась. См. docs/ТЗ-МЕТРИКИ.md §14.2.
STOP_REASON_RU = {
    "silence": "тишина после разговора",
    "max_duration": "предел длительности",
    "chat_stop": "стоп-слово в чате",
    "call_ended": "встречу завершили для всех",
    "left_call": "бота убрали из звонка",
    "nobody_joined": "никто не пришёл",
    "thinned_out": "все вышли",
    "stopped": "остановлено вручную",
    "error": "сбой во время записи",
}
# Исход встречи у планировщика. Считается ОТДЕЛЬНО от строки задачи
# распознавания: у записанной встречи есть обе, складывать их нельзя.
MEETING_OUTCOMES = ("recorded", "missed", "skipped", "rec_error")
# ⚠️ Порог из ТЗ §9: при знаменателе меньше 20 процент не считать и не
# показывать. При десятке встреч «явка 90 %» — это «одна не состоялась»,
# и читать её как процент вреднее, чем не читать вовсе.
_MIN_DENOM = 20
# И9: «прочитан» — открыт человеком в течение 72 часов после готовности.
# Окно нужно, чтобы метрика мерила свежесть пользы, а не накапливалась вечно:
# протокол, открытый через месяц, — это уже И15 «возврат к архиву».
READ_WINDOW_SEC = float(__import__("os").getenv("VTX_READ_WINDOW_HOURS", "72")) * 3600
# И63: агрегат «читал тот, кого не было на встрече» и прочие разрезы по людям
# показываются только при пяти и более читателях — иначе доля вычисляется
# обратно до конкретного человека.
_MIN_READERS = 5
# Со скольких секунд вход бота считается опозданием — тот же порог, что и в
# планировщике (`VTX_LATE_JOIN_SEC`), чтобы карточка и сводка не расходились.
LATE_JOIN_SEC = float(__import__("os").getenv("VTX_LATE_JOIN_SEC", "120"))


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


# Стадии вызова модели по-русски — для «Обзора» и отчётов.
STAGE_RU = {
    "map": "чтение частей встречи",
    "merge": "уплотнение заметок",
    "reduce": "сборка протокола",
    "verify": "проверка цитат",
    "regen": "перегенерация тем",
    "ask": "вопросы по встрече",
}


def stop_reason_ru(code: str | None) -> str:
    code = (code or "").strip()
    return STOP_REASON_RU.get(code, code or "причина неизвестна")


def _path(team: str):
    return security.user_dir(team) / "meeting_stats.json"


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


def record(job: Any) -> None:
    """Store one finished job's outcome. Best-effort: never breaks the pipeline."""
    try:
        team = security.team_of(getattr(job, "owner", "") or "")
        if not team:
            return
        a = getattr(job, "analysis", None) or {}
        row = {
            "id": job.id,
            "team": team,
            "at": getattr(job, "finished_at", None) or time.time(),
            "title": (getattr(job, "filename", "") or "")[:200],
            "duration_sec": float(getattr(job, "duration", 0) or 0),
            "speakers": int(getattr(job, "speakers", 0) or 0),
            "tasks": len(a.get("tasks") or []) + len(a.get("minor_tasks") or []),
            "decisions": len(a.get("decisions") or []),
            "participants": len(a.get("participants") or []),
            "has_protocol": bool(a),
            "ok": getattr(job, "status", "") != "error",
            # Вид строки. У записанной встречи их ДВЕ — исход встречи
            # (kind="meeting") и эта, по задаче распознавания. Складывать их
            # нельзя, поэтому сводка разделяет их по этому полю; у строк,
            # записанных до появления колонки, оно пустое и значит "job".
            "kind": "job",
        }
        # Расход модели. В строке задачи он тоже есть, но строку через сутки
        # стирает ретеншн, а «сколько команда потратила за месяц» нужно
        # спрашивать спустя месяцы.
        u = getattr(job, "llm_usage", None) or {}
        by_model = u.get("by_model") or {}
        row.update({
            "tokens_in": int(u.get("in") or 0),
            "tokens_cached": int(u.get("cached") or 0),
            "tokens_out": int(u.get("out") or 0),
            "llm_calls": int(u.get("calls") or 0),
            "usd": usage.cost_usd(by_model),
            "engines": ", ".join(sorted(by_model)) or None,
            # Расход по КАЖДОЙ модели с числами. Раньше сюда уходила только
            # строка имён, и вопрос «сколько стоит один движок против другого
            # на наших встречах» оставался без ответа при наличии данных.
            "tokens_by_model": by_model or None,
        })
        row.update(_quality_row(job, a))
        if db.enabled():
            db.stats_add(row)
        else:
            rows = [r for r in _file_load(team) if r.get("id") != row["id"]]
            rows.append(row)
            _file_save(team, rows)
    except Exception:
        # Метрика не должна ронять конвейер, но и молчать ей нельзя: молча
        # потерянная строка выглядит как «встречи не было».
        _LOG.warning("Метрика по задаче %s не записана",
                     getattr(job, "id", "?"), exc_info=True)


def record_meeting(st: Any, outcome: str) -> None:
    """Сохранить ИСХОД ВСТРЕЧИ у планировщика (docs/ТЗ-МЕТРИКИ.md §14.2).

    До этого метрика знала только те встречи, по которым создалась задача
    распознавания. Встреча, на которую бот не пришёл, которую отсеял фильтр или
    у которой сорвалась запись, не оставляла следа НИГДЕ: карточка живёт в
    памяти, снапшота у таких состояний не было, строки метрики — тоже. Поэтому
    «явка бота» (И36), у которой знаменатель — запланированные встречи, была
    непосчитаема в принципе.

    Ключ строки — ключ карточки встречи («команда:задача:время начала»), так что
    повторные вызовы по одной встрече обновляют её же строку: отменённый пропуск
    («записывать всё-таки будем») не оставляет лишнего провала.
    """
    try:
        if outcome not in MEETING_OUTCOMES:
            raise ValueError(f"неизвестный исход встречи: {outcome}")
        team = security.team_of(getattr(st, "owner", "") or "")
        if not team:
            return
        row = {
            "id": f"m:{getattr(st, 'key', '') or id(st)}",
            "team": team,
            "at": time.time(),
            "title": (getattr(st, "title", "") or "")[:200],
            "kind": "meeting",
            "status": outcome,
            "ok": outcome == "recorded",
            "has_protocol": False,
            "duration_sec": float(getattr(st, "recorded_sec", 0) or 0),
            "stop_reason": getattr(st, "stop_reason", None) or None,
            # Запись не уехала в облако. Поле существовало и раньше, но жило
            # только в памяти карточки — после перезапуска узнать было негде.
            "upload_error": (str(getattr(st, "upload_error", "") or "")[:300]
                             or None),
            "detail": (str(getattr(st, "detail", "") or "")[:300] or None),
            "join_delay_sec": getattr(st, "join_delay_sec", None),
            "silent": bool(getattr(st, "audio_warning", False)),
            "job_id": getattr(st, "job_id", None) or None,
        }
        if db.enabled():
            db.stats_add(row)
        else:
            rows = [r for r in _file_load(team) if r.get("id") != row["id"]]
            rows.append(row)
            _file_save(team, rows)
    except Exception:
        _LOG.warning("Исход встречи %s (%s) не записан",
                     getattr(st, "key", "?"), outcome, exc_info=True)


def _quality_row(job: Any, a: dict) -> dict:
    """Качество протокола — из того, что УЖЕ посчитано на этой встрече.

    Всё перечисленное жило ровно сутки внутри `job.analysis` и стиралось
    ретеншном: доля подтверждённых цитатами пунктов, дефекты `_quality`,
    удалённые галлюцинации, выброшенный шум, откат на запасной движок, следы
    ручной правки. Без этих чисел на вопрос «протоколы стали лучше или хуже»
    отвечать нечем — см. docs/ТЗ-МЕТРИКИ.md §3.
    """
    q = a.get("_quality") or {}
    ver = a.get("verification") or {}
    vs = ver.get("stats") or {}
    status = getattr(job, "status", "") or ""
    # ⚠️ «Задача завершилась» и «протокол готов» — разные вещи. Сборка
    # протокола падает внутри задачи, которая остаётся `done` с текстом в
    # analysis_error, и в метрике такая встреча раньше была успехом.
    protocol_ok = bool(a) and not getattr(job, "analysis_error", None)
    # Ответственный есть — задача исполнима; без него задачи не делаются.
    tasks = [t for t in (a.get("tasks") or []) if isinstance(t, dict)]
    with_owner = sum(1 for t in tasks
                     if str(t.get("owner") or "").strip() not in ("", "—", "-"))
    return {
        "status": status,
        "protocol_ok": protocol_ok,
        "engine": a.get("_provider") or getattr(job, "provider", None) or None,
        # Откат на запасной движок человек видит как «ЧЕРНОВИК» в шапке Word.
        "fallback": bool(a.get("_fallback")),
        "preset": getattr(job, "preset", None) or None,
        # Чистое время распознавания: finished_at − started_at для этого не
        # годится, его сдвигает пересборка протокола.
        "transcribe_sec": getattr(job, "transcribe_sec", None),
        "verify_checked": int(vs.get("checked") or 0) or None,
        "verify_confirmed": int(vs.get("confirmed") or 0) if vs else None,
        "verify_mode": ver.get("mode") or None,
        # Версия правил проверки: доля подтверждённых сравнима во времени
        # только внутри одной версии (ослабили порог — доля прыгнула).
        "verify_version": vs.get("version") or None,
        "topics": int(q.get("topics") or 0) or None,
        "empty_topics": len(q.get("empty_topics") or []) if q else None,
        "dropped_topics": len(a.get("_dropped_topics") or []) or None,
        "dropped_items": len(a.get("_dropped") or []) or None,
        "summary_is_toc": bool(q.get("summary_is_toc")) if q else None,
        "edited": bool(a.get("_edited")),
        "tasks_with_owner": with_owner if tasks else None,
        # Чем кончилась запись, из которой взялась задача. Нужно рядом с
        # качеством: протокол встречи, оборванной по пределу длительности, и
        # протокол нормально завершённой — разные истории.
        "stop_reason": getattr(job, "stop_reason", "") or None,
        # «Укол в процесс» для отчёта клиенту (И58): вопросы, повисшие без
        # ответа, и задачи прошлой встречи серии, о которых не вспомнили.
        "unanswered": sum(1 for x in (a.get("statuses") or [])
                          if isinstance(x, dict) and x.get("status") == "без ответа")
                      if a.get("statuses") else None,
        "carried_stale": sum(1 for x in ((a.get("_carried") or {}).get("items") or [])
                             if isinstance(x, dict) and x.get("status") == "без упоминания")
                         if a.get("_carried") else None,
    }


def summary(user: str, days: int = 30) -> dict:
    """Aggregate the team's metrics over the last `days`."""
    team = security.team_of(user)
    since = time.time() - max(1, int(days)) * 86400
    if db.enabled():
        all_rows = db.stats_load(team, since)
    else:
        all_rows = [r for r in _file_load(team) if (r.get("at") or 0) >= since]

    # ⚠️ Два вида строк в одной таблице. У записанной встречи есть И строка
    # исхода (kind="meeting"), И строка задачи распознавания — считать их
    # вместе значит удвоить встречи. Строки, записанные до появления колонки,
    # пустые по kind и относятся к задачам.
    rows = [r for r in all_rows if (r.get("kind") or "job") == "job"]
    mrows = [r for r in all_rows if r.get("kind") == "meeting"]

    ok = [r for r in rows if r.get("ok")]
    secs = sum(float(r.get("duration_sec") or 0) for r in ok)
    # Person-hours: how much of the org's combined time these meetings consumed.
    person_secs = sum(float(r.get("duration_sec") or 0) * max(1, int(r.get("speakers") or 1))
                      for r in ok)
    tasks = sum(int(r.get("tasks") or 0) for r in ok)
    decisions = sum(int(r.get("decisions") or 0) for r in ok)
    protocols = sum(1 for r in ok if r.get("has_protocol"))
    failed = sum(1 for r in rows if not r.get("ok"))

    # Расход модели: токены — всегда, деньги — только если цена задана хотя бы
    # у части встреч. `priced` говорит, по скольким встречам сумма собрана:
    # без этого «$2 за месяц» читалось бы как полная стоимость, даже если
    # цена известна у одной встречи из сорока.
    tok_in = sum(int(r.get("tokens_in") or 0) for r in rows)
    tok_cached = sum(int(r.get("tokens_cached") or 0) for r in rows)
    tok_out = sum(int(r.get("tokens_out") or 0) for r in rows)
    llm_calls = sum(int(r.get("llm_calls") or 0) for r in rows)
    priced = [r for r in rows if r.get("usd") is not None]
    usd = round(sum(float(r.get("usd") or 0) for r in priced), 2) if priced else None
    engines = sorted({e.strip() for r in rows
                      for e in (r.get("engines") or "").split(",") if e.strip()})

    # --- качество протоколов -------------------------------------------------
    # Доли считаются от РАЗНЫХ знаменателей, и это принципиально: «проверено»
    # бывает не у всех протоколов (проверка могла сорваться), «с ответственным»
    # — только там, где задачи вообще есть.
    checked = sum(int(r.get("verify_checked") or 0) for r in rows)
    confirmed = sum(int(r.get("verify_confirmed") or 0) for r in rows)
    with_owner = sum(int(r.get("tasks_with_owner") or 0) for r in rows)
    protocols_ok = sum(1 for r in rows if r.get("protocol_ok"))
    # ⚠️ Протокол, который не собрался, раньше попадал в метрику как успех:
    # сборка падает внутри задачи, а задача остаётся `done`.
    protocol_failed = sum(1 for r in rows
                          if r.get("has_protocol") is not None
                          and r.get("protocol_ok") is False and r.get("ok"))
    drafts = sum(1 for r in rows if r.get("fallback"))
    edited = sum(1 for r in rows if r.get("edited"))
    dropped_topics = sum(int(r.get("dropped_topics") or 0) for r in rows)
    dropped_items = sum(int(r.get("dropped_items") or 0) for r in rows)
    empty_topics = sum(int(r.get("empty_topics") or 0) for r in rows)
    topics_total = sum(int(r.get("topics") or 0) for r in rows)
    cancelled = sum(1 for r in rows if r.get("status") == "cancelled")
    unanswered = sum(int(r.get("unanswered") or 0) for r in rows)
    carried_stale = sum(int(r.get("carried_stale") or 0) for r in rows)
    tr_secs = [float(r.get("transcribe_sec") or 0) for r in rows
               if r.get("transcribe_sec")]
    # Версии правил проверки за период. Если их несколько, доля подтверждённых
    # НЕСРАВНИМА внутри периода — правила менялись.
    versions = sorted({r.get("verify_version") for r in rows
                       if r.get("verify_version")})
    # Расход по движкам с числами.
    by_engine: dict[str, dict] = {}
    for r in rows:
        for model, v in (_as_dict(r.get("tokens_by_model")) or {}).items():
            e = by_engine.setdefault(model, {"calls": 0, "in": 0, "out": 0,
                                             "meetings": 0})
            e["calls"] += int((v or {}).get("calls") or 0)
            e["in"] += int((v or {}).get("in") or 0)
            e["out"] += int((v or {}).get("out") or 0)
            e["meetings"] += 1

    # Per-day counts for the trend bars (oldest -> newest).
    by_day: dict[str, int] = {}
    for i in range(int(days)):
        d = time.strftime("%Y-%m-%d", time.localtime(time.time() - (days - 1 - i) * 86400))
        by_day[d] = 0
    for r in ok:
        d = time.strftime("%Y-%m-%d", time.localtime(r.get("at") or 0))
        if d in by_day:
            by_day[d] += 1

    # Which recurring meetings eat the most time (title -> count + hours).
    agg: dict[str, dict] = {}
    for r in ok:
        key = _norm_title(r.get("title") or "—")
        e = agg.setdefault(key, {"title": key, "count": 0, "hours": 0.0})
        e["count"] += 1
        e["hours"] += float(r.get("duration_sec") or 0) / 3600
    top = sorted(agg.values(), key=lambda x: x["hours"], reverse=True)[:5]

    # --- ценность: протокол прочитали и он пригодился (§5) -------------------
    # ⚠️ Производство протоколов — это ПРЕДЛОЖЕНИЕ: бот ходит на встречи сам,
    # и «сколько встреч обработано» растёт от календаря клиента, а не от пользы
    # продукта. Ценность измеряется только потреблением, поэтому знаменатель —
    # собранные протоколы, а числитель берётся из событий ЧЕЛОВЕКА.
    all_ev = events.load(team, since)
    ev = events.human_rows(all_ev)
    opens = events.by_job(ev, {events.OPENED})
    actions = events.by_job(ev, events.ACTIONS)
    built = [r for r in rows if r.get("protocol_ok")]
    ready_at = {r["id"]: float(r.get("at") or 0) for r in built if r.get("id")}
    read_jobs, lags, readers_per_job = [], [], []
    for jid, ready in ready_at.items():
        rs = opens.get(jid) or []
        # ⚠️ Окно 72 часа считается от ГОТОВНОСТИ протокола, а не от запроса
        # сводки: иначе вчерашние встречи всегда «не прочитаны», а месячные —
        # всегда прочитаны, и метрика меряла бы длину периода.
        fresh = [e for e in rs if 0 <= float(e.get("at") or 0) - ready <= READ_WINDOW_SEC]
        if not fresh:
            continue
        read_jobs.append(jid)
        lags.append((min(float(e.get("at") or 0) for e in fresh) - ready) / 60)
        readers_per_job.append(len({e.get("actor") for e in fresh}))
    acted = [jid for jid in ready_at if actions.get(jid)]
    all_readers = {e.get("actor") for e in ev if e.get("kind") == events.OPENED}

    # --- куда уходят деньги: расход по стадиям (§7, И8) -----------------------
    # Общий расход встречи — одно число, и вопрос «за что заплатили» остаётся
    # без ответа. Проверка цитат на длинной встрече стоит больше половины
    # входа, и увидеть это можно, только разделив вызовы по стадиям.
    # ⚠️ Деньги берутся из СОБЫТИЯ, где цена записана на момент вызова:
    # пересчитать задним числом нельзя, прайсы меняются.
    by_stage: dict[str, dict] = {}
    for e in all_ev:
        if e.get("kind") != events.LLM_CALL:
            continue
        x = _as_dict(e.get("extra"))
        code = x.get("stage") or "прочее"
        st_row = by_stage.setdefault(code, {"calls": 0, "in": 0, "out": 0,
                                            "usd": 0.0, "priced": 0})
        st_row["calls"] += 1
        st_row["in"] += int(x.get("in") or 0)
        st_row["out"] += int(x.get("out") or 0)
        if x.get("usd") is not None:
            st_row["usd"] += float(x["usd"])
            st_row["priced"] += 1

    # --- юнит-экономика (§7) --------------------------------------------------
    from .automation import settings as auto_settings
    try:
        team_cfg = auto_settings.load(team)
    except Exception:   # noqa: BLE001 — экономика не должна ронять сводку
        team_cfg = {}
    econ = economics(rows, mrows, all_ev, team_cfg)

    # --- исходы встреч (§14.2) ----------------------------------------------
    # Единица — ВСТРЕЧА, а не задача распознавания: клиенту всё равно, на каком
    # шаге сломалось, и провал планировщика («бот не пришёл») виден только
    # отсюда.
    by_outcome = {o: sum(1 for r in mrows if r.get("status") == o)
                  for o in MEETING_OUTCOMES}
    planned = len(mrows)
    # Знаменатель явки — запланированные МИНУС сознательно пропущенные
    # (фильтр, «не записывать», бот уже в этом звонке): отказ по решению — не
    # провал бота.
    due = planned - by_outcome["skipped"]
    upload_failed = sum(1 for r in mrows if r.get("upload_error"))
    # ⚠️ Явка разделяется на три отказа, потому что лечатся они по-разному:
    # «не пришёл» — планировщик, «не пустили» — вёрстка Телемоста или вход в
    # Яндекс, «опоздал» — бот дошёл, но начала разговора в записи нет.
    join_failed = sum(1 for r in mrows if r.get("stop_reason") == "join_failed")
    delays = [float(r.get("join_delay_sec")) for r in mrows
              if r.get("join_delay_sec") is not None]
    late = sum(1 for d in delays if d > LATE_JOIN_SEC)
    stop_reasons: dict[str, int] = {}
    for r in mrows:
        code = r.get("stop_reason")
        if code:
            stop_reasons[code] = stop_reasons.get(code, 0) + 1

    return {
        "days": int(days),
        "meetings": len(ok),
        # --- экономика (§7) ---
        **{f"econ_{k}": v for k, v in econ.items()},
        # --- ценность (§5) ---
        # Знаменатель — протоколы, собранные БЕЗ фатальной ошибки: файл,
        # которого нет, никто не мог прочитать.
        "protocols_built": len(built),
        "protocols_read": len(read_jobs),
        # ⚠️ Читать только в паре с `readers_median`: одно открытие владельца,
        # проверяющего бота, от чтения командой неотличимо.
        "read_ratio": (round(len(read_jobs) / len(built), 3)
                       if len(built) >= _MIN_DENOM else None),
        "read_window_hours": round(READ_WINDOW_SEC / 3600),
        "readers_median": _median([float(x) for x in readers_per_job]),
        # ⚠️ Уникальных читателей за период показываем от пяти: меньше —
        # и «доля» вычисляется обратно до конкретного человека (И63).
        "readers_total": len(all_readers) if len(all_readers) >= _MIN_READERS else None,
        # Медиана минут от готовности протокола до первого открытия. У команд с
        # пятничным разбором длинный лаг законен — это не поломка.
        "time_to_open_min": (round(_median(lags)) if lags else None),
        # И12: прочитали ≠ пригодилось. В числителе — только действия ЧЕЛОВЕКА
        # (правка, выгрузка, вопрос, перегенерация, задача в трекер).
        "protocols_acted": len(acted),
        "acted_ratio": (round(len(acted) / len(built), 3)
                        if len(built) >= _MIN_DENOM else None),
        # --- исходы встреч ---
        "planned": planned,
        "recorded": by_outcome["recorded"],
        "missed": by_outcome["missed"],
        "skipped": by_outcome["skipped"],
        "rec_failed": by_outcome["rec_error"],
        # ⚠️ Доля показывается только при знаменателе от 20 (ТЗ §9): при пяти
        # встречах процент — это пересказанная единица, и он вводит в
        # заблуждение сильнее, чем её отсутствие. Абсолютные числа — всегда.
        "attendance": (round(by_outcome["recorded"] / due, 3)
                       if due >= _MIN_DENOM else None),
        "attendance_base": due,
        # Разложение «не явился» на три причины (И36).
        "join_failed": join_failed,
        "late_joins": late,
        "join_delay_median_sec": (round(_median(delays)) if delays else None),
        # Вовремя — вошёл и начал писать в пределах порога. Это и есть
        # «здоровый» вход; опоздавший бот формально записал встречу, но начало,
        # где обычно и ставят задачи, потеряно.
        "on_time": (round((by_outcome["recorded"] - late) / due, 3)
                    if due >= _MIN_DENOM else None),
        # Запись осталась на сервере, потому что облако её не приняло.
        # Видео на сервере жить не должно — это прямой расход диска.
        "upload_failed": upload_failed,
        "by_stop_reason": [{"reason": c, "label": stop_reason_ru(c), "count": n}
                           for c, n in sorted(stop_reasons.items(),
                                              key=lambda kv: -kv[1])],
        "hours": round(secs / 3600, 1),
        "person_hours": round(person_secs / 3600, 1),
        "protocols": protocols,
        "tasks": tasks,
        "decisions": decisions,
        "avg_minutes": round((secs / 60 / len(ok)) if ok else 0),
        "failed": failed,
        "reliability": round(100 * len(ok) / len(rows)) if rows else 100,
        # ESTIMATE — see MANUAL_MINUTES_COEFF; the UI states the assumption.
        "hours_saved": round(secs / 3600 * MANUAL_MINUTES_COEFF, 1),
        "saved_coeff": MANUAL_MINUTES_COEFF,
        "by_day": [{"date": d, "count": c} for d, c in by_day.items()],
        "top": top,
        "tokens_in": tok_in,
        "tokens_cached": tok_cached,
        "tokens_out": tok_out,
        "llm_calls": llm_calls,
        "tokens_per_meeting": round((tok_in + tok_out) / len(ok)) if ok else 0,
        "usd": usd,
        "usd_meetings": len(priced),
        "engines": engines,
        "by_engine": [dict(model=m, **v) for m, v in
                      sorted(by_engine.items(), key=lambda kv: -kv[1]["in"])],
        # Расход по стадиям: карта / уплотнение / сведение / проверка /
        # перегенерация / вопрос. `usd` показываем только там, где цена была
        # известна у ВСЕХ вызовов стадии — частичная сумма врёт как полная.
        "by_stage": [{"stage": code, "label": STAGE_RU.get(code, code),
                      "calls": v["calls"], "in": v["in"], "out": v["out"],
                      "usd": (round(v["usd"], 4)
                              if v["priced"] == v["calls"] and v["calls"] else None)}
                     for code, v in sorted(by_stage.items(),
                                           key=lambda kv: -kv[1]["in"])],
        # --- качество ---
        "verify_checked": checked,
        "verify_confirmed": confirmed,
        # Доля пунктов с подтверждающей цитатой. ⚠️ Читать ТОЛЬКО в паре с
        # `confirmed_per_hour`: саму долю легко «улучшить», выбросив все
        # неподтверждённые пункты.
        "confirmed_ratio": round(confirmed / checked, 3) if checked else None,
        "confirmed_per_hour": (round(confirmed / (secs / 3600), 1)
                               if secs and confirmed else 0),
        "verify_versions": versions,
        "tasks_with_owner": with_owner,
        "owner_ratio": round(with_owner / tasks, 3) if tasks else None,
        "protocol_failed": protocol_failed,
        "drafts": drafts,
        "draft_ratio": round(drafts / protocols_ok, 3) if protocols_ok else None,
        "edited": edited,
        "dropped_topics": dropped_topics,
        "dropped_items": dropped_items,
        "empty_topics": empty_topics,
        "topics_total": topics_total,
        "cancelled": cancelled,
        # Укол в процесс (И58): повисшие вопросы и несдвинувшиеся задачи серии.
        "unanswered": unanswered,
        "carried_stale": carried_stale,
        # Коэффициент распознавания: секунд обработки на секунду записи.
        "transcribe_ratio": (round(sum(tr_secs) / secs, 2)
                             if tr_secs and secs else None),
    }


def _cores_share() -> float:
    """Доля ядер сервера, которую занимает распознавание (VTX_CPU_THREADS)."""
    import os
    from . import config
    total = os.cpu_count() or config.CPU_THREADS or 1
    return max(0.0, min(1.0, config.CPU_THREADS / total))


def economics(rows: list[dict], mrows: list[dict], all_ev: list[dict],
              cfg: dict | None = None) -> dict:
    """Себестоимость встреч и команды (docs/ТЗ-МЕТРИКИ.md §7, И27—И35).

    Единица — ВСТРЕЧА, прошедшая распознавание (строка kind=job). К ней
    относятся: вызовы модели с её job_id (включая пересборки — иначе маржа
    выглядит лучше реальности ровно у тех, кто пересобирает чаще), распознавание
    (машино-час × доля ядер × секунды) и запись (час слота × длительность —
    браузер и ffmpeg держат ресурсы всю встречу, включая тишину).

    ⚠️ Каждый компонент считается ТОЛЬКО при заданной цене; встреча получает
    итог, только если известны все её компоненты. Частичная себестоимость
    выглядит как полная и врёт в сторону «дёшево» — самую опасную для тарифа.
    ⚠️ Никаких средних: медиана и 90-й перцентиль (И29).
    """
    cfg = cfg or {}
    cores = _cores_share()

    # Вызовы модели по встречам — из событий, где записаны цена на момент
    # вызова, чей ключ и была ли это пересборка.
    llm_by_job: dict[str, dict] = {}
    for e in all_ev:
        if e.get("kind") != events.LLM_CALL or not e.get("job_id"):
            continue
        x = _as_dict(e.get("extra"))
        d = llm_by_job.setdefault(e["job_id"], {"service": 0.0, "team": 0.0,
                                                 "rerun": 0.0, "unpriced": 0,
                                                 "calls": 0})
        d["calls"] += 1
        if x.get("usd") is None:
            d["unpriced"] += 1
            continue
        usd = float(x["usd"])
        if x.get("key") == usage.TEAM_KEY:
            d["team"] += usd          # платит клиент своим ключом (И34)
        else:
            d["service"] += usd
            if x.get("rerun"):
                d["rerun"] += usd

    rec_by_job = {r["job_id"]: r for r in mrows if r.get("job_id")}
    unlinked_rec_sec = sum(float(r.get("duration_sec") or 0) for r in mrows
                           if r.get("status") == "recorded" and not r.get("job_id"))

    per_meeting: list[float] = []
    per_minute: list[float] = []
    tot = {"llm": 0.0, "recog": 0.0, "record": 0.0, "team_key": 0.0, "rerun": 0.0}
    priced = 0
    minutes_total = 0.0
    for r in rows:
        secs = float(r.get("duration_sec") or 0)
        minutes_total += secs / 60
        jid = r.get("id")
        llm = llm_by_job.get(jid)
        # Старые строки без событий: расход задачи считаем ключом сервиса.
        if llm is None:
            llm_usd = r.get("usd")
            llm_known = llm_usd is not None
            llm_usd = float(llm_usd or 0)
            team_usd = rerun_usd = 0.0
        else:
            llm_known = llm["unpriced"] == 0
            llm_usd, team_usd, rerun_usd = llm["service"], llm["team"], llm["rerun"]
        recog = None
        if COST_CPU_HOUR_USD is not None and r.get("transcribe_sec") is not None:
            recog = float(r["transcribe_sec"]) / 3600 * COST_CPU_HOUR_USD * cores
        record = None
        if COST_SLOT_HOUR_USD is not None:
            mr = rec_by_job.get(jid)
            rec_sec = float((mr or {}).get("duration_sec") or 0) if mr else secs
            record = rec_sec / 3600 * COST_SLOT_HOUR_USD
        tot["team_key"] += team_usd
        tot["rerun"] += rerun_usd
        if not (llm_known and recog is not None and record is not None):
            continue
        cost = llm_usd + recog + record
        tot["llm"] += llm_usd
        tot["recog"] += recog
        tot["record"] += record
        priced += 1
        per_meeting.append(cost)
        if secs > 0:
            per_minute.append(cost / (secs / 60))
    if COST_SLOT_HOUR_USD is not None and unlinked_rec_sec:
        # Записи без распознавания тоже стоили слота — в итог, но не в «на встречу».
        tot["record"] += unlinked_rec_sec / 3600 * COST_SLOT_HOUR_USD
    total = tot["llm"] + tot["recog"] + tot["record"]
    priced_minutes = sum(float(r.get("duration_sec") or 0) / 60 for r in rows) if priced == len(rows) else None

    # И32: три объяснения к «съедает маржу».
    durs = [float(r.get("duration_sec") or 0) for r in rows if r.get("duration_sec")]
    long_minutes = sum(d for d in durs if d > LONG_MEETING_SEC) / 60
    recorded = [r for r in mrows if r.get("status") == "recorded"]
    silent = sum(1 for r in recorded if r.get("silent"))

    # И28/И31/И35: команда, подписка, безубыточность.
    sub_rub = cfg.get("subscription_rub")
    support_h = float(cfg.get("support_hours_month") or 0)
    team_cost_usd = (support_h * SUPPORT_HOUR_RUB / USD_RUB
                     if support_h and SUPPORT_HOUR_RUB and USD_RUB else
                     (0.0 if not support_h else None))
    sub_usd = (float(sub_rub) / USD_RUB if sub_rub and USD_RUB else None)
    cost_per_hour = (60 * _median(per_minute)) if per_minute else None
    cost_to_price = breakeven_hours = None
    if sub_usd and team_cost_usd is not None and priced == len(rows) and rows:
        cost_to_price = round((total + team_cost_usd) / sub_usd, 3)
    if sub_usd and team_cost_usd is not None and cost_per_hour:
        breakeven_hours = round(max(0.0, sub_usd - team_cost_usd) / cost_per_hour, 1)

    missing = [n for n, v in (("VTX_COST_CPU_HOUR_USD", COST_CPU_HOUR_USD),
                              ("VTX_COST_SLOT_HOUR_USD", COST_SLOT_HOUR_USD),
                              ("VTX_USD_RUB", USD_RUB)) if v is None]
    return {
        "cost_total_usd": round(total, 4) if priced else None,
        "cost_llm_usd": round(tot["llm"], 4) if priced else None,
        "cost_recog_usd": round(tot["recog"], 4) if priced else None,
        "cost_record_usd": round(tot["record"], 4) if priced else None,
        # Вызовы на ключе клиента: для владельца ноль, но знать их надо (И34).
        "cost_client_key_usd": round(tot["team_key"], 4),
        # Доля себестоимости, ушедшей на пересборки и повторы (И32).
        "rerun_cost_share": (round(tot["rerun"] / tot["llm"], 3)
                             if tot["llm"] else None),
        "priced_meetings": priced,
        "cost_median_usd": (round(_median(per_meeting), 4) if per_meeting else None),
        "cost_p90_usd": (round(_pct(per_meeting, 0.9), 4) if per_meeting else None),
        # И30: почти вся себестоимость линейна по минутам, а не по встречам.
        # Отсюда следствие: тариф «за встречу» — мина; продавать часы.
        "cost_per_minute_usd": (round(_median(per_minute), 5) if per_minute else None),
        "cost_per_hour_usd": (round(cost_per_hour, 4) if cost_per_hour else None),
        "long_minutes_share": (round(long_minutes / (sum(durs) / 60), 3)
                               if durs and sum(durs) else None),
        "duration_p95_min": (round(_pct(durs, 0.95) / 60) if durs else None),
        "silent_share": (round(silent / len(recorded), 3) if recorded else None),
        "silent_meetings": silent,
        "subscription_rub": sub_rub,
        "support_hours_month": support_h or None,
        "team_cost_usd": (round(team_cost_usd, 2) if team_cost_usd else team_cost_usd),
        # И31: больше 0,3 — наблюдение, 0,5 — разговор о тарифе, 1,0 — убыток.
        "cost_to_price": cost_to_price,
        # И35: сколько часов встреч команда может провести за месяц, пока
        # тариф не ушёл в минус. Знать ДО продажи и зашивать в тариф.
        "breakeven_hours": breakeven_hours,
        "cost_missing": missing,
        "priced_minutes": (round(priced_minutes) if priced_minutes is not None else None),
    }


def _as_dict(v):
    """JSONB приходит словарём, файловый режим — тоже; строка бывает у старых
    записей, сохранённых до появления колонки."""
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip().startswith("{"):
        try:
            return json.loads(v)
        except ValueError:
            return {}
    return {}


def _norm_title(t: str) -> str:
    """Group recurring meetings: strip the date/time prefix the recorder adds."""
    t = (t or "").strip()
    # "16.07.2026, 14:35. - Операционная встреча.mp4" -> "Операционная встреча"
    if " - " in t:
        t = t.split(" - ", 1)[1]
    for ext in (".mp4", ".mp3", ".wav", ".m4a", ".mkv", ".webm"):
        if t.lower().endswith(ext):
            t = t[: -len(ext)]
    return t.strip(" .") or "—"


# --------------------------------------------------------------------------- #
# Отчёты (docs/ТЗ-МЕТРИКИ.md §11)
# --------------------------------------------------------------------------- #
def _n(v, digits: int = 0):
    """Число для текста отчёта; None — прочерк, а не ноль."""
    if v is None:
        return "—"
    return f"{v:.{digits}f}".replace(".", ",") if digits else str(int(round(v)))


def _pct_text(ratio, base: int | None = None) -> str:
    """Доля словами. ⚠️ При знаменателе меньше 20 процент не показываем (§9):
    вместо «отток 9 %» — «один из одиннадцати»."""
    if ratio is None:
        return "мало данных" if (base or 0) < _MIN_DENOM else "—"
    return f"{round(ratio * 100)} %"


def client_report(user: str, days: int = 30) -> dict:
    """Отчёт клиенту, одна страница (И58).

    Что здесь ЕСТЬ: обработано встреч и часов, решений и задач, задач ушло в
    трекер, сэкономленное время С ФОРМУЛОЙ, надёжность человеческим языком и
    «укол в процесс» — самая полезная и самая неудобная часть.

    Чего здесь НЕТ и не будет (И59): себестоимости, движков и токенов (кроме
    факта «черновик»), сравнения с другими клиентами и любой персональной
    активности сотрудников — только числа по команде.
    """
    st = summary(user, days=days)
    lines: list[str] = []
    lines.append(f"Отчёт за {days} дн.")
    lines.append(f"Обработано встреч: {st['meetings']}, часов записи: {_n(st['hours'], 1)}.")
    lines.append(f"Зафиксировано решений: {st['decisions']}, задач: {st['tasks']}"
                 + (f", из них с ответственным: {st['tasks_with_owner']}"
                    if st["tasks_with_owner"] else "") + ".")
    if st.get("protocols_acted"):
        lines.append(f"Протоколов, по которым что-то сделали (правка, выгрузка, вопрос, "
                     f"задача в трекер): {st['protocols_acted']} из {st['protocols_built']}.")
    # Сэкономленное время — с явной формулой, никогда как измерение (И58, §10).
    lines.append(f"Сэкономлено на ведении протоколов: около {_n(st['hours_saved'], 1)} ч "
                 f"(оценка: {_n(st['hours'], 1)} ч встреч × {st['saved_coeff']} — столько "
                 "обычно уходит на ручной протокол; это не замер).")
    # Надёжность — человеческим языком: «пришли на 41 из 41».
    if st.get("planned"):
        came = st["recorded"]
        due = st["attendance_base"]
        rel = f"Бот пришёл на {came} {_plural(came, 'встречу', 'встречи', 'встреч')} из {due}"
        if st.get("late_joins"):
            rel += f", на {st['late_joins']} — с опозданием"
        if st.get("missed"):
            rel += f", {st['missed']} пропущено"
        if st.get("join_failed"):
            rel += f", на {st['join_failed']} не пустили"
        lines.append(rel + ".")
    if st.get("drafts"):
        lines.append(f"Протоколов-черновиков (собраны запасным движком): {st['drafts']}.")
    if st.get("protocol_failed"):
        lines.append(f"Не собралось протоколов: {st['protocol_failed']}.")
    # Укол в процесс.
    prick = []
    if st["tasks"] and st.get("tasks_with_owner") is not None:
        no_owner = st["tasks"] - st["tasks_with_owner"]
        if no_owner > 0:
            prick.append(f"задач без ответственного — {no_owner}")
    if st.get("unanswered"):
        prick.append(f"вопросов повисло без ответа — {st['unanswered']}")
    if st.get("carried_stale"):
        prick.append(f"задач прошлых встреч, о которых не вспомнили, — {st['carried_stale']}")
    if prick:
        lines.append("На что стоит посмотреть: " + "; ".join(prick) + ".")
    return {"days": days, "text": "\n".join(lines), "lines": lines,
            "meetings": st["meetings"], "hours": st["hours"],
            "decisions": st["decisions"], "tasks": st["tasks"],
            "tasks_with_owner": st["tasks_with_owner"],
            "unanswered": st.get("unanswered"), "carried_stale": st.get("carried_stale"),
            "protocols_acted": st.get("protocols_acted"),
            "recorded": st.get("recorded"), "attendance_base": st.get("attendance_base"),
            "drafts": st.get("drafts"), "hours_saved": st["hours_saved"]}


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if 11 <= n % 100 <= 19:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


# Спящая платящая команда (И48): бот работает, протоколы собираются, а люди их
# не открывают уже столько дней. Это не число для дашборда, а список на неделю.
SLEEP_DAYS = int(__import__("os").getenv("VTX_SLEEP_DAYS", "14"))


def owner_report(days: int = 7) -> dict:
    """Еженедельная таблица владельца сервиса по всем командам (И56).

    Строка на команду: чтение, действия, себестоимость, инциденты, черновики,
    пересборки успешных протоколов; отдельно — спящие платящие (И48).
    ⚠️ Только для основателя сервера: здесь видны все команды разом.
    """
    from .automation import settings as auto_settings
    teams = []
    sleeping = []
    for team in security.list_teams():
        st = summary(team, days=days)
        try:
            cfg = auto_settings.load(team)
        except Exception:   # noqa: BLE001
            cfg = {}
        paying = bool(cfg.get("subscription_rub"))
        row = {
            "team": team,
            "paying": paying,
            "meetings": st["meetings"],
            "protocols_built": st["protocols_built"],
            "protocols_read": st["protocols_read"],
            "read_ratio": st["read_ratio"],
            "protocols_acted": st["protocols_acted"],
            "cost_total_usd": st["econ_cost_total_usd"],
            "cost_to_price": st["econ_cost_to_price"],
            "drafts": st["drafts"],
            "incidents": (st["missed"] + st["rec_failed"] + st["upload_failed"]
                          + st["protocol_failed"]),
            "lost": st["missed"] + st["rec_failed"],
            "edited": st["edited"],
        }
        teams.append(row)
        # Спящие: платят, протоколы есть, но за окно никто не открыл ни одного.
        if paying:
            recent = summary(team, days=SLEEP_DAYS)
            if recent["protocols_built"] and not recent["protocols_read"]:
                sleeping.append({"team": team,
                                 "protocols_built": recent["protocols_built"],
                                 "days": SLEEP_DAYS})
    teams.sort(key=lambda r: (-int(r["paying"]), -(r["cost_total_usd"] or 0)))
    return {"days": days, "teams": teams, "sleeping": sleeping,
            "lost_total": sum(r["lost"] for r in teams)}
