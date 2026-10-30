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


# --------------------------------------------------------------------------- #
# Похожие обсуждения на прошлых встречах — справка для сборки протокола
# --------------------------------------------------------------------------- #
# Фаза 2 плана RAG (п. 7): до этого в промпт шли только ПОСЛЕДНИЕ встречи
# серии по дате («раньше в этой серии»). Если бюджет сайта обсуждали два
# месяца назад или на встрече ДРУГОЙ серии, модель об этом не знала. Здесь
# прошлые протоколы подбираются ПО СОДЕРЖАНИЮ текущей встречи: по темам из
# заметок карты (длинная встреча) или по ключевым словам расшифровки
# (короткая, где заметок ещё нет). Бюджет жёсткий — это справка, а не вторая
# расшифровка: лишний килобайт в сведении стоит денег на каждой встрече.
RELATED_HEADER = "=== ПОХОЖИЕ ОБСУЖДЕНИЯ НА ПРОШЛЫХ ВСТРЕЧАХ"
_RELATED_MAX_DOCS = 4          # встреч в блоке — больше модель не свяжет
_RELATED_PER_DOC = 2           # кусков на встречу
_RELATED_SNIPPET = 400         # символов куска в строке блока
_RELATED_MAX_QUERIES = 8       # запросов к базе на одну сборку
_RELATED_SEARCH_LIMIT = 8      # кусков на один запрос
_RELATED_MIN_SERIES_DOCS = 2   # меньше встреч своей серии — ищем по всей команде

# Слова-паразиты устной речи: длинные, частые и ни о чём. Без этого списка
# «наверное» и «соответственно» вытесняли из ключевых слов названия проектов.
_SPEECH_FILLERS = frozenset("""
наверное поэтому сейчас вообще например потому можно нужно просто давайте
смотрите смотри понятно получается соответственно короче допустим собственно
конечно именно практически естественно действительно наверно какой-то
какие-то какую-то какая-то который которые которая которую сегодня завтра
вчера хорошо спасибо пожалуйста давай ладно правильно неправильно насчет
насчёт вопрос вопросы момент моменты человек люди ребята коллеги минуту
секунду слушай слушайте скажи скажите думаю думаем кажется честно
нормально понял поняла поняли знаю знаете знаем будет будут будем будете
может можем могут хочу хотим хотят также тогда всегда никогда опять снова
только очень много мало больше меньше дальше потом после перед между
""".split())
_LATIN_WORD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{1,}$")
_CYR_WORD_RE = re.compile(r"^[А-Яа-яЁё][А-Яа-яЁё\-]+$")
_CAPITAL_RE = re.compile(r"^[А-ЯЁ][а-яё]{2,}$")
# «500 тысяч», «3 млн», «20 %», «две недели» не ловим — только цифры с
# единицей: именно такие числа запоминаются и отличают обсуждение.
_NUM_UNIT_RE = re.compile(
    r"(?<![\d:\[])(\d{1,3}(?:[ \u00a0]?\d{3})*(?:[.,]\d+)?)\s?"
    r"(тыс\w*|млн\w*|млрд\w*|руб\w*|₽|%|процент\w*|час(?:а|ов)?|дн(?:я|ей)|"
    r"недел\w*|мес(?:яц\w*)?|штук\w*|шт\.?|человек\w*|\$|€|евро|доллар\w*)(?![\w:])",
    re.IGNORECASE)
_SENT_END_RE = re.compile(r"[.!?…]$")
_MIN_LONG_WORD = 7


def salient_queries(transcript: str, limit: int = 6) -> list[str]:
    """Ключевые слова встречи БЕЗ модели — для короткой встречи, где заметок
    карты ещё нет. Кандидаты: латиница (названия систем — Weeek, CRM, API),
    слова с заглавной буквы не в начале предложения (имена, проекты), числа с
    единицей («500 тысяч»), длинные слова. Ранжирование по частоте с весом
    за вид кандидата; служебные блоки, таймкоды, подписи говорящих, стоп-слова
    и слова-паразиты не считаются."""
    from .analyze import _ASK_GENERIC, _ASK_STOPWORDS, _INJECTED_BLOCK_RE, _strip_labels
    text = _strip_labels(_INJECTED_BLOCK_RE.sub(" ", transcript or ""))
    scores: dict[str, float] = {}
    shown: dict[str, str] = {}

    def add(key: str, weight: float, show: str) -> None:
        scores[key] = scores.get(key, 0.0) + weight
        shown.setdefault(key, show)

    for m in _NUM_UNIT_RE.finditer(text):
        phrase = re.sub(r"\s+", " ", m.group(0)).strip()
        add(phrase.lower(), 2.0, phrase)
    for line in text.splitlines():
        prev = ""
        for raw in line.split():
            w = raw.strip(".,;:!?()[]«»\"'“”—–-")
            if not w:
                prev = raw
                continue
            low = w.lower().replace("ё", "е")
            if low in _ASK_STOPWORDS or low in _ASK_GENERIC or low in _SPEECH_FILLERS:
                prev = raw
                continue
            if _LATIN_WORD_RE.match(w) and len(w) >= 2:
                add(low, 3.0, w)
            elif _CYR_WORD_RE.match(w):
                mid = bool(prev) and not _SENT_END_RE.search(prev)
                if _CAPITAL_RE.match(w) and mid:
                    add(low, 2.0, w)
                elif len(low) >= _MIN_LONG_WORD:
                    add(low, 1.0, low)
            prev = raw
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [shown[k] for k, _s in ranked[:max(int(limit), 0)]]


def _fts_query(phrase: str) -> str:
    """Формулировка темы → запрос к полнотекстовому индексу по образцу
    analyze._fts_queries: значимые слова через «or» («Бюджет сайта и сроки
    редизайна» → «бюджет or сайта or сроки or редизайна»)."""
    from .analyze import _ASK_GENERIC, _ASK_STOPWORDS, _norm_for_match
    words = [w for w in _norm_for_match(phrase).split()
             if w not in _ASK_STOPWORDS and w not in _ASK_GENERIC
             and w not in _SPEECH_FILLERS and len(w) >= 2]
    return " or ".join(dict.fromkeys(words))


def _related_rows(db, team: str, series_key: str, q: str,
                  exclude_doc: str | None) -> list[dict]:
    """Куски протоколов под один запрос: сначала своя серия, потом вся
    команда, если в серии нашлось меньше двух встреч (тема могла звучать на
    встрече другой серии — «бюджет сайта» обсуждают и лидеры, и проект)."""
    rows = []
    if series_key:
        rows = db.chunks_search(team, q, limit=_RELATED_SEARCH_LIMIT,
                                series_key=series_key, kinds=(KIND_PROTOCOL,))
        rows = [r for r in rows if r.get("doc_id") != exclude_doc]
    if len({r.get("doc_id") for r in rows}) < _RELATED_MIN_SERIES_DOCS:
        more = db.chunks_search(team, q, limit=_RELATED_SEARCH_LIMIT,
                                kinds=(KIND_PROTOCOL,))
        rows += [r for r in more if r.get("doc_id") != exclude_doc]
    return rows


def related_block(team: str, series_key: str, queries: list[str],
                  exclude_doc: str | None = None, budget_chars: int = 2500) -> str:
    """Блок «ПОХОЖИЕ ОБСУЖДЕНИЯ НА ПРОШЛЫХ ВСТРЕЧАХ» для промпта сборки.

    По каждой формулировке — поиск по протоколам команды; результаты
    сливаются: кусок, найденный несколькими запросами, выше; при равном
    ранге свежая встреча предпочтительнее. Не больше `_RELATED_MAX_DOCS`
    встреч, `_RELATED_PER_DOC` кусков на встречу, текущая встреча
    (`exclude_doc`) исключается. Пусто → "". Любая ошибка базы → "" и
    предупреждение в лог: справка не должна ронять протокол."""
    from . import db
    qs: list[str] = []
    for raw in queries or []:
        q = _fts_query(str(raw or ""))
        if q and q not in qs:
            qs.append(q)
    qs = qs[:_RELATED_MAX_QUERIES]
    if not qs or not team:
        return ""
    # (doc_id, idx) → {row, hits, rank}
    found: dict[tuple, dict] = {}
    try:
        for q in qs:
            for r in _related_rows(db, team, series_key or "", q, exclude_doc):
                key = (r.get("doc_id"), int(r.get("idx") or 0))
                rec = found.setdefault(key, {"row": r, "hits": 0, "rank": 0.0})
                rec["hits"] += 1
                rec["rank"] += float(r.get("rank") or 0.0)
    except Exception:  # noqa: BLE001 — кривой tsquery или база недоступна
        log.warning("Похожие обсуждения не собраны (команда %s)", team, exc_info=True)
        return ""
    if not found:
        return ""

    def _ts(rec: dict) -> float:
        return float(rec["row"].get("meeting_ts") or 0.0)

    ordered = sorted(found.values(),
                     key=lambda rec: (-rec["hits"], -rec["rank"], -_ts(rec)))
    by_doc: dict[str, list[dict]] = {}
    for rec in ordered:
        doc = str(rec["row"].get("doc_id") or "")
        if doc not in by_doc:
            if len(by_doc) >= _RELATED_MAX_DOCS:
                continue
            by_doc[doc] = []
        if len(by_doc[doc]) < _RELATED_PER_DOC:
            by_doc[doc].append(rec)
    lines = [f"{RELATED_HEADER} (справка для понимания, НЕ переноси в "
             "решения/задачи этой встречи) ==="]
    used = len(lines[0])
    # Встречи идут по убыванию релевантности, куски внутри встречи — по
    # порядку разделов протокола. Бюджет кончился — дальше не смотрим: менее
    # релевантная встреча не должна влезать за счёт более короткой строки.
    picked = [rec for recs in by_doc.values()
              for rec in sorted(recs, key=lambda x: int(x["row"].get("idx") or 0))]
    for rec in picked:
        r = rec["row"]
        snippet = re.sub(r"\s+", " ", str(r.get("text") or "")).strip()
        if len(snippet) > _RELATED_SNIPPET:
            snippet = snippet[:_RELATED_SNIPPET].rsplit(" ", 1)[0] + "…"
        line = (f"• {r.get('title') or 'встреча'}, "
                f"{r.get('meeting_date') or '?'}: {snippet}")
        if used + len(line) + 1 > budget_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines) if len(lines) > 1 else ""
