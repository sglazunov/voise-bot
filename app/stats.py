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

from . import db, logs, security, usage

_LOG = logs.get("vtx.stats")

# How long writing minutes by hand takes, as a share of the meeting itself
# (listening back + typing). Deliberately conservative; shown in the UI as an
# estimate with the assumption spelled out.
MANUAL_MINUTES_COEFF = float(__import__("os").getenv("VTX_MINUTES_COEFF", "0.5"))
_MAX_ROWS = 5000     # file backend: keep the log bounded


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
    }


def summary(user: str, days: int = 30) -> dict:
    """Aggregate the team's metrics over the last `days`."""
    team = security.team_of(user)
    since = time.time() - max(1, int(days)) * 86400
    if db.enabled():
        rows = db.stats_load(team, since)
    else:
        rows = [r for r in _file_load(team) if (r.get("at") or 0) >= since]

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

    return {
        "days": int(days),
        "meetings": len(ok),
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
        # Коэффициент распознавания: секунд обработки на секунду записи.
        "transcribe_ratio": (round(sum(tr_secs) / secs, 2)
                             if tr_secs and secs else None),
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
