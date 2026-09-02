"""Черновики задач для Weeek из протокола встречи.

Чего хотел заказчик (операционная встреча 27.08.2026): «чтобы из встречи
вычислялись задачи и ставились в Weeek» (Зоя Р), «чтобы он отписывался:
поставить ли задачу и её описание, и потом отправляешь ему команду» (Кирилл
Бубнов). То есть не автомат вслепую, а ЧЕРНОВИКИ с подтверждением человеком.

Как устроено:
1. После сборки протокола `prepare()` превращает tasks (+ minor_tasks, если
   включено) в черновики: формулировка, исполнитель (сопоставлен с участником
   воркспейса Weeek), срок (разобран из «до пятницы» / «к 5 сентября»
   относительно даты встречи), основание-цитата с таймкодом.
2. Человек в интерфейсе отмечает нужные, правит исполнителя/срок и жмёт
   «Создать в Weeek» → `create_selected()`.
3. Авторежим (`weeek_tasks_auto`, выключен по умолчанию) создаёт только те,
   у которых есть дословное основание И однозначный исполнитель.

Идемпотентность: у черновика есть `key` (job + раздел + индекс) и `fp`
(отпечаток текста); статус «создано» переживает пересборку протокола по `fp`,
повторный клик по тому же `key` задачу не дублирует.

Черновики хранятся в `Job.weeek_tasks` — ОТДЕЛЬНО от `analysis`, потому что
пересборка/правка протокола заменяет `analysis` целиком.
"""
from __future__ import annotations

import hashlib
import re
import time
from datetime import date, datetime, timedelta

from . import names
from .analyze_schemas import norm_owner

_MAX_TITLE = 200

# --------------------------------------------------------------------------- #
# Сроки: «до пятницы», «к 5 сентября», «через неделю» → дата от даты встречи.
# Модель просят вернуть срок ДОСЛОВНО, дату считаем здесь — модели в датах
# ошибаются, а «пятница» относительно известного дня считается точно.
# --------------------------------------------------------------------------- #
_WEEKDAYS = {
    "понедельник": 0, "понедельника": 0, "понедельнику": 0,
    "вторник": 1, "вторника": 1, "вторнику": 1,
    "среда": 2, "среды": 2, "среду": 2, "среде": 2,
    "четверг": 3, "четверга": 3, "четвергу": 3,
    "пятница": 4, "пятницы": 4, "пятницу": 4, "пятнице": 4,
    "суббота": 5, "субботы": 5, "субботу": 5, "субботе": 5,
    "воскресенье": 6, "воскресенья": 6, "воскресенью": 6,
}
_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
# Только явные подсказки срока: «в понедельник обсуждали» — не дедлайн.
_DUE_CUES = re.compile(
    r"(?:\bдо|\bк|\bко|\bчерез|не позднее|не позже|срок|дедлайн|сдать|готов)\b", re.I)


def _month_num(word: str) -> int | None:
    w = word.lower().replace("ё", "е")
    for stem, n in _MONTHS.items():
        if w.startswith(stem) and (stem != "ма" or w in ("мая", "май", "мае")):
            return n
    return None


def parse_due(text: str, base: date) -> tuple[str | None, str | None]:
    """(ISO-дата, как прозвучало) или (None, None).

    Относительные сроки считаются от `base` — даты встречи. «до пятницы» на
    встрече в пятницу — следующая пятница. Непонятное → None, без выдумки.
    """
    if not text:
        return None, None
    low = text.lower().replace("ё", "е")

    m = re.search(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?!\d)", low)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        year = base.year if not y else (int(y) if len(y) == 4 else 2000 + int(y))
        try:
            dt = date(year, mo, d)
            if not y and dt < base - timedelta(days=60):
                dt = date(year + 1, mo, d)
            return dt.isoformat(), m.group(0)
        except ValueError:
            pass

    m = re.search(r"(?<!\d)(\d{1,2})(?:-?го)?\s+([а-я]{3,9})", low)
    if m and _month_num(m.group(2)):
        d, mo = int(m.group(1)), _month_num(m.group(2))
        year = base.year
        try:
            dt = date(year, mo, d)
            if dt < base - timedelta(days=60):
                dt = date(year + 1, mo, d)
            return dt.isoformat(), m.group(0)
        except ValueError:
            pass

    if re.search(r"\bпослезавтра\b", low):
        return (base + timedelta(days=2)).isoformat(), "послезавтра"
    if re.search(r"\bзавтра\b", low):
        return (base + timedelta(days=1)).isoformat(), "завтра"
    if re.search(r"\bсегодня\b", low):
        return base.isoformat(), "сегодня"
    if re.search(r"до конца (этой )?недели", low):
        return (base + timedelta(days=(4 - base.weekday()) % 7 or 0)).isoformat(), "до конца недели"
    if re.search(r"(на|к) следующей неделе|через неделю", low):
        return (base + timedelta(days=7)).isoformat(), re.search(
            r"(на|к) следующей неделе|через неделю", low).group(0)
    if re.search(r"через (две|2) недели", low):
        return (base + timedelta(days=14)).isoformat(), "через две недели"
    if re.search(r"до конца месяца", low):
        nxt = (base.replace(day=28) + timedelta(days=4)).replace(day=1)
        return (nxt - timedelta(days=1)).isoformat(), "до конца месяца"

    for word, wd in _WEEKDAYS.items():
        m = re.search(rf"\b{word}\b", low)
        if not m:
            continue
        # Требуем подсказку срока рядом («до», «к», «в»), иначе «в понедельник
        # обсуждали» стало бы сроком.
        before = low[max(0, m.start() - 14):m.start()]
        if not _DUE_CUES.search(before):
            continue
        delta = (wd - base.weekday()) % 7
        if delta == 0:
            delta = 7
        if re.search(r"следующ", before):
            delta += 7 if delta < 7 else 0
        return (base + timedelta(days=delta)).isoformat(), low[
            max(0, m.start() - 14):m.end()].strip()
    return None, None


# --------------------------------------------------------------------------- #
# Исполнитель: имя из протокола → участник воркспейса Weeek
# --------------------------------------------------------------------------- #
def _member_names(m: dict) -> list[str]:
    """Формы ПОЛНОГО имени участника: «Имя Фамилия», «Фамилия Имя», «Имя Ф».
    Одно имя без фамилии сюда не входит — это отдельный, нестрогий матч."""
    first = str(m.get("firstName") or m.get("first_name") or "").strip()
    last = str(m.get("lastName") or m.get("last_name") or "").strip()
    full = str(m.get("name") or "").strip()
    out = []
    if first and last:
        out += [f"{first} {last}", f"{last} {first}", f"{first} {last[:1]}"]
    if full and " " in full:
        out.append(full)
    return out


def _key(s: str) -> str:
    try:
        return names.key(s)
    except Exception:  # noqa: BLE001 — на случай экзотики в names.key
        return re.sub(r"\s+", " ", (s or "").lower().replace("ё", "е")).strip()


def match_member(name: str, members: list[dict], user_map: dict | None = None
                 ) -> tuple[str | None, str]:
    """(userId, вид совпадения: exact | map | fuzzy | none).

    Явный маппинг команды («Зоя Р» → uuid) побеждает. Затем — полное имя или
    «Имя Ф.» ровно у одного участника. Только имя, уникальное в воркспейсе, —
    `fuzzy`: показать, но не назначать автоматически.
    """
    name = norm_owner(name)
    if not name:
        return None, "none"
    k = _key(name)
    for mapped, uid in (user_map or {}).items():
        if _key(mapped) == k and uid:
            return str(uid), "map"
    exact = []
    for m in members or []:
        cands = {_key(c) for c in _member_names(m)}
        if k in cands:
            exact.append(m)
    if len(exact) == 1:
        return str(exact[0].get("id")), "exact"
    if len(exact) > 1:
        return None, "none"
    # Только имя (первое слово) — если уникально среди участников.
    first_word = k.split(" ")[0] if k else ""
    if first_word and len(first_word) >= 3:
        hits = [m for m in members or []
                if _key(str(m.get("firstName") or m.get("first_name")
                            or str(m.get("name") or "").split(" ")[0])) == first_word]
        if len(hits) == 1:
            return str(hits[0].get("id")), "fuzzy"
    return None, "none"


# --------------------------------------------------------------------------- #
# Черновики
# --------------------------------------------------------------------------- #
def _fingerprint(text: str) -> str:
    return hashlib.sha1(_key(text).encode("utf-8")).hexdigest()[:12]


def _verification(analysis: dict, section: str, idx: int) -> dict | None:
    try:
        rec = (analysis.get("verification") or {})[section][idx]
    except (KeyError, IndexError, TypeError):
        return None
    return rec if isinstance(rec, dict) else None


def prepare(job_id: str, analysis: dict | None, cfg: dict, meeting_date: date,
            members: list[dict] | None = None, previous: list[dict] | None = None,
            series: dict | None = None) -> list[dict]:
    """Черновики из протокола. Статусы уже созданных/пропущенных переносятся
    из `previous` по key, затем по fp — чтобы пересборка протокола не
    предлагала завести задачу второй раз."""
    if not analysis:
        return []
    sections = ["tasks"]
    if cfg.get("weeek_tasks_include_minor"):
        sections.append("minor_tasks")
    prev_by_key = {d.get("key"): d for d in (previous or []) if d.get("key")}
    prev_by_fp = {d.get("fp"): d for d in (previous or [])
                  if d.get("fp") and d.get("status") in ("created", "skipped")}
    user_map = cfg.get("weeek_user_map") or {}
    default_days = cfg.get("weeek_tasks_default_due_days")
    project_id = (series or {}).get("weeek_project_id") or cfg.get("weeek_tasks_project_id")
    out: list[dict] = []
    for section in sections:
        for idx, item in enumerate(analysis.get(section) or []):
            if isinstance(item, dict):
                text = str(item.get("task") or "").strip()
                owner = norm_owner(item.get("owner"))
                due_hint = str(item.get("due") or "").strip()
            else:
                text, owner, due_hint = str(item or "").strip(), "", ""
            if not text:
                continue
            key = f"{job_id}:{section}:{idx}"
            fp = _fingerprint(text)
            ver = _verification(analysis, section, idx)
            grounded = bool(ver and ver.get("ok"))
            uid, kind = match_member(owner, members or [], user_map)
            due, due_raw = parse_due(due_hint or text, meeting_date)
            if not due and default_days not in (None, "", 0):
                try:
                    due = (meeting_date + timedelta(days=int(default_days))).isoformat()
                except (TypeError, ValueError):
                    due = None
            draft = {
                "key": key, "fp": fp, "section": section, "index": idx,
                "title": text[:_MAX_TITLE], "task": text,
                "owner_name": owner, "owner_user_id": uid, "owner_match": kind,
                "due": due, "due_raw": due_raw or (due_hint or None),
                "priority": None,
                "grounded": grounded,
                "quote": (ver or {}).get("quote") or "",
                "t": (ver or {}).get("t") or "",
                "project_id": project_id,
                "board_id": cfg.get("weeek_tasks_board_id"),
                "column_id": cfg.get("weeek_tasks_column_id"),
                "status": "draft", "weeek_task_id": None, "weeek_url": None,
                "error": None, "created_at": None, "created_by": None,
            }
            old = prev_by_key.get(key) or prev_by_fp.get(fp)
            if old and old.get("status") in ("created", "skipped", "failed"):
                for k in ("status", "weeek_task_id", "weeek_url", "error",
                          "created_at", "created_by"):
                    draft[k] = old.get(k)
                if old.get("status") == "created":
                    for k in ("title", "owner_user_id", "due"):
                        draft[k] = old.get(k) or draft[k]
            out.append(draft)
    return out


def default_selected(draft: dict, cfg: dict) -> bool:
    """Отмечать ли черновик по умолчанию (и брать ли в авторежим)."""
    if draft.get("status") != "draft":
        return False
    if cfg.get("weeek_tasks_only_grounded", True) and not draft.get("grounded"):
        return False
    return draft.get("owner_match") in ("exact", "map")


# --------------------------------------------------------------------------- #
# Создание в Weeek
# --------------------------------------------------------------------------- #
def _html(s: str) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def description_html(draft: dict, meeting_title: str, meeting_date: str,
                     protocol_url: str = "", video_url: str = "") -> str:
    parts = [f"<p>Из протокола встречи «{_html(meeting_title)}» {_html(meeting_date)}.</p>"]
    if draft.get("quote"):
        t = f" [{_html(draft.get('t'))}]" if draft.get("t") else ""
        parts.append(f"<p><b>Основание</b>{t}: «{_html(draft['quote'])}»</p>")
    if draft.get("owner_name"):
        parts.append(f"<p>Ответственный по протоколу: {_html(draft['owner_name'])}</p>")
    if draft.get("due_raw"):
        parts.append(f"<p>Срок по протоколу: {_html(draft['due_raw'])}</p>")
    links = []
    if protocol_url:
        links.append(f'<a href="{_html(protocol_url)}">протокол</a>')
    if video_url:
        links.append(f'<a href="{_html(video_url)}">запись</a>')
    if links:
        parts.append("<p>" + " · ".join(links) + "</p>")
    parts.append(f'<p style="color:#888">voise-bot · {_html(draft.get("key"))}</p>')
    return "".join(parts)


def create_selected(drafts: list[dict], selected: list[dict], token: str,
                    meeting_title: str, meeting_date: str, user: str,
                    protocol_url: str = "", video_url: str = "",
                    client=None) -> list[dict]:
    """Создать отмеченные черновики. `selected` — [{key, title?, owner_user_id?,
    due?, priority?, project_id?, board_id?, column_id?}]. Возвращает список
    результатов по каждому key; черновики обновляются на месте.

    Частичная неудача ничего не откатывает: у каждой строки свой статус.
    `client` — модуль с create_task/update_task (по умолчанию automation.weeek),
    подменяется в тестах.
    """
    from .automation import weeek as _weeek
    client = client or _weeek
    by_key = {d["key"]: d for d in drafts}
    results = []
    for sel in selected:
        key = str(sel.get("key") or "")
        d = by_key.get(key)
        if d is None:
            results.append({"key": key, "ok": False, "error": "черновик не найден"})
            continue
        if d.get("status") == "created" and d.get("weeek_task_id"):
            results.append({"key": key, "ok": True, "already": True,
                            "weeek_task_id": d["weeek_task_id"], "weeek_url": d.get("weeek_url")})
            continue
        for f in ("title", "owner_user_id", "due", "priority", "project_id",
                  "board_id", "column_id"):
            if f in sel and sel[f] not in (None, ""):
                d[f] = sel[f]
        if not (d.get("title") or "").strip():
            results.append({"key": key, "ok": False, "error": "пустое название"})
            continue
        try:
            res = client.create_task(
                token, title=d["title"][:_MAX_TITLE],
                description=description_html(d, meeting_title, meeting_date,
                                             protocol_url, video_url),
                project_id=d.get("project_id"), board_id=d.get("board_id"),
                column_id=d.get("column_id"), user_id=d.get("owner_user_id"),
                priority=d.get("priority"), due=d.get("due"))
        except Exception as e:  # noqa: BLE001 — статус на строке, идём дальше
            d["status"], d["error"] = "failed", str(e)[:300]
            results.append({"key": key, "ok": False, "error": d["error"]})
            time.sleep(0.3)
            continue
        d["status"], d["error"] = "created", None
        d["weeek_task_id"] = res.get("id")
        d["weeek_url"] = res.get("url")
        d["created_at"], d["created_by"] = time.time(), user
        results.append({"key": key, "ok": True, "weeek_task_id": d["weeek_task_id"],
                        "weeek_url": d.get("weeek_url"), "warnings": res.get("warnings") or []})
        time.sleep(0.3)
    return results


def meeting_date_of(title: str, created_at: float | None) -> date:
    from . import meeting_series
    s = meeting_series.date_from_title(title)
    if s:
        try:
            return datetime.strptime(s, "%d.%m.%Y").date()
        except ValueError:
            pass
    return datetime.fromtimestamp(created_at or time.time()).date()
