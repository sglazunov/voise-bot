"""Память СЕРИИ встреч: контекст повторяющейся встречи и итоги прошлой.

Откуда взялось (операционная встреча 27.08.2026, Зоя Р и Сергей Глазунов):
встречи в Weeek повторяются под одним названием — «Встреча лидеров», «ОД сайт,
редизайн, магазин - проектная встреча», «Гранты - внутренняя»… Протокол каждой
собирался с нуля, и модель заново не понимала, кто есть кто, что за проект и
что решили неделю назад. Отсюда «простыня текста с практическими
неточностями» и невозможность отказаться от рукописных протоколов.

Идея: у каждой серии — своя карточка, которую ведёт человек (проект и суть,
участники и роли, цели встречи, глоссарий, закреплённый тип протокола,
приоритет), плюс автоматическая память о ПРОШЛОЙ встрече серии (решения,
открытые задачи, кратко о чём была). Оба блока уходят в промпт перед
расшифровкой — «сначала чтение контекста, потом самой встречи».

Ключ серии — нормализованное название встречи без даты/времени/расширения:
«28.08.2026, 16-00. - Встреча лидеров.mp4» и «Встреча лидеров» — одна серия.

Хранится per TEAM (как ai_context): файл `meeting_series.json` в каталоге
админа команды или таблица `meeting_series` в Postgres. Не секрет — не
шифруется, удобно править руками.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from . import db, security

# Ограничения — чтобы память серии не съела бюджет промпта.
_MAX_CONTEXT = 8_000          # текст карточки серии (пишет человек)
_MAX_SUMMARY = 700            # «о чём была прошлая встреча»
_MAX_LAST_DECISIONS = 15
_MAX_LAST_TASKS = 25
_MAX_ITEM = 220               # одна строка решения/задачи в памяти
_MAX_HISTORY = 12             # сколько прошлых встреч помнить в списке
_MAX_SERIES = 300

PRIORITIES = ("normal", "urgent", "record_only")

_LOCK = threading.Lock()

# «28.08.2026, 16-00. - », «2026-08-28 16:00 — », «28.08.2026 - »
_DATE_PREFIX_RE = re.compile(
    r"^\s*(?:\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}|\d{4}-\d{2}-\d{2})"
    r"[,\s]*(?:\d{1,2}[:\-.]\d{2})?\.?\s*[-–—]?\s*")
_EXT_RE = re.compile(r"\.(mp4|mkv|webm|mov|avi|mp3|wav|m4a|ogg|opus|flac|aac|docx|txt)$",
                     re.IGNORECASE)
_PROTOCOL_SUFFIX_RE = re.compile(r"\s*[-–—]\s*(протокол|расшифровка)\s*$", re.IGNORECASE)
# Хвост, который планировщик дописывает при ПОВТОРНОМ заходе в ту же комнату
# (см. scheduler._free_path): «…Встреча лидеров (2).mp4».
_COPY_SUFFIX_RE = re.compile(r"\s*\(\d+\)\s*$")


def series_key(title: str | None) -> str:
    """Нормализованный ключ серии по названию встречи / имени файла.

    >>> series_key("28.08.2026, 16-00. - Встреча лидеров.mp4")
    'встреча лидеров'
    >>> series_key("ОД сайт, редизайн, магазин - проектная встреча - протокол.docx")
    'од сайт редизайн магазин проектная встреча'
    """
    s = str(title or "").strip()
    s = _EXT_RE.sub("", s)
    s = _COPY_SUFFIX_RE.sub("", s)
    s = _DATE_PREFIX_RE.sub("", s)
    s = _PROTOCOL_SUFFIX_RE.sub("", s)
    s = s.lower().replace("ё", "е")
    s = re.sub(r"[^\w]+", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def display_title(title: str | None) -> str:
    """Название серии для показа: без даты/времени и расширения, с регистром."""
    s = str(title or "").strip()
    s = _EXT_RE.sub("", s)
    s = _COPY_SUFFIX_RE.sub("", s)
    s = _DATE_PREFIX_RE.sub("", s)
    s = _PROTOCOL_SUFFIX_RE.sub("", s)
    return s.strip(" -–—.") or str(title or "").strip()


_DATE_IN_TITLE_RE = re.compile(r"(?<!\d)(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4}|\d{2})(?!\d)")
_ISO_DATE_IN_TITLE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")


def date_from_title(title: str | None) -> str:
    """«28.08.2026, 16-00. - …» → «28.08.2026»; '' если даты в названии нет."""
    s = str(title or "")
    m = _ISO_DATE_IN_TITLE_RE.search(s)
    if m:
        y, mo, d = m.groups()
        return f"{int(d):02d}.{int(mo):02d}.{y}"
    m = _DATE_IN_TITLE_RE.search(s)
    if not m:
        return ""
    d, mo, y = m.groups()
    if len(y) == 2:
        y = "20" + y
    return f"{int(d):02d}.{int(mo):02d}.{y}"


# --------------------------------------------------------------------------- #
# Хранение
# --------------------------------------------------------------------------- #
def _path(team: str) -> Path:
    return security.user_dir(team) / "meeting_series.json"


def _empty_entry(title: str = "") -> dict:
    return {
        "title": title,          # отображаемое название (как в Weeek, без даты)
        "context": "",           # карточка серии — пишет человек
        "preset": "",            # закреплённый тип протокола ('' = авто по названию)
        "priority": "normal",    # normal | urgent | record_only
        "weeek_project_id": None,  # куда заводить задачи из протокола этой серии
        "last": None,            # память о прошлой встрече (см. remember)
        "history": [],           # [{date, job_id, tasks, decisions}] — последние
        "updated_at": 0.0,
    }


def _clean_entry(raw: dict, key: str) -> dict:
    e = _empty_entry()
    if not isinstance(raw, dict):
        return e
    e["title"] = str(raw.get("title") or "").strip()[:200] or key
    e["context"] = str(raw.get("context") or "").strip()[:_MAX_CONTEXT]
    e["preset"] = str(raw.get("preset") or "").strip()[:60]
    pr = str(raw.get("priority") or "normal").strip()
    e["priority"] = pr if pr in PRIORITIES else "normal"
    wp = raw.get("weeek_project_id")
    e["weeek_project_id"] = (str(wp).strip()[:40] or None) if wp not in (None, "") else None
    last = raw.get("last")
    e["last"] = last if isinstance(last, dict) else None
    hist = raw.get("history")
    e["history"] = [h for h in hist if isinstance(h, dict)][-_MAX_HISTORY:] \
        if isinstance(hist, list) else []
    try:
        e["updated_at"] = float(raw.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        e["updated_at"] = 0.0
    return e


def _read(team: str) -> dict:
    if db.enabled():
        raw = db.series_load(team) or {}
    else:
        try:
            raw = json.loads(_path(team).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {k: _clean_entry(v, k) for k, v in raw.items() if isinstance(k, str) and k}


def _write(team: str, data: dict) -> None:
    if len(data) > _MAX_SERIES:
        # Самые старые по updated_at — на выход (серии, которых давно нет).
        keep = sorted(data.items(), key=lambda kv: kv[1].get("updated_at") or 0.0)
        data = dict(keep[-_MAX_SERIES:])
    if db.enabled():
        db.series_save(team, data)
        return
    p = _path(team)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def load(user: str) -> dict:
    """Все серии команды: {key: entry}."""
    with _LOCK:
        return _read(security.team_of(user))


# --------------------------------------------------------------------------- #
# Поиск серии по названию
# --------------------------------------------------------------------------- #
def _match_key(data: dict, key: str) -> str | None:
    """Точное совпадение, иначе — ближайший родственник: «встреча лидеров
    стратегия» наследует карточку «встреча лидеров» (и наоборот), если один
    ключ начинается с другого по границе слова. Берётся самый длинный."""
    if not key:
        return None
    if key in data:
        return key
    best = None
    for k in data:
        if k.startswith(key + " ") or key.startswith(k + " "):
            if best is None or len(k) > len(best):
                best = k
    return best


def find(user: str, title: str | None) -> tuple[str, dict | None]:
    """(ключ серии, карточка или None). Ключ возвращается всегда — под ним
    серия и будет создана при первом протоколе."""
    key = series_key(title)
    data = load(user)
    hit = _match_key(data, key)
    return (hit or key), (data.get(hit) if hit else None)


def list_for_ui(user: str) -> list[dict]:
    """Серии для интерфейса: карточка + краткая память, отсортировано по
    свежести."""
    data = load(user)
    out = []
    for key, e in data.items():
        last = e.get("last") or {}
        out.append({
            "key": key,
            "title": e.get("title") or key,
            "context": e.get("context", ""),
            "preset": e.get("preset", ""),
            "priority": e.get("priority", "normal"),
            "weeek_project_id": e.get("weeek_project_id"),
            "last_date": last.get("date", ""),
            "last_job_id": last.get("job_id", ""),
            "last_summary": last.get("summary", ""),
            "open_tasks": len(last.get("tasks") or []),
            "meetings": len(e.get("history") or []),
            "updated_at": e.get("updated_at", 0.0),
        })
    out.sort(key=lambda x: -(x["updated_at"] or 0.0))
    return out


# --------------------------------------------------------------------------- #
# Правка карточки (интерфейс)
# --------------------------------------------------------------------------- #
def upsert(user: str, key: str, fields: dict) -> dict:
    """Создать/обновить карточку серии. `key` — ключ серии или любое название
    (нормализуется здесь). Память о прошлой встрече не трогается."""
    key = series_key(key)
    if not key:
        raise ValueError("Пустое название серии.")
    team = security.team_of(user)
    with _LOCK:
        data = _read(team)
        e = data.get(key) or _empty_entry(key)
        f = fields or {}
        if "title" in f:
            e["title"] = str(f.get("title") or "").strip()[:200] or e["title"] or key
        if "context" in f:
            e["context"] = str(f.get("context") or "").strip()[:_MAX_CONTEXT]
        if "preset" in f:
            e["preset"] = str(f.get("preset") or "").strip()[:60]
        if "priority" in f:
            pr = str(f.get("priority") or "normal").strip()
            e["priority"] = pr if pr in PRIORITIES else "normal"
        if "weeek_project_id" in f:
            wp = f.get("weeek_project_id")
            e["weeek_project_id"] = (str(wp).strip()[:40] or None) if wp not in (None, "") else None
        e["updated_at"] = time.time()
        data[key] = e
        _write(team, data)
    return {"key": key, **e}


def delete(user: str, key: str) -> bool:
    team = security.team_of(user)
    with _LOCK:
        data = _read(team)
        if key not in data:
            return False
        del data[key]
        _write(team, data)
    return True


def forget_last(user: str, key: str) -> bool:
    """Стереть память о прошлой встрече (карточку оставить) — если протокол
    прошлой встречи был ошибочным и его не надо тянуть дальше."""
    team = security.team_of(user)
    with _LOCK:
        data = _read(team)
        e = data.get(key)
        if not e:
            return False
        e["last"] = None
        e["updated_at"] = time.time()
        _write(team, data)
    return True


# --------------------------------------------------------------------------- #
# Память о прошлой встрече — заполняется после сборки протокола
# --------------------------------------------------------------------------- #
def _task_text(item) -> tuple[str, str]:
    if isinstance(item, dict):
        return (str(item.get("task") or "").strip(), str(item.get("owner") or "").strip())
    return (str(item or "").strip(), "")


def _verified(analysis: dict, key: str, idx: int) -> bool | None:
    """True/False по grounding-проверке, None если проверки не было."""
    try:
        rec = (analysis.get("verification") or {})[key][idx]
    except (KeyError, IndexError, TypeError):
        return None
    return bool(rec.get("ok")) if isinstance(rec, dict) else None


def remember(user: str, title: str | None, analysis: dict | None,
             job_id: str = "", date: str = "") -> str | None:
    """Сохранить компактную память о встрече серии. Возвращает ключ серии.

    В память идут только пункты С ПОДТВЕРЖДЕНИЕМ (если проверка была): иначе
    выдуманная задача переехала бы в контекст следующей встречи и там уже
    выглядела бы фактом. Пересборка того же протокола (тот же job_id) память
    перезаписывает, а не добавляет вторую запись в историю.
    """
    if not analysis or not isinstance(analysis, dict):
        return None
    key = series_key(title)
    if not key:
        return None
    date = date or date_from_title(title) or time.strftime("%d.%m.%Y")

    decisions: list[str] = []
    for i, d in enumerate(analysis.get("decisions") or []):
        txt = str(d or "").strip()
        if txt and _verified(analysis, "decisions", i) is not False:
            decisions.append(txt[:_MAX_ITEM])
    tasks: list[dict] = []
    for lst in ("tasks", "minor_tasks"):
        for i, item in enumerate(analysis.get(lst) or []):
            txt, owner = _task_text(item)
            if txt and _verified(analysis, lst, i) is not False:
                tasks.append({"task": txt[:_MAX_ITEM],
                              "owner": owner if owner and owner != "—" else ""})
    done: list[str] = []
    for i, item in enumerate(analysis.get("done_tasks") or []):
        txt, _o = _task_text(item)
        if txt and _verified(analysis, "done_tasks", i) is not False:
            done.append(txt[:_MAX_ITEM])

    last = {
        "date": date,
        "job_id": job_id or "",
        "summary": str(analysis.get("summary") or "").strip()[:_MAX_SUMMARY],
        "decisions": decisions[:_MAX_LAST_DECISIONS],
        "tasks": tasks[:_MAX_LAST_TASKS],
        "done": done[:_MAX_LAST_TASKS],
        "participants": [
            str(p.get("name") or "").strip() for p in (analysis.get("participants") or [])
            if isinstance(p, dict) and (p.get("name") or "").strip()][:30],
    }

    team = security.team_of(user)
    with _LOCK:
        data = _read(team)
        hit = _match_key(data, key) or key
        e = data.get(hit) or _empty_entry(display_title(title))
        if not e.get("title"):
            e["title"] = display_title(title)
        # История: одна запись на протокол (пересборка перезаписывает).
        hist = [h for h in (e.get("history") or []) if h.get("job_id") != (job_id or None)]
        hist.append({"date": date, "job_id": job_id or "",
                     "tasks": len(tasks), "decisions": len(decisions)})
        e["history"] = hist[-_MAX_HISTORY:]
        # «Прошлая» — самая поздняя по дате; пересборка старого протокола не
        # должна затирать память о более свежей встрече.
        prev = e.get("last") or {}
        if (not prev or prev.get("job_id") == (job_id or None)
                or _date_key(date) >= _date_key(prev.get("date", ""))):
            e["last"] = last
        e["updated_at"] = time.time()
        data[hit] = e
        _write(team, data)
    return hit


def _date_key(d: str) -> tuple:
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", d or "")
    return (int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else (0, 0, 0)


# --------------------------------------------------------------------------- #
# Блок для промпта
# --------------------------------------------------------------------------- #
SERIES_HEADER = "=== КОНТЕКСТ СЕРИИ ВСТРЕЧ"
LAST_HEADER = "=== ПРОШЛАЯ ВСТРЕЧА ЭТОЙ СЕРИИ"


def block_for(user: str, title: str | None) -> str:
    """Текст, который дописывается к входу анализа: карточка серии (если
    человек её заполнил) и память о прошлой встрече (если она была).
    Пустая строка, если серия неизвестна."""
    key, e = find(user, title)
    if not e:
        return ""
    parts: list[str] = []
    ctx = (e.get("context") or "").strip()
    if ctx:
        parts.append(
            f"{SERIES_HEADER} «{e.get('title') or key}» (заполнено человеком: проект, "
            "участники и роли, цели, термины — используй для понимания, НЕ переноси "
            "в решения/задачи) ===\n" + ctx)
    last = e.get("last") or {}
    if last and (last.get("decisions") or last.get("tasks") or last.get("summary")):
        lines = [f"{LAST_HEADER} ({last.get('date') or '?'}) — только справка о том, "
                 "на чём остановились; НЕ повторяй эти пункты как решения/задачи ЭТОЙ "
                 "встречи, если их не обсуждали заново. Если на этой встрече сказали, "
                 "что задача из списка сделана — отметь её в done_tasks ==="]
        if last.get("summary"):
            lines.append("О чём была: " + last["summary"])
        if last.get("decisions"):
            lines.append("Решили тогда:")
            lines += [f"- {d}" for d in last["decisions"]]
        if last.get("tasks"):
            lines.append("Задачи, поставленные тогда (проверь статус по разговору):")
            lines += [f"- {t['task']}" + (f" — {t['owner']}" if t.get("owner") else "")
                      for t in last["tasks"]]
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


_NAME_PAIR = re.compile(r"\b([А-ЯЁA-Z][а-яёa-z]{2,})\s+([А-ЯЁA-Z][а-яёa-z]{1,})\b")


def known_names(user: str, title: str | None) -> list[str]:
    """Имена участников из карточки серии и памяти о прошлой встрече — для
    канонизации подписей плиток и подсказки Whisper."""
    _key, e = find(user, title)
    if not e:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def add(n: str) -> None:
        k = n.lower()
        if k not in seen:
            seen.add(k)
            out.append(n)

    for m in _NAME_PAIR.finditer(e.get("context") or ""):
        add(f"{m.group(1)} {m.group(2)}")
    for n in ((e.get("last") or {}).get("participants") or []):
        if n:
            add(str(n))
    return out[:60]


def pinned_preset(user: str, title: str | None) -> str:
    """Закреплённый в карточке серии тип протокола ('' = не закреплён)."""
    _key, e = find(user, title)
    return str((e or {}).get("preset") or "")


def priority_of(user: str, title: str | None) -> str:
    _key, e = find(user, title)
    return str((e or {}).get("priority") or "normal")
