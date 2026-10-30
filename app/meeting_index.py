"""Куски встреч для поиска и вопросов ПО ВСЕМ встречам команды (30.10.2026).

Откуда взялось. Полнотекстовый индекс `search_docs` стирается ретеншном
вместе с задачей (`jobs._purge_old`, `VTX_RETENTION_HOURS`=24): «поиск по всем
встречам» на деле покрывал сутки, а базы встреч в Postgres не было — только
архив протоколов в облаке. Между тем у команды сотни протоколов, и вопросы
вида «что решили по ОД за месяц?» или «когда обсуждали бюджет сайта?» спросить
было не у кого: `ask_meeting` отвечает по ОДНОЙ записи.

Что здесь. Встреча режется на куски: расшифровка — окнами по 40 строк с
перекрытием (как ретривер в `ask_meeting`), протокол — по разделам (о чём,
решения, задачи, темы). Куски лежат в таблице `meeting_chunks` и ПЕРЕЖИВАЮТ
ретеншн — как `meeting_stats`: удаляются только с удалением аккаунта команды
(`db.delete_user_data`) или при переиндексации того же документа.

Два источника кусков: задача распознавания (`index_job`, зовётся из
`jobs.index_search` при каждой (пере)сборке) и архив готовых протоколов .docx
из облака (`parse_protocol_docx` + `scripts/index_archive.py`). Текст задачи в
куски попадает после `redact` — секреты уже замаскированы.

Изоляция: каждый кусок несёт `team` (логин админа команды), и любой запрос к
таблице фильтруется по нему — как `search_docs`.
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from . import logs

log = logs.get("vtx.meeting_index")

KIND_TRANSCRIPT = "transcript"
KIND_PROTOCOL = "protocol"

CHUNK_LINES = 40            # окно расшифровки, как в analyze.ask_meeting
CHUNK_STEP = 20             # шаг — половина окна (перекрытие)
_MAX_CHUNK_CHARS = 6_000    # страховка от строк-простыней
_MAX_SECTION_CHARS = 4_000  # раздел протокола в одном куске
_MAX_TRANSCRIPT_CHARS = 1_500_000

# «[12:34]», «[01:02:03]», «[123:45]» — метки формата formats.to_txt и docx.
_TS_RE = re.compile(r"\[(\d{1,3}:\d{2}(?::\d{2})?)\]")

ARCHIVE_PREFIX = "archive:"


def enabled() -> bool:
    """Память встреч живёт только в Postgres (в файловом режиме её нет).
    Отдельная функция, а не db.enabled(): её подменяют тесты, не трогая
    остальную базу."""
    from . import db
    return db.enabled()


# --------------------------------------------------------------------------- #
# Нарезка
# --------------------------------------------------------------------------- #
def _first_ts(text: str) -> str:
    m = _TS_RE.search(text)
    return m.group(1) if m else ""


def chunk_transcript(text: str, win: int = CHUNK_LINES,
                     step: int = CHUNK_STEP) -> list[dict]:
    """Окна расшифровки: [{idx, ts, text}]. `ts` — первая метка времени окна
    (по ней ответ ссылается на место в записи)."""
    lines = [ln for ln in (text or "")[:_MAX_TRANSCRIPT_CHARS].splitlines()]
    out: list[dict] = []
    if not any(ln.strip() for ln in lines):
        return out
    idx = 0
    for i in range(0, max(len(lines), 1), step):
        chunk = "\n".join(lines[i:i + win]).strip()
        if not chunk:
            continue
        out.append({"idx": idx, "ts": _first_ts(chunk), "text": chunk[:_MAX_CHUNK_CHARS]})
        idx += 1
        if i + win >= len(lines):
            break
    return out


def _task_line(item) -> str:
    if isinstance(item, dict):
        txt = str(item.get("task") or "").strip()
        owner = str(item.get("owner") or "").strip()
        due = str(item.get("due") or "").strip()
        if owner and owner != "—":
            txt += f" — {owner}"
        if due:
            txt += f" (срок: {due})"
        return txt
    return str(item or "").strip()


def protocol_chunks(analysis: dict | None) -> list[dict]:
    """Разделы протокола как отдельные куски: [{idx, ts, text}].
    Порядок — о чём, решения, задачи, сделано, статусы, темы."""
    a = analysis if isinstance(analysis, dict) else {}
    sections: list[str] = []
    summary = str(a.get("summary") or "").strip()
    if summary:
        sections.append("О чём шёл разговор: " + summary)
    decisions = [str(d).strip() for d in (a.get("decisions") or []) if str(d or "").strip()]
    if decisions:
        sections.append("Решения и договорённости:\n" + "\n".join(f"- {d}" for d in decisions))
    tasks = [_task_line(t) for t in (a.get("tasks") or [])] + \
            [_task_line(t) for t in (a.get("minor_tasks") or [])]
    tasks = [t for t in tasks if t]
    if tasks:
        sections.append("Задачи:\n" + "\n".join(f"- {t}" for t in tasks))
    done = [_task_line(t) for t in (a.get("done_tasks") or [])]
    done = [t for t in done if t]
    if done:
        sections.append("Сделано:\n" + "\n".join(f"- {t}" for t in done))
    statuses = [st for st in (a.get("statuses") or []) if isinstance(st, dict) and st.get("item")]
    if statuses:
        sections.append("Что спрашивали и что ответили:\n" + "\n".join(
            f"- {st.get('item')}: {st.get('status') or '—'}"
            + (f" ({st.get('note')})" if st.get("note") else "") for st in statuses))
    for d in (a.get("detailed") or []):
        if isinstance(d, dict) and (d.get("topic") or d.get("details")):
            sections.append(f"Тема: {str(d.get('topic') or '').strip()}\n"
                            f"{str(d.get('details') or '').strip()}")
    return [{"idx": i, "ts": "", "text": s[:_MAX_SECTION_CHARS]}
            for i, s in enumerate(sections)]


def doc_rows(doc_id: str, team: str, *, title: str, series_key: str,
             meeting_date: str, meeting_ts: float | None,
             transcript: str = "", analysis: dict | None = None,
             is_draft: bool = False, source: str = "") -> list[dict]:
    """Строки таблицы `meeting_chunks` для одного документа (встречи)."""
    now = time.time()
    rows: list[dict] = []
    for kind, chunks in ((KIND_PROTOCOL, protocol_chunks(analysis)),
                         (KIND_TRANSCRIPT, chunk_transcript(transcript))):
        for c in chunks:
            rows.append({
                "id": f"{doc_id}:{kind[0]}{c['idx']}",
                "doc_id": doc_id, "team": team, "series_key": series_key or "",
                "title": title or "", "meeting_date": meeting_date or "",
                "meeting_ts": float(meeting_ts) if meeting_ts else None,
                "kind": kind, "idx": int(c["idx"]), "ts": c.get("ts") or "",
                "text": c["text"], "is_draft": bool(is_draft),
                "source": source or "", "created_at": now,
            })
    return rows


def meeting_ts_of(meeting_date: str, fallback: float | None = None) -> float | None:
    """«дд.мм.гггг» → unix-время полудня того дня (для сортировки)."""
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", meeting_date or "")
    if not m:
        return fallback
    d, mo, y = (int(x) for x in m.groups())
    try:
        return time.mktime((y, mo, d, 12, 0, 0, 0, 0, -1))
    except (OverflowError, ValueError):
        return fallback


def archive_doc_id(team: str, source: str) -> str:
    """Устойчивый id документа архива: по команде и пути файла в облаке —
    повторный прогон индексатора ЗАМЕНЯЕТ куски, а не плодит вторые."""
    h = hashlib.sha1(f"{team}|{source}".encode("utf-8")).hexdigest()[:16]
    return ARCHIVE_PREFIX + h


# --------------------------------------------------------------------------- #
# Задача распознавания → куски
# --------------------------------------------------------------------------- #
def index_job(store, job) -> int:
    """Положить куски задачи в таблицу (замена прежних того же job.id).
    Возвращает число кусков; 0 — индексировать нечего или база выключена."""
    from . import db, meeting_series, security
    if not db.enabled():
        return 0
    txt_path = store.result_path(job.id, "txt")
    transcript = txt_path.read_text(encoding="utf-8") if txt_path.exists() else ""
    analysis = job.analysis if isinstance(job.analysis, dict) else None
    if not transcript.strip() and not analysis:
        return 0
    team = security.team_of(job.owner)
    title = meeting_series.display_title(Path(job.filename).stem) or Path(job.filename).stem
    date = meeting_series.meeting_day(job.filename, job.created_at)
    fb = (analysis or {}).get("_fallback")
    rows = doc_rows(job.id, team, title=title,
                    series_key=meeting_series.series_key(job.filename),
                    meeting_date=date,
                    meeting_ts=meeting_ts_of(date, job.created_at),
                    transcript=transcript, analysis=analysis,
                    is_draft=isinstance(fb, list) and bool(fb),
                    source=f"job:{job.id}")
    db.chunks_replace(job.id, rows)
    return len(rows)


# --------------------------------------------------------------------------- #
# Архив: протокол .docx → структура
# --------------------------------------------------------------------------- #
# Заголовки разделов — ровно те, что пишет docx_export.generate_report.
_H_PARTICIPANTS = "Участники"
_H_SUMMARY = "О чём шёл разговор"
_H_DECISIONS = "Решения и договорённости"
_H_TASKS = "Задачи (нужно сделать)"
_H_TASKS_NO_OWNER = "Задачи без ответственного"
_H_UNVERIFIED = "Требуют проверки"
_H_MINOR = "Мелкие задачи и доработки"
_H_DONE = "Сделано (выполненные задачи)"
_H_STATUSES = "Что спрашивали и что ответили"
_H_TOPICS = "Подробный разбор"
_H_TRANSCRIPT = "Полная транскрипция"
_DRAFT_MARK = "ЧЕРНОВИК"
_META_DATE_RE = re.compile(r"Дата:\s*(\d{2}\.\d{2}\.\d{4})")
_BULLET_RE = re.compile(r"^[☐☑•\-–—]\s*")
# «Собрать макет  — Наталья П» — исполнителя docx_export дописывает через
# два пробела и тире; «(срок: …)» стоит перед ним.
_OWNER_SUFFIX_RE = re.compile(r"\s{2,}—\s*(.+?)\s*$")
_DUE_RE = re.compile(r"\s*\(срок:\s*([^)]*)\)")


def _split_task(text: str) -> dict:
    t = _BULLET_RE.sub("", text).strip()
    owner = ""
    m = _OWNER_SUFFIX_RE.search(t)
    if m:
        owner, t = m.group(1).strip(), t[:m.start()].strip()
    due = ""
    m = _DUE_RE.search(t)
    if m:
        due, t = m.group(1).strip(), (t[:m.start()] + t[m.end():]).strip()
    return {"task": t, "owner": "" if owner == "—" else owner, "due": due}


def _iter_body(doc):
    """Абзацы и таблицы документа В ПОРЯДКЕ ДОКУМЕНТА (python-docx отдаёт
    их двумя раздельными списками)."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            yield Paragraph(child, doc)
        elif tag == "tbl":
            yield Table(child, doc)


def _para_text(p) -> str:
    # <w:br/> внутри абзаца python-docx отдаёт как «\n» — переносы сохраняем.
    return (p.text or "").replace("\r", "").strip()


def _heading_level(p) -> int | None:
    name = (p.style.name if p.style is not None else "") or ""
    if name == "Title":
        return 0
    m = re.match(r"Heading (\d)", name)
    return int(m.group(1)) if m else None


def parse_protocol_docx(path: str | Path) -> dict:
    """Структура протокола из .docx, собранного `docx_export`:
    {title, is_draft, date, participants[], summary, decisions[], tasks[{task,
    owner, due}], done[], topics[{topic, details}], transcript}.
    Разделы, которых в файле нет, — пустые. Чужой документ (не наш экспорт)
    даёт пустую структуру с `transcript` = весь текст."""
    from docx import Document
    from docx.table import Table
    doc = Document(str(path))
    out = {"title": "", "is_draft": False, "date": "", "participants": [],
           "summary": "", "decisions": [], "tasks": [], "done": [],
           "topics": [], "transcript": "", "ours": False}
    section = ""
    topic: dict | None = None
    transcript_lines: list[str] = []
    summary_parts: list[str] = []
    subtitle_seen = False
    for el in _iter_body(doc):
        if isinstance(el, Table):
            if section == _H_TASKS:
                for ri, row in enumerate(el.rows):
                    cells = [c.text.strip() for c in row.cells]
                    if ri == 0 or len(cells) < 2:
                        continue
                    task = cells[1] if len(cells) > 1 else ""
                    owner = cells[2] if len(cells) > 2 else ""
                    due = cells[3] if len(cells) > 3 else ""
                    if task:
                        out["tasks"].append({"task": task,
                                             "owner": "" if owner == "—" else owner,
                                             "due": "" if due == "—" else due})
            continue
        text = _para_text(el)
        if not text:
            continue
        lvl = _heading_level(el)
        if lvl == 0:
            out["ours"] = text.startswith("Протокол встречи")
            out["is_draft"] = _DRAFT_MARK in text.upper()
            section = "title"
            continue
        if lvl == 1:
            section = text
            topic = None
            continue
        if lvl == 2 and section == _H_TOPICS:
            topic = {"topic": text, "details": ""}
            out["topics"].append(topic)
            continue
        if section == "title":
            if not subtitle_seen:
                out["title"] = text
                subtitle_seen = True
            else:
                m = _META_DATE_RE.search(text)
                if m and not out["date"]:
                    out["date"] = m.group(1)
            continue
        if section == _H_PARTICIPANTS:
            if text.startswith("Серия встреч:"):
                continue
            out["participants"].append(text)
        elif section == _H_SUMMARY:
            summary_parts.append(text)
        elif section == _H_DECISIONS:
            out["decisions"].append(_BULLET_RE.sub("", text))
        elif section.startswith(_H_TASKS_NO_OWNER) or section == _H_MINOR \
                or section == _H_UNVERIFIED:
            t = _split_task(text)
            if t["task"] and not t["task"].startswith("Эти задачи") \
                    and not t["task"].startswith("Пункты"):
                out["tasks"].append(t)
        elif section == _H_DONE:
            out["done"].append(_split_task(text))
        elif section == _H_TOPICS and topic is not None:
            topic["details"] = (topic["details"] + "\n" + text).strip()
        elif section == _H_TRANSCRIPT:
            if text != "Транскрипция недоступна.":
                transcript_lines.append(re.sub(r"\]\s{2,}", "] ", text))
    out["summary"] = "\n".join(summary_parts).strip()
    out["transcript"] = "\n".join(transcript_lines)
    if not out["ours"]:
        out["transcript"] = "\n".join(
            _para_text(p) for p in doc.paragraphs if _para_text(p))
    return out


def analysis_from_parsed(parsed: dict) -> dict:
    """Структура разобранного .docx в виде `analysis` для protocol_chunks."""
    return {
        "summary": parsed.get("summary") or "",
        "decisions": list(parsed.get("decisions") or []),
        "tasks": [{"task": t.get("task"), "owner": t.get("owner"), "due": t.get("due")}
                  for t in (parsed.get("tasks") or [])],
        "done_tasks": [{"task": d.get("task"), "owner": d.get("owner")}
                       for d in (parsed.get("done") or [])],
        "detailed": list(parsed.get("topics") or []),
    }
