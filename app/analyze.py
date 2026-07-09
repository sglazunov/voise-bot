"""Transcript analysis: turn a raw transcript into a structured protocol.

The actual LLM call is delegated to a pluggable provider (see llm.py), so the
same analysis works with a free local model (Ollama), a free cloud model
(Groq) or a paid one (Claude). This module owns the prompt, long-transcript
chunking and the parsing/validation of the model's JSON answer.

Long meetings are the common case, so transcripts longer than a single context
window are processed map-reduce style: each chunk is summarised into notes,
then all notes are merged into the final protocol. This is what lets minor /
late tasks (e.g. a tag-naming aside near the end) survive instead of being
truncated away.
"""
from __future__ import annotations

import json
import re
import time

from . import llm

# Single-pass threshold. Above this we chunk (map-reduce) so the WHOLE meeting
# is analysed, not just the first part.
_MAX_CHARS = 16000
# Size of each chunk when the transcript is too long for one pass. Smaller
# chunks = more thorough extraction (the model reads each part more carefully).
_CHUNK_CHARS = 10000
# Overlap between chunks so a topic split across a boundary isn't lost.
_CHUNK_OVERLAP = 800
# Cap the number of chunks so a huge file doesn't fan out into too many calls;
# beyond this we grow the chunk size instead.
_MAX_CHUNKS = 16

# Shared description of the JSON shape we want back.
_SCHEMA = (
    "{{\n"
    '  "participants": [{{"name": "Имя участника или «Спикер 1»", "role": "роль/чем '
    'занимается, если ясно из разговора, иначе пустая строка"}}],\n'
    '  "summary": "о чём была встреча в целом и какие темы затрагивали (5-8 предложений)",\n'
    '  "detailed": [\n'
    '    {{"topic": "Понятное, информативное название темы — сразу ясно, О ЧЁМ шла речь '
    '(не одно-два слова, а суть)", "details": "ПОЛНОЕ, САМОДОСТАТОЧНОЕ описание темы на '
    '10-12 предложений: сначала — по какой теме и в каком контексте говорили (чтобы было '
    'понятно без прослушивания записи), затем что именно обсуждали, КТО что предложил и кто '
    'возражал (по именам) и почему, аргументы, конкретика (цифры, проценты, названия, сроки, '
    'примеры) и к чему в итоге пришли. Пиши так, чтобы, прочитав ТОЛЬКО этот абзац, человек '
    'полностью понял тему"}}\n'
    "  ],\n"
    '  "key_thoughts": ["ключевая мысль 1 (с указанием, кто её высказал, если ясно)", "..."],\n'
    '  "conclusions": ["вывод/итог 1", "вывод/итог 2"],\n'
    '  "decisions": ["принятое решение/договорённость 1", "..."],\n'
    '  "done_tasks": [{{"task": "что уже сделано/выполнено", "owner": "кто сделал, или —"}}],\n'
    '  "tasks": [{{"task": "крупная задача, которую нужно сделать", "owner": "ответственный (имя/роль), или —"}}],\n'
    '  "minor_tasks": [{{"task": "мелкая задача/доработка", "owner": "ответственный, или —"}}]\n'
    "}}"
)

_RULES = (
    "ГЛАВНОЕ — ТОЧНОСТЬ И ПОЛНОТА. Опирайся СТРОГО на текст: ничего не выдумывай и "
    "не додумывай. Пройди по ВСЕЙ встрече от начала до конца и не упусти ни одной "
    "обсуждённой темы, договорённости или задачи. Сохраняй конкретику дословно: "
    "имена, числа, проценты, сроки/даты, названия (модулей, тегов, систем, продуктов), "
    "термины. Если что-то сказано неуверенно или неясно из расшифровки — так и помечай "
    "(«предположительно», «не до конца ясно»), но не пропускай. Лучше подробнее, чем "
    "короче — это рабочий протокол, по которому будут восстанавливать ход встречи.\n\n"
    "КРИТИЧЕСКИ ВАЖНО ПРО АВТОРСТВО И ОТВЕТСТВЕННЫХ. Это автоматическая расшифровка БЕЗ "
    "пометок, кто говорит: реплики идут сплошным текстом. Поэтому НИКОГДА не угадывай, "
    "кто что сказал и кто за что отвечает. Указывай автора реплики или ответственного за "
    "задачу ТОЛЬКО когда это ОДНОЗНАЧНО видно из слов: прямое обращение по имени "
    "(«Кирилл, сделай…», «Наташа, с тебя…»), или человек сам берёт задачу («я возьму», "
    "«я сделаю», «за мной», «беру на себя», «давайте я»). ЗАПОМНИ: тот, кто ГОВОРИТ или "
    "РАССКАЗЫВАЕТ о задаче/теме, — НЕ обязательно тот, кто её ДЕЛАЕТ. Не приписывай "
    "ответственность рассказчику и не «назначай» того, кто просто активно обсуждал тему. "
    "Если однозначного указания в тексте нет — оставляй автора неуказанным, а owner ставь "
    "'—'. Лучше вообще без имени, чем с ОШИБОЧНЫМ именем — неверно назначенный "
    "ответственный хуже, чем пустой.\n\n"
    "Правила:\n"
    "- participants: определи участников встречи и КТО ОНИ. Имена бери ТОЛЬКО из "
    "текста — по обращениям («Кирилл, …», «Наташа, …»), по самопредставлениям и "
    "упоминаниям. Если имя не названо — пиши «Спикер 1», «Спикер 2». Укажи роль, если "
    "она ясна (фронтенд, бэкенд, дизайнер, руководитель и т.п.).\n"
    "- КТО ЧТО ГОВОРИЛ: в detailed и key_thoughts указывай автора ТОЛЬКО когда он "
    "однозначно ясен из текста — по прямому обращению («Кирилл, …»), самопредставлению "
    "или явному «я предлагаю / я считаю / я возьму». Если по тексту непонятно, кто именно "
    "это сказал, — НЕ подставляй имя наугад, просто опиши факт без автора («предложили "
    "сделать…», «прозвучало возражение…»). Ошибочно приписанная реплика хуже, чем реплика "
    "без автора.\n"
    "- ВАЖНО: если в тексте перед репликами стоят метки с ИМЕНЕМ и временем — например "
    "«[00:12] Сергей Глазунов:» — это ДОСТОВЕРНЫЙ автор реплик, считанный с видео встречи "
    "(по подсветке активного говорящего). ПОЛНОСТЬЮ доверяй этим меткам: всё, что идёт "
    "после метки и до следующей метки, сказал именно этот человек. Используй их, чтобы "
    "точно указывать, КТО что предложил/возразил/сказал, и чтобы правильно ставить "
    "ОТВЕТСТВЕННЫХ по задачам (кто вслух взял задачу — тот и owner). Это надёжнее любых "
    "догадок по контексту.\n"
    "- Если метки вида «Спикер 1:», «Спикер 2:» (без имени, включена диаризация) — тоже "
    "используй их, и где из разговора понятно настоящее имя, подписывай реальным именем "
    "(например: «Спикер 2 (Кирилл)»).\n"
    "- Если в конце текста есть блок «ТЕКСТ С ЭКРАНА» — это распознанное содержимое "
    "того, что ПОКАЗЫВАЛИ на экране (слайды, код, документы, таблицы). Учитывай его "
    "наравне с речью: отрази в разборе, что демонстрировали и как это связано с "
    "обсуждением; при необходимости добавь отдельную тему про показанное на экране.\n"
    "ПИШИ ДЛЯ ЧЕЛОВЕКА, КОТОРОГО НЕ БЫЛО НА ВСТРЕЧЕ. Он не знает контекста, проектов и "
    "сокращений. Объясняй простым, живым языком, как будто пересказываешь коллеге: "
    "полными фразами, без канцелярита и без «воды». Если звучит термин, аббревиатура или "
    "название (модуля, тега, системы) — коротко поясняй, что это, если понятно из "
    "разговора. Цель — чтобы, прочитав протокол, человек понял, ЧТО происходило в каждой "
    "части встречи, даже если он не слышал ни секунды записи.\n"
    "- summary: 5-8 предложений простым языком — о чём вообще была встреча, зачем "
    "собрались, какие главные темы затронули и к чему в основном пришли. Как краткий "
    "пересказ для того, кто спросил «что там было?».\n"
    "- detailed: МАКСИМАЛЬНО ПОДРОБНЫЙ разбор по темам — раздели разговор на столько тем, "
    "сколько их реально было (обычно 6-15), идя по встрече по порядку. У каждой темы два "
    "поля:\n"
    "    • topic — ПОНЯТНОЕ, ИНФОРМАТИВНОЕ название: по нему сразу ясно, ПО КАКОЙ теме "
    "говорили. Не одно-два слова, а суть (например: «Редизайн карточки товара: структура "
    "блоков и приоритеты», а не «Карточка»).\n"
    "    • details — ПОЛНОЕ, САМОДОСТАТОЧНОЕ описание темы на 10-12 предложений простым "
    "языком. НАЧНИ с 1-2 фраз контекста — о чём и почему зашла речь (чтобы понял тот, кто "
    "не был на встрече), затем — что именно обсуждали, какие звучали предложения и "
    "возражения и почему, конкретика (цифры, проценты, названия модулей/тегов/систем, "
    "сроки, примеры), и чем тема закончилась (к чему пришли / что решили / что осталось "
    "открытым). Авторов реплик подставляй только там, где это однозначно ясно (см. правило "
    "про авторство). Пиши так, чтобы, прочитав ТОЛЬКО этот абзац, человек полностью понял "
    "тему. НЕ обобщай и НЕ сокращай — это главная часть протокола.\n"
    "- key_thoughts: 8-15 ключевых тезисов и важных формулировок (по возможности — "
    "с указанием, кто их высказал).\n"
    "- conclusions: ВЫВОДЫ и итоги — к чему в целом пришла команда, оценка статуса/"
    "ситуации, общие заключения (4-8 пунктов).\n"
    "- decisions: что именно решили/договорились (пустой список, если решений нет).\n"
    "- done_tasks: что УЖЕ СДЕЛАНО/выполнено к моменту встречи (готово, закрыто, "
    "починено). owner — кто это сделал, ТОЛЬКО если в тексте прямо сказано, кто именно "
    "(«я починил», «Сергей выкатил»). Иначе owner: '—'.\n"
    "- tasks: что НУЖНО СДЕЛАТЬ — крупные задачи на будущее. owner ставь ТОЛЬКО при "
    "явном указании: человек сам взял задачу («я возьму / беру / сделаю / за мной») или "
    "её поручили конкретному человеку по имени («Кирилл, сделай…», «давай это на тебе»). "
    "НЕ назначай ответственным того, кто просто предложил задачу, рассказал о ней или "
    "активно её обсуждал. Если в тексте нет однозначного «кто делает» — owner: '—'. "
    "Пустой owner — это нормально и правильно, когда ответственного не назначили вслух.\n"
    "- minor_tasks: ОБЯЗАТЕЛЬНО выпиши и мелкие, второстепенные задачи и доработки "
    "(мелкие правки UI, договорённости об именовании — названия тегов/кнопок/сущностей, "
    "кто кому что скинет/даст доступ). owner — по тем же строгим правилам (или '—').\n"
    "- owner — это конкретный человек или роль (например: «Сергей», «Кирилл», "
    "«дизайнер», «бэкенд»). НЕ выдумывай имена и НЕ угадывай — бери только из текста и "
    "только при явном назначении. Сомневаешься — ставь '—'.\n"
    "- Не путай сделанное с тем, что нужно сделать: done_tasks — прошедшее время, "
    "tasks и minor_tasks — будущее.\n"
    "- Всё на русском языке. Верни ТОЛЬКО JSON, без markdown и пояснений."
)

_PROMPT_TEMPLATE = (
    "Проанализируй транскрипцию рабочей встречи (автоматическая расшифровка, возможны "
    "ошибки распознавания имён и терминов) и верни ответ ТОЛЬКО в виде JSON.\n\n"
    "Транскрипция:\n{transcript}\n\n"
    "Формат ответа:\n" + _SCHEMA + "\n\n" + _RULES
)

# Map step: condense one chunk into plain-text notes (not JSON).
_MAP_TEMPLATE = (
    "Это часть {i} из {n} расшифровки рабочей встречи (автоматическая, возможны "
    "ошибки). Подробно по-русски выпиши из ЭТОГО фрагмента:\n"
    "• Участников, которые проявились (имена из обращений/самопредставлений; если "
    "имени нет — «Спикер N»), и их роли, если ясны.\n"
    "• Темы, которые обсуждались — ПОДРОБНО по каждой: о чём и в каком контексте шла "
    "речь, что именно обсуждали, какие были предложения/возражения и аргументы, "
    "конкретика (числа, проценты, названия, сроки, примеры), к чему пришли; и КТО что "
    "сказал/предложил/возразил, где понятно из контекста. Не сжимай темы до одной строки "
    "— сохрани достаточно деталей, чтобы по ним можно было восстановить тему целиком.\n"
    "• Ключевые мысли, решения и договорённости.\n"
    "• ВСЕ задачи и действия, включая мелкие и второстепенные (мелкие правки UI, "
    "договорённости об именовании — названия тегов/кнопок/сущностей, кто кому даёт "
    "доступ/что-то скидывает, мелкие техдоделки). Для каждой задачи укажи "
    "ОТВЕТСТВЕННОГО ТОЛЬКО если в тексте прямо сказано, кто её берёт / кому поручили "
    "(«я возьму», «за мной», «Кирилл, сделай»). Если явного назначения нет — пометь "
    "«ответственный не назван». НЕ угадывай и НЕ назначай ответственным того, кто просто "
    "рассказывал о задаче или обсуждал её.\n"
    "Это автоматическая расшифровка БЕЗ пометок, кто говорит, — поэтому не приписывай "
    "реплики и ответственность наугад: имя автора ставь только там, где оно однозначно "
    "следует из текста. Выпиши ВСЁ существенное из этого фрагмента, ничего не пропускай. "
    "Сохраняй дословно имена, числа, проценты, сроки/даты, названия и термины. Не "
    "выдумывай того, чего нет в тексте. Пиши списком, без вступления и заключения.\n\n"
    "Фрагмент:\n{chunk}"
)

# Reduce step: merge all chunk notes into the final protocol JSON.
_REDUCE_TEMPLATE = (
    "Ниже — заметки, собранные по последовательным частям одной рабочей встречи. "
    "Объедини их в ЕДИНЫЙ ПОДРОБНЫЙ протокол: убери только дословные дубли, но "
    "СОХРАНИ ВСЕ детали, темы, договорённости и задачи (включая мелкие) — НЕ "
    "сокращай и не выбрасывай содержательные пункты, агрегируй без потери смысла. "
    "Объединяй сведения об участниках и ответственных из разных частей. "
    "Верни ответ ТОЛЬКО в виде JSON.\n\n"
    "Заметки по частям:\n{notes}\n\n"
    "Формат ответа:\n" + _SCHEMA + "\n\n" + _RULES
)


# The full instruction the model normally receives (everything except the
# transcript itself). Shown in "expert mode" so the user can edit it. Single
# braces here (no .format applied), unlike the templates above.
EXPERT_PROMPT_DEFAULT = (
    "Проанализируй транскрипцию рабочей встречи (она добавляется ниже; "
    "автоматическая расшифровка, возможны ошибки распознавания) и верни ответ "
    "ТОЛЬКО в виде JSON.\n\n"
    "Формат ответа:\n" + _SCHEMA.replace("{{", "{").replace("}}", "}") + "\n\n" + _RULES
)


def _extract_json(raw: str) -> dict:
    """Parse the model's answer into a dict, tolerating code fences / stray text."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def _split_chunks(text: str) -> list[str]:
    """Split transcript into line-aligned chunks with overlap (segments intact)."""
    size = _CHUNK_CHARS
    # Grow chunk size if we'd otherwise exceed the chunk cap.
    if len(text) > size * _MAX_CHUNKS:
        size = len(text) // _MAX_CHUNKS + 1
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in text.splitlines(keepends=True):
        if cur_len + len(line) > size and cur:
            chunks.append("".join(cur))
            # Carry the trailing ~_CHUNK_OVERLAP chars into the next chunk so a
            # topic spanning the boundary survives in both.
            ov: list[str] = []
            ov_len = 0
            for prev in reversed(cur):
                if ov_len + len(prev) > _CHUNK_OVERLAP:
                    break
                ov.insert(0, prev)
                ov_len += len(prev)
            cur, cur_len = ov[:], ov_len
        cur.append(line)
        cur_len += len(line)
    if cur:
        chunks.append("".join(cur))
    return chunks


def _with_extra(prompt: str, extra: str) -> str:
    """Append the user's custom instructions to a prompt, if any."""
    extra = (extra or "").strip()
    if not extra:
        return prompt
    return (prompt + "\n\nДОПОЛНИТЕЛЬНЫЕ ТРЕБОВАНИЯ ПОЛЬЗОВАТЕЛЯ (обязательно учти "
            "их, но сохрани формат JSON и все поля):\n" + extra)


class AnalysisCancelled(RuntimeError):
    """Raised when the user cancels protocol generation mid-way."""


def _stream_complete(backend, prompt, max_tokens, on_progress, stage, force_json=True):
    """Call backend.complete, streaming tokens to on_progress when supported.

    Only Ollama streams; other providers return the full text in one shot (we
    still emit the stage so the UI shows what's happening)."""
    if on_progress:
        on_progress(stage, "")
    if on_progress and isinstance(backend, llm.OllamaProvider):
        return backend.complete(
            prompt, max_tokens=max_tokens, force_json=force_json,
            on_token=lambda full: on_progress(stage, full))
    return backend.complete(prompt, max_tokens=max_tokens, force_json=force_json)


def analyze_transcript(transcript_text: str, provider: str | None = None,
                       extra_instructions: str = "", custom_prompt: str = "",
                       on_progress=None, cancel_check=None, keys: dict | None = None) -> dict:
    """Send the transcript to the chosen LLM provider and return structured analysis.

    `provider` is one of "ollama" | "groq" | "gemini" | "yandex" | "gigachat" |
    "anthropic" | "auto" | None.
    `extra_instructions` — optional free-form text appended to the prompt.
    `custom_prompt` — EXPERT MODE: a full replacement for the built-in instruction
    (the transcript / notes are still appended by the app). If it doesn't ask for
    the same JSON fields, the Word export may come out empty — caller's risk.
    `on_progress(stage, text)` — optional callback for the live UI: `stage` is a
    human label, `text` is the growing generated text (Ollama streams it).
    Returns a dict with keys: summary, detailed, key_thoughts, conclusions,
    decisions, done_tasks, tasks, minor_tasks, _provider.
    """
    backend = llm.get_provider(provider, keys)
    text = (transcript_text or "").strip()
    custom = (custom_prompt or "").strip()

    def _ck():
        if cancel_check and cancel_check():
            raise AnalysisCancelled()

    _ck()
    if len(text) <= _MAX_CHARS:
        if custom:
            prompt = custom + "\n\nТранскрипция:\n" + text
        else:
            prompt = _PROMPT_TEMPLATE.format(transcript=text)
        raw = _stream_complete(backend, _with_extra(prompt, extra_instructions),
                               10000, on_progress, "Генерация протокола…")
    else:
        chunks = _split_chunks(text)
        notes_parts = []
        for i, chunk in enumerate(chunks, 1):
            _ck()  # cancel between chunks
            note = _stream_complete(
                backend, _MAP_TEMPLATE.format(i=i, n=len(chunks), chunk=chunk),
                3500, on_progress, f"Читаю встречу: часть {i} из {len(chunks)}…",
                force_json=False)
            notes_parts.append(f"=== Часть {i} ===\n{note.strip()}")
            # Free cloud tiers rate-limit easily; pace the chunk calls a bit.
            if backend.name == "groq" and i < len(chunks):
                time.sleep(2)
        notes = "\n\n".join(notes_parts)
        _ck()  # cancel before the final merge
        if custom:
            prompt = (custom + "\n\nНиже — заметки по последовательным частям "
                      "встречи; объедини их в итог по требованиям выше:\n" + notes)
        else:
            prompt = _REDUCE_TEMPLATE.format(notes=notes)
        raw = _stream_complete(backend, _with_extra(prompt, extra_instructions),
                               10000, on_progress, "Свожу протокол…")

    result = _extract_json(raw)

    # Normalise — guarantee the shape the rest of the app expects.
    result.setdefault("summary", "")
    result.setdefault("detailed", [])
    result["participants"] = _normalise_participants(result.get("participants", []))
    # Plain string lists.
    for list_key in ("key_thoughts", "conclusions", "decisions"):
        val = result.get(list_key, [])
        if isinstance(val, str):
            val = [val] if val.strip() else []
        result[list_key] = [str(x).strip() for x in val if str(x).strip()]
    # Task lists carry an owner.
    for list_key in ("done_tasks", "tasks", "minor_tasks"):
        result[list_key] = _normalise_tasks(result.get(list_key, []))
    result["detailed"] = _normalise_detailed(result["detailed"])
    result["_provider"] = backend.name
    return result


def _normalise_participants(items) -> list[dict]:
    """Coerce participants into [{name, role}], tolerating strings/dicts."""
    if not items:
        return []
    if isinstance(items, str):
        items = [items]
    out = []
    for it in items:
        if isinstance(it, dict):
            name = str(it.get("name") or it.get("speaker") or "").strip()
            role = str(it.get("role") or "").strip()
        else:
            name, role = str(it).strip(), ""
        if name:
            out.append({"name": name, "role": role})
    return out


def _normalise_tasks(tasks) -> list[dict]:
    """Coerce a task list into [{task, owner}], tolerating plain strings."""
    if not tasks:
        return []
    if isinstance(tasks, str):
        tasks = [tasks]
    out = []
    for item in tasks:
        if isinstance(item, dict):
            text = str(item.get("task") or item.get("title") or item.get("text") or "").strip()
            owner = str(item.get("owner") or item.get("assignee") or "").strip()
        else:
            text, owner = str(item).strip(), ""
        if owner in ("—", "-", "не назначен", "неизвестно", "?"):
            owner = ""
        if text:
            out.append({"task": text, "owner": owner})
    return out


def _normalise_detailed(detailed) -> list[dict]:
    """Coerce the 'detailed' field into a list of {topic, details} dicts.

    Free models sometimes return a plain string, a list of strings, or a dict
    of topic->text instead of the requested list of objects.
    """
    if not detailed:
        return []
    if isinstance(detailed, str):
        return [{"topic": "", "details": detailed}]
    if isinstance(detailed, dict):
        return [{"topic": str(k), "details": str(v)} for k, v in detailed.items()]
    out = []
    for item in detailed:
        if isinstance(item, dict):
            topic = str(item.get("topic") or item.get("title") or "").strip()
            details = str(item.get("details") or item.get("text") or "").strip()
            if topic or details:
                out.append({"topic": topic, "details": details})
        elif isinstance(item, str) and item.strip():
            out.append({"topic": "", "details": item.strip()})
    return out
