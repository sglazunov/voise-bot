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

Рядом: analyze_schemas.py — Pydantic-контракт ответов, analyze_prompts.py —
тексты промптов и пресеты типов встреч.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import time

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import config, llm, protocol_quality

# Схемы и тексты промптов вынесены в соседние модули: 450 строк деклараций и
# русского текста без единой ветки логики мешали читать сам конвейер. Имена
# ре-экспортируются, потому что снаружи (и в тестах) обращаются к analyze.X.
from .analyze_schemas import (          # noqa: F401 — часть публичного API
    TopicNote, DecisionNote, TaskNote, MapNotes,
    ProtoParticipant, ProtoTopic, ProtoTask, Protocol, norm_owner,
)
from .analyze_prompts import (          # noqa: F401 — часть публичного API
    PROTOCOL_PRESETS, EXPERT_PROMPT_DEFAULT,
    preset_rules, preset_for_title,
    _SCHEMA, _RULES, _PROMPT_TEMPLATE, _MAP_SCHEMA, _MAP_TEMPLATE,
    _MERGE_TEMPLATE, _REDUCE_TEMPLATE, _NOTES_BLOCK, _with_notes,
)

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

class TruncatedAnswer(json.JSONDecodeError):
    """Ответ модели оборван: JSON начат, но верхний объект так и не закрыт.

    Раньше такой ответ НЕ был ошибкой: перебор «с каждой скобки» находил первый
    ВЛОЖЕННЫЙ объект ({"name": "Аня", "role": …}), снисходительная схема его
    пропускала, и наружу уходил протокол со всеми пустыми полями — без единого
    признака, что модель просто не дописала. На бою это выглядело как «ноль
    задач» или «все пункты без подтверждения».
    """


def _extract_json(raw: str, expected_keys=None) -> dict:
    """Разобрать ответ модели в словарь, стерпев ограду кода и лишний текст.

    Лишнее бывает и ПОСЛЕ объекта: пояснение «Вот ваш протокол…», второй JSON,
    следы размышлений. Раньше такой ответ падал с «Extra data», хотя сам
    протокол в нём был целым, — и готовая работа модели выбрасывалась. Поэтому
    берём ПЕРВЫЙ полный объект через raw_decode, а хвост игнорируем.

    Объект, начинающийся НЕ с первой скобки, принимается только если в нём есть
    хотя бы одно из `expected_keys` — иначе это вложенный кусок оборванного
    ответа, и честнее поднять TruncatedAnswer.
    """
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    first = raw.find("{")
    if first < 0:
        raise json.JSONDecodeError("В ответе модели нет JSON-объекта", raw, 0)
    expected = set(expected_keys or ())
    dec = json.JSONDecoder()
    # Пробуем с каждой открывающей скобки: перед объектом тоже бывает текст.
    for i, ch in enumerate(raw):
        if ch != "{":
            continue
        try:
            obj, _end = dec.raw_decode(raw, i)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if i == first or (expected and expected & set(obj)):
            return obj
        # Вложенный объект внутри оборванного ответа — не результат.
    # Последняя попытка — жадный поиск от первой скобки до последней: так
    # разбирается объект, внутри которого модель наделала мелких огрех.
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    raise TruncatedAnswer(
        "Ответ модели оборван: JSON начат, но не закрыт (кончился лимит токенов)",
        raw, first)


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


def _parse_ts(t) -> float | None:
    """«мм:сс» / «чч:мм:сс» → seconds; None when absent/unparseable."""
    if not t:
        return None
    m = re.match(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{2})\s*$", str(t))
    if not m:
        return None
    h = int(m.group(1) or 0)
    return h * 3600 + int(m.group(2)) * 60 + int(m.group(3))


# Overlap-zone duplicates: the same task shows up in two neighbouring chunks
# with slightly different wording. Close in time + near-identical text = dup.
_DEDUP_WINDOW_SEC = 120
_DEDUP_RATIO = 0.85
# Без таймкода окно времени не работает — тогда дублем считается только почти
# буквальное совпадение, иначе склейка идёт через всю встречу.
_DEDUP_RATIO_NO_TIME = 0.97


def _is_duplicate(txt: str, sec, it: dict, prev_txt: str, s2, prev: dict) -> bool:
    """Одна и та же задача из зоны перекрытия — или две РАЗНЫЕ похожие?

    Посимвольная похожесть целых строк не отличает «на синий» от «на красный»
    и «Ивану» от «Марии» (0.85–0.90) — на бою такие пары склеивались, причём
    ответственный второй переезжал на первую. Поэтому, кроме похожести:
    у обеих сторон не должно быть СВОИХ значимых слов (замена объекта/адресата —
    это разные задачи; уточнение с одной стороны — та же), а два разных
    ответственных — всегда две задачи.
    """
    both = sec is not None and s2 is not None
    if both and abs(sec - s2) > _DEDUP_WINDOW_SEC:
        return False
    ratio = difflib.SequenceMatcher(None, txt.lower(), prev_txt.lower()).ratio()
    if ratio < (_DEDUP_RATIO if both else _DEDUP_RATIO_NO_TIME):
        return False
    o1, o2 = norm_owner(it.get("owner")), norm_owner(prev.get("owner"))
    if o1 and o2 and o1.lower() != o2.lower():
        return False
    a, b = _stems(txt), _stems(prev_txt)
    if (a - b) and (b - a):
        return False
    return True


def _dedup_maps(maps: list[dict]) -> list[dict]:
    """Drop duplicate tasks/decisions across the chunk notes (overlap zone).

    The FIRST occurrence survives; a duplicate's owner/evidence/done enrich the
    survivor when it lacked them (the second chunk often sees the assignment)."""
    for key, text_field in (("tasks", "task"), ("decisions", "text")):
        seen: list[tuple[float | None, str, dict]] = []
        for m in maps:
            kept = []
            for it in (m.get(key) or []):
                if not isinstance(it, dict):
                    continue
                txt = str(it.get(text_field) or "").strip()
                sec = _parse_ts(it.get("t"))
                dup = None
                if txt:
                    for s2, prev_txt, prev in seen:
                        if _is_duplicate(txt, sec, it, prev_txt, s2, prev):
                            dup = prev
                            break
                if dup is not None:
                    if not norm_owner(dup.get("owner")) and norm_owner(it.get("owner")):
                        dup["owner"] = it["owner"]
                        dup["owner_evidence"] = it.get("owner_evidence")
                    if it.get("done"):
                        dup["done"] = True
                    continue
                kept.append(it)
                seen.append((sec, txt, it))
            m[key] = kept
    return maps


def _est_tokens(text: str) -> int:
    return len(text) // 3   # ~3 chars per token for mixed ru/en text


def _ctx_budget(backend) -> int:
    """How many PROMPT tokens one request to this engine can safely carry."""
    base = str(getattr(backend, "name", "")).split(":")[0]
    tpm = config.PROVIDER_TPM.get(base)
    if tpm:
        return tpm
    if base == "ollama":
        return llm.OLLAMA_NUM_CTX
    return 100_000   # big-context clouds (Gemini, Claude, Yandex…)


def _mech_merge(a: dict, b: dict) -> dict:
    """Lossless no-LLM merge of two neighbouring note-sets (fallback when the
    LLM merge fails): concatenates lists, unions participants."""
    ra, rb = str(a.get("time_range") or ""), str(b.get("time_range") or "")
    return {
        "time_range": (ra.split("–")[0] + "–" + rb.split("–")[-1]) if ra and rb else ra or rb,
        "participants": list(dict.fromkeys(
            (a.get("participants") or []) + (b.get("participants") or []))),
        "topics": (a.get("topics") or []) + (b.get("topics") or []),
        "decisions": (a.get("decisions") or []) + (b.get("decisions") or []),
        "tasks": (a.get("tasks") or []) + (b.get("tasks") or []),
    }


def _notes_blob(maps: list[dict]) -> str:
    return "\n\n".join(
        f"=== Часть {i} ({m.get('time_range') or '?'}) ===\n"
        + json.dumps(m, ensure_ascii=False)
        for i, m in enumerate(maps, 1))


def _with_extra(prompt: str, extra: str) -> str:
    """Append the user's custom instructions to a prompt, if any."""
    extra = (extra or "").strip()
    if not extra:
        return prompt
    return (prompt + "\n\nДОПОЛНИТЕЛЬНЫЕ ТРЕБОВАНИЯ ПОЛЬЗОВАТЕЛЯ (обязательно учти "
            "их, но сохрани формат JSON и все поля):\n" + extra)


# --------------------------------------------------------------------------- #
# Guard: never build a protocol without real speech to build it from.
# --------------------------------------------------------------------------- #
# The analysis input is NOT just the transcript — the app appends the call
# roster («УЧАСТНИКИ ЗВОНКА», read off the video grid), the team's standing
# context («ПОСТОЯННЫЙ КОНТЕКСТ») and any on-screen OCR. When recognition
# produced nothing, those blocks are still there, so the model receives real
# names and real project topics with no speech — and writes a plausible meeting
# that never happened. Observed on real data: 10 of 45 protocols were pure
# invention (a 5-minute interview came back as a module-design discussion with
# tasks assigned to people who never spoke).
#
# So the gate measures ONLY the speech, with those blocks stripped out.
_INJECTED_BLOCK_RE = re.compile(
    r"\n*===\s*(УЧАСТНИКИ ЗВОНКА|ПОСТОЯННЫЙ КОНТЕКСТ|ТЕКСТ С ЭКРАНА"
    r"|КОНТЕКСТ СЕРИИ ВСТРЕЧ|ПРОШЛАЯ ВСТРЕЧА ЭТОЙ СЕРИИ)"
    r"[\s\S]*?(?=\n\s*===|\Z)", re.IGNORECASE)


# Контекст (участники звонка, постоянный контекст команды, карточка серии,
# итоги прошлой встречи) идёт ОТДЕЛЬНЫМ параметром и ставится ПЕРЕД текстом в
# каждом запросе. Раньше он дописывался в конец расшифровки: на длинной
# встрече при нарезке на части он попадал только в ПОСЛЕДНИЙ фрагмент, а
# финальное сведение (оно видит лишь заметки) не получало его вовсе — то есть
# именно на часовых встречах, где контекст нужнее всего, его не было.
_CTX_MAP_BLOCK_CHARS = int(os.getenv("VTX_CTX_MAP_BLOCK_CHARS", "2500"))


def _ctx_prefix(context: str) -> str:
    ctx = (context or "").strip()
    return (ctx + "\n\n") if ctx else ""


def _context_for_map(context: str) -> str:
    """Контекст для шага чтения фрагментов: без итогов прошлой встречи (они
    нужны только при сведении) и с потолком на каждый блок — иначе часовая
    встреча из 12 фрагментов возит по 40 КБ контекста в каждом запросе."""
    ctx = (context or "").strip()
    if not ctx:
        return ""
    blocks = re.split(r"\n(?=\s*===)", ctx)
    out = []
    for b in blocks:
        b = b.strip()
        if not b or b.startswith("=== ПРОШЛАЯ ВСТРЕЧА ЭТОЙ СЕРИИ"):
            continue
        if len(b) > _CTX_MAP_BLOCK_CHARS:
            b = b[:_CTX_MAP_BLOCK_CHARS].rsplit("\n", 1)[0] + "\n…"
        out.append(b)
    return "\n\n".join(out)
# Timecodes are structure, not content: "[00:12]" must not count as speech.
_TIMECODE_RE = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?\]")
# Minimum genuinely spoken words before a protocol may be generated. A meeting
# worth a protocol always clears this; failed recognition never does.
MIN_SPEECH_WORDS = int(os.getenv("VTX_MIN_SPEECH_WORDS", "40"))
# Ниже этого — встреча фактически не состоялась: люди поздоровались и разошлись.
# Протокол собираем (человек всё равно захочет посмотреть), но честно помечаем.
# Замер 06.08: встреча 04.08 10:00 дала 118 слов речи за четыре часа записи и
# получила полноценный с виду протокол на 241 слово — из воздуха.
THIN_SPEECH_WORDS = int(os.getenv("VTX_THIN_SPEECH_WORDS", "300"))


class NoTranscript(RuntimeError):
    """Raised instead of inventing a protocol when there is nothing to analyse."""


def speech_words(text: str) -> int:
    """Count genuinely spoken words in the analysis input.

    Strips the injected roster/context/OCR blocks and timecode markers, so an
    empty recognition scores ~0 no matter how much context was appended.
    """
    body = _INJECTED_BLOCK_RE.sub(" ", text or "")
    body = _TIMECODE_RE.sub(" ", body)
    return len([w for w in body.split() if any(ch.isalpha() for ch in w)])


def ensure_analysable(text: str) -> None:
    """Raise NoTranscript when there is too little speech to build a protocol."""
    n = speech_words(text)
    if n < MIN_SPEECH_WORDS:
        raise NoTranscript(
            f"Протокол не собран: в записи не распознано речи "
            f"(слов: {n}, нужно от {MIN_SPEECH_WORDS}). Обычно это значит, что "
            "запись получилась без звука или пустой. Проверьте аудио-устройство "
            "(«Проверить звук» на странице автоматизации) и запустите анализ "
            "повторно."
        )


class AnalysisCancelled(RuntimeError):
    """Raised when the user cancels protocol generation mid-way."""


# Сколько токенов ответа нужно на полноценный протокол длинной встречи.
# Ниже этого разбор по темам начинает схлопываться в пересказ оглавления.
_WANT_PROTOCOL_TOKENS = 6000


def _reduce_notes_budget(backend, want_answer: int = _WANT_PROTOCOL_TOKENS) -> int:
    """Сколько токенов заметок можно унести в финальное сведение, чтобы у САМОГО
    протокола осталось место.

    Раньше заметки уплотнялись до размера КОНТЕКСТА движка — и этого мало.
    У Groq контекст большой, а лимит токенов в минуту всего 12000 на вход и
    выход вместе: заметки часовой встречи занимали почти весь лимит, и на ответ
    оставалось 1200 токенов. Отсюда и брались протоколы на 238 слов по
    двухчасовой встрече — чем длиннее встреча, тем сильнее её схлопывало.

    Теперь приоритет обратный: сначала резервируем место под протокол, а
    заметки ужимаем под остаток. Уплотнение заметок — смысловое (модель
    сохраняет факты), обрезание ответа — слепое, поэтому первое лучше второго.
    """
    ctx = _ctx_budget(backend)
    base = str(getattr(backend, "name", "")).split(":")[0]
    tpm = config.PROVIDER_TPM.get(base)
    if tpm:
        ctx = min(ctx, max(2000, tpm - want_answer - 400))
    return ctx


def _fit_max_tokens(backend, prompt: str, want: int) -> int:
    """Shrink the requested answer size so one request fits the provider's
    per-minute token budget (input + requested output). Without this, asking for a
    large protocol makes Groq reject the request outright with HTTP 413 — and no
    amount of extra keys helps, since every account of that tier has the same cap.
    Providers with no known budget (Ollama, Claude…) are left untouched."""
    base = str(getattr(backend, "name", "")).split(":")[0]
    tpm = config.PROVIDER_TPM.get(base)
    if not tpm:
        return want
    est_input = len(prompt) // 3      # ~3 chars per token for mixed ru/en text
    budget = tpm - est_input - 400    # safety margin for the system/prompt overhead
    return max(1200, min(want, budget))


def _stream_complete(backend, prompt, max_tokens, on_progress, stage,
                     force_json=True, cancel_check=None, json_schema=None):
    """Call backend.complete, streaming tokens to on_progress when supported.

    Only Ollama streams; other providers return the full text in one shot (we
    still emit the stage so the UI shows what's happening). `cancel_check` lets a
    streaming (Ollama) generation be aborted mid-way, so «Отменить» is responsive
    even inside one long chunk — not only between chunks. `json_schema` reaches
    Ollama as a structured-output grammar; cloud providers ignore it (their
    answers are validated by the caller instead)."""
    max_tokens = _fit_max_tokens(backend, prompt, max_tokens)
    if on_progress:
        on_progress(stage, "")
    # A fallback chain streams too (when Ollama is one of its links) — it takes
    # the same kwargs and forwards them only to the backend that supports them.
    if isinstance(backend, llm.OllamaProvider) or getattr(backend, "supports_stream", False):
        try:
            return backend.complete(
                prompt, max_tokens=max_tokens, force_json=force_json,
                on_token=(lambda full: on_progress(stage, full)) if on_progress else None,
                should_stop=cancel_check, json_schema=json_schema)
        except llm.GenerationCancelled:
            raise AnalysisCancelled()
    # Облачный движок тоже может прерываться ВНУТРИ вызова, но токенов
    # наружу не отдаёт, поэтому под условие выше не попадала: «Стоп» не
    # действовал, если в цепочке не было Ollama. Признак accepts_should_stop
    # есть и у самого провайдера, и у обёртки ротации ключей.
    if getattr(backend, "accepts_should_stop", False) and cancel_check:
        try:
            return backend.complete(prompt, max_tokens, force_json,
                                    should_stop=cancel_check)
        except llm.GenerationCancelled:
            raise AnalysisCancelled()
    return backend.complete(prompt, max_tokens=max_tokens, force_json=force_json)


def _complete_validated(backend, prompt, model_cls, max_tokens, on_progress,
                        stage, cancel_check=None) -> dict:
    """One LLM call whose answer must satisfy `model_cls` (Pydantic).

    Ollama gets the JSON schema as a decoding grammar, so its answer is valid by
    construction. Cloud answers are validated after the fact; on failure the
    model gets ONE retry with the validation error quoted — that fixes the
    typical «wrapped in prose / field renamed» misses of weaker models. Raises
    ValueError if the retry still doesn't validate; the caller decides how to
    degrade."""
    schema = model_cls.model_json_schema()
    raw = _stream_complete(backend, prompt, max_tokens, on_progress, stage,
                           cancel_check=cancel_check, json_schema=schema)
    try:
        return _parse_for(model_cls, raw)
    except TruncatedAnswer:
        # Оборванный ответ — просить «исправить схему» бессмысленно: модель
        # снова упрётся в лимит. Просим то же, но короче в описаниях.
        retry = (prompt + "\n\nТвой прошлый ответ ОБОРВАЛСЯ на середине — кончился "
                 "лимит токенов. Верни ПОЛНЫЙ и корректно закрытый JSON: сократи "
                 "описания тем (details) вдвое, но списки решений и задач выпиши "
                 "полностью. Без markdown и пояснений.")
    except (ValidationError, ValueError) as e:
        err = str(e)[:600]
        retry = (prompt + "\n\nТвой прошлый ответ не прошёл проверку схемы: "
                 + err + "\nВерни ИСПРАВЛЕННЫЙ ответ строго по требуемой JSON-схеме, "
                 "без markdown и пояснений.")
    raw = _stream_complete(backend, retry, max_tokens, on_progress, stage,
                           cancel_check=cancel_check, json_schema=schema)
    try:
        return _parse_for(model_cls, raw)
    except (ValidationError, ValueError) as e:
        raise _SchemaMiss(f"Ответ модели не прошёл валидацию схемы: {e}", raw) from e


def _parse_for(model_cls, raw: str) -> dict:
    """Разбор + проверка, что это ответ ПО ЭТОЙ схеме, а не случайный словарь.

    Снисходительные схемы (все поля со значениями по умолчанию) принимали любой
    объект — даже {"name": …} из оборванного ответа — и отдавали пустой
    результат без ошибки. Хотя бы одно ожидаемое поле обязано присутствовать.
    """
    fields = set(model_cls.model_fields)
    obj = _extract_json(raw, expected_keys=fields)
    if not isinstance(obj, dict) or not (set(obj) & fields):
        got = ", ".join(list(obj)[:5]) if isinstance(obj, dict) else type(obj).__name__
        raise ValueError(f"в ответе нет ни одного ожидаемого поля "
                         f"({', '.join(sorted(fields)[:4])}…); пришло: {got}")
    return model_cls.model_validate(obj).model_dump()


def _lenient_protocol(raw: str) -> dict:
    """Запасной разбор после двух неудачных валидаций: берём что есть, но
    ТОЛЬКО если это похоже на протокол. Раньше сюда проходил любой словарь, и
    «пустой протокол» уходил в docx как готовый (В8)."""
    fields = set(Protocol.model_fields)
    obj = _extract_json(raw, expected_keys=fields)
    if not isinstance(obj, dict) or not (set(obj) & fields):
        raise RuntimeError(
            "Модель дважды вернула ответ не по схеме протокола — протокол не "
            "собран. Нажмите «Пересобрать», лучше другим движком.")
    obj["_schema_miss"] = True
    return obj


class _SchemaMiss(ValueError):
    """Both attempts failed validation; carries the last raw answer so the
    caller can degrade gracefully instead of losing the model's work."""

    def __init__(self, msg: str, raw: str = ""):
        super().__init__(msg)
        self.raw = raw or ""


def analyze_transcript(transcript_text: str, provider: str | None = None,
                       extra_instructions: str = "", custom_prompt: str = "",
                       on_progress=None, cancel_check=None, keys: dict | None = None,
                       user_notes: str = "", context: str = "") -> dict:
    """Send the transcript to the chosen LLM provider and return structured analysis.

    `context` — служебные блоки (участники звонка, постоянный контекст,
    карточка серии, итоги прошлой встречи): ставятся ПЕРЕД текстом в каждом
    запросе, в речи не считаются и цитатами для проверки не служат.

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
    text = (transcript_text or "").strip()
    # Nothing was said → say so. Checked BEFORE the engine is touched, so a
    # failed recording can never come back as an invented meeting (the roster
    # and standing-context blocks alone are enough material for the model to
    # fabricate one). Covers every caller: first run and re-analysis alike.
    ensure_analysable(text)

    # Chain, not a single provider: a 503 from the pinned engine must degrade to
    # the next configured one (e.g. Gemini down → Groq → Ollama), not kill the job.
    backend = llm.get_provider_chain(provider, keys)
    custom = (custom_prompt or "").strip()

    # A LONG meeting must not run on a CPU-only local engine: map-reduce over an
    # hour of speech takes HOURS there (prompt eval ~tens of tok/s). When the
    # engine wasn't pinned explicitly and a cloud engine is configured, start
    # from the cloud; Ollama remains the fallback for outages.
    if (len(text) > _MAX_CHARS and (provider or "auto").split(":")[0] in ("auto", "ollama")
            and getattr(backend, "prefer_cloud", None)):
        if backend.prefer_cloud() and on_progress:
            on_progress("Длинная встреча — использую облачный движок "
                        f"({backend.name}); локальный остаётся запасным.", "")

    def _ck():
        if cancel_check and cancel_check():
            raise AnalysisCancelled()

    _ck()
    warning = None
    result = None
    ctx_full = _ctx_prefix(context)
    ctx_map = _ctx_prefix(_context_for_map(context))
    if len(text) <= _MAX_CHARS:
        if custom:
            prompt = _with_notes(custom + "\n\n" + ctx_full + "Транскрипция:\n" + text,
                                 user_notes)
            raw = _stream_complete(backend, _with_extra(prompt, extra_instructions),
                                   10000, on_progress, "Генерация протокола…",
                                   cancel_check=cancel_check)
            result = _extract_json(raw)
        else:
            prompt = _with_extra(
                _with_notes(_PROMPT_TEMPLATE.format(transcript=text, context=ctx_full),
                            user_notes),
                extra_instructions)
            try:
                result = _complete_validated(backend, prompt, Protocol, 10000,
                                             on_progress, "Генерация протокола…",
                                             cancel_check)
            except _SchemaMiss as e:
                result = _lenient_protocol(e.raw)
    else:
        chunks = _split_chunks(text)
        n = len(chunks)
        maps: list[dict] = []
        for i, chunk in enumerate(chunks, 1):
            _ck()  # cancel between chunks
            stage = f"Читаю встречу: часть {i} из {n}…"
            try:
                m = _complete_validated(
                    backend, _MAP_TEMPLATE.format(i=i, n=n, chunk=chunk, context=ctx_map),
                    MapNotes, 3500, on_progress, stage, cancel_check)
            except _SchemaMiss as e:
                # The model's notes didn't fit the schema even after a retry —
                # keep them as one flat topic rather than losing the chunk.
                m = MapNotes(topics=[TopicNote(
                    topic=f"Фрагмент {i}", details=e.raw[:6000])]).model_dump()
            maps.append(m)
            # Free cloud tiers rate-limit easily; pace the chunk calls a bit.
            if backend.name == "groq" and i < n:
                time.sleep(2)

        # Mechanical dedup of overlap-zone tasks/decisions (±2 мин + похожесть
        # текста) — BEFORE the LLM sees the notes, so it can't «объединить»
        # два разных пункта или продублировать один.
        maps = _dedup_maps(maps)

        # Context-budget control: if all the notes together overflow the
        # engine's window, merge neighbours pairwise (hierarchical reduce)
        # until the final merge fits. Silent truncation is the enemy — it eats
        # the END of the meeting.
        # Бюджет заметок считается ОТ МЕСТА ПОД ПРОТОКОЛ, а не от контекста:
        # иначе заметки длинной встречи съедают минутный лимит движка и ответ
        # обрезается до пересказа оглавления.
        budget = _reduce_notes_budget(backend)
        rounds = 0
        while (len(maps) > 1 and rounds < 4
               and _est_tokens(_notes_blob(maps)) > 0.8 * budget):
            merged: list[dict] = []
            for j in range(0, len(maps), 2):
                if j + 1 >= len(maps):
                    merged.append(maps[j])
                    continue
                _ck()
                a, b = maps[j], maps[j + 1]
                stage = (f"Уплотняю заметки: {j // 2 + 1} из "
                         f"{(len(maps) + 1) // 2}…")
                try:
                    mm = _complete_validated(
                        backend,
                        _MERGE_TEMPLATE.format(
                            a=json.dumps(a, ensure_ascii=False),
                            b=json.dumps(b, ensure_ascii=False)),
                        MapNotes, 3500, on_progress, stage, cancel_check)
                except AnalysisCancelled:
                    raise
                except Exception:  # noqa: BLE001 — degrade, the merge is an optimisation
                    mm = _mech_merge(a, b)  # lossless, no LLM
                merged.append(mm)
            maps = merged
            rounds += 1

        notes = _notes_blob(maps)
        _ck()  # cancel before the final merge
        if custom:
            prompt = _with_notes(
                custom + "\n\n" + ctx_full
                + "Ниже — структурированные заметки (JSON) по "
                "последовательным частям встречи, в хронологическом порядке; "
                "объедини их в итог по требованиям выше:\n" + notes, user_notes)
            raw = _stream_complete(backend, _with_extra(prompt, extra_instructions),
                                   10000, on_progress, "Свожу протокол…",
                                   cancel_check=cancel_check)
            result = _extract_json(raw)
        else:
            prompt = _with_extra(
                _with_notes(_REDUCE_TEMPLATE.format(notes=notes, context=ctx_full),
                            user_notes),
                extra_instructions)
            if _fit_max_tokens(backend, prompt, 10000) < 4000:
                warning = ("Протокол мог потерять детали: у движка "
                           f"«{backend.name}» осталось мало лимита на ответ. "
                           "Попробуйте «Пересобрать» другим движком.")
            try:
                result = _complete_validated(backend, prompt, Protocol, 10000,
                                             on_progress, "Свожу протокол…",
                                             cancel_check)
            except _SchemaMiss as e:
                result = _lenient_protocol(e.raw)

    # Normalise — guarantee the shape the rest of the app expects.
    result.setdefault("summary", "")
    result.setdefault("detailed", [])
    result["participants"] = _normalise_participants(result.get("participants", []))
    # Plain string lists.
    for list_key in ("key_thoughts", "conclusions", "decisions"):
        val = result.get(list_key, [])
        if isinstance(val, str):
            val = [val] if val.strip() else []
        # Отсеиваем None ДО str(): модель иногда присылает null в списке, и
        # str(None) давал в протоколе строку «None».
        result[list_key] = [str(x).strip() for x in val
                            if x is not None and str(x).strip()]
    # Task lists carry an owner.
    for list_key in ("done_tasks", "tasks", "minor_tasks"):
        result[list_key] = _normalise_tasks(result.get(list_key, []))
    result["detailed"] = merge_similar_topics(_normalise_detailed(result["detailed"]))
    result["_provider"] = backend.name
    spoken = speech_words(text)
    if spoken < THIN_SPEECH_WORDS:
        result["_thin_speech"] = spoken
    # Конкретная модель — рядом с провайдером: под одним именем провайдера
    # живут десятки разных моделей, и по имени провайдера не понять,
    # какая из них собрала протокол. Цепочка отката подставляет сюда тот
    # движок, который реально ответил, а не тот, который выбрали.
    result["_model"] = str(getattr(backend, "model", "") or "")
    # Если сработал откат — сохраняем причину: иначе выбор движка выглядит
    # проигнорированным. Человек выбрал DeepSeek, получил протокол от Gemini
    # и не знает, почему.
    skipped = list(getattr(backend, "skipped", []) or [])
    if skipped:
        result["_fallback"] = skipped
    # Пустые разделы не показываем человеку: сначала пробуем переписать их по
    # расшифровке, и только безнадёжные убираем. Пометки мало — вода всё равно
    # попадёт в документ.
    result = refill_empty_topics(result, text, provider=provider, keys=keys,
                                 user_notes=user_notes, on_progress=on_progress,
                                 cancel_check=cancel_check, backend=backend)
    # Читаемость протокола — измеримая величина, а не ощущение. Пустые разделы
    # («Обсуждается X. Решается вопрос о X») и summary-оглавление кладём в
    # результат: видно в UI, считается на эталонах, не даёт дефекту тихо расти.
    result["_quality"] = protocol_quality.report(result)
    if warning:
        result["_warning"] = warning
    return result


# Слова-пустышки в начале заголовка: они есть почти у каждой темы и мешают
# сравнивать заголовки по существу («Обсуждение цвета» vs «Обсуждение названий»
# похожи только этим словом).
_TOPIC_NOISE_RE = re.compile(
    r"^\s*(обсужден\w*|разговор\w*|вопрос\w*|тема)\s+(о\s+|об\s+|по\s+)?", re.I)
# Порог похожести заголовков, при котором темы считаются одной. Подобран так,
# чтобы «Планы на будущее» и «Планы на будущее и коммуникации» слились, а
# «Цвет подсветки» и «Названия элементов» — нет.
_TOPIC_SIM = float(os.getenv("VTX_TOPIC_MERGE_SIM", "0.72"))


def _topic_key(title: str) -> str:
    """Заголовок без вводных слов и пунктуации — для сравнения по существу."""
    t = _TOPIC_NOISE_RE.sub("", (title or "").lower())
    return re.sub(r"[^\w\s]", " ", t).strip()


def merge_similar_topics(topics: list[dict]) -> list[dict]:
    """Схлопнуть темы-двойники в одну.

    Боевой случай (ЭМО 29.07): 25 тем на 4800 слов расшифровки, описания по
    2-3 фразы, а среди заголовков «Обсуждение планов на будущее» и «Обсуждение
    планов на будущее и коммуникации», «задачи» в трёх вариантах. Одна тема
    размазана по нескольким разделам — читать такой протокол не легче, чем
    протокол из воды: смысл рассыпается, просто иначе.

    Полагаться на промпт тут нельзя (проверено на summary — Groq правило
    игнорировал), поэтому склейка механическая: по похожести заголовков.
    Тексты объединяются, порядок первого вхождения сохраняется.
    """
    out: list[dict] = []
    for block in topics:
        title = (block or {}).get("topic", "") or ""
        details = (block or {}).get("details", "") or ""
        key = _topic_key(title)
        target = None
        if key:
            for kept in out:
                k2 = _topic_key(kept.get("topic", ""))
                if not k2:
                    continue
                # Вхождение — признак двойника только для содержательного
                # заголовка: «Сроки» ⊂ «Сроки релиза» ⊂ «Сроки оплаты
                # подрядчику» — три разные темы, а не одна.
                short = min(key, k2, key=len)
                contains = ((key in k2 or k2 in key)
                            and (len(short) >= 6 or len(_stems(short)) >= 2))
                if (difflib.SequenceMatcher(None, key, k2).ratio() >= _TOPIC_SIM
                        or contains):
                    target = kept
                    break
        if target is None:
            out.append({"topic": title, "details": details})
            continue
        # Оставляем более информативный заголовок, тексты соединяем.
        if len(title) > len(target.get("topic", "")):
            target["topic"] = title
        add = details.strip()
        if add and add not in (target.get("details") or ""):
            target["details"] = (target.get("details", "").rstrip() + " " + add).strip()
    return out


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
            owner = norm_owner(item.get("owner") or item.get("assignee"))
            due = str(item.get("due") or "").strip()[:80]
            if due.lower() in ("null", "none", "—", "-", "нет"):
                due = ""
        else:
            text, owner, due = str(item).strip(), "", ""
        if text:
            rec = {"task": text, "owner": owner}
            if due:
                rec["due"] = due
            out.append(rec)
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


# --------------------------------------------------------------------------- #
# Д5: grounding — every task/decision must be backed by a VERBATIM quote from
# the transcript (or the participant's notes). A protocol point the meeting
# never said is the #1 trust-killer; unverified points get flagged, and an
# owner the quotes don't support is stripped back to «—».
# --------------------------------------------------------------------------- #
class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    i: int
    quote: str = ""
    t: str | None = None
    owner_ok: bool = False


class EvidenceList(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    items: list[EvidenceItem] = Field(default_factory=list)


_VERIFY_TEMPLATE = (
    "Ниже — пункты протокола рабочей встречи и фрагмент её расшифровки "
    "(реплики размечены [мм:сс], иногда с именем говорящего). Для КАЖДОГО "
    "пункта, который подтверждается ЭТИМ фрагментом, верни ДОСЛОВНУЮ цитату — "
    "скопируй фразу из фрагмента символ в символ, НИЧЕГО не перефразируя — и "
    "таймкод ближайшей метки [мм:сс].\n"
    "owner_ok ставь true ТОЛЬКО если из цитаты или из метки говорящего видно, "
    "что задачу взял/поручили именно указанному ответственному («я возьму» от "
    "него самого, «Кирилл, сделай…»). Просто упоминание темы этим человеком — "
    "НЕ подтверждение ответственного.\n"
    "Пункты, которых в этом фрагменте нет, НЕ включай в ответ. Не выдумывай "
    "цитат. Верни ТОЛЬКО JSON вида {{\"items\": [{{\"i\": номер, \"quote\": "
    "\"дословная фраза\", \"t\": \"мм:сс\", \"owner_ok\": true|false}}]}}.\n\n"
    "Пункты протокола:\n{points}\n\nФрагмент расшифровки:\n{fragment}"
)

_VERIFIED_LISTS = ("tasks", "minor_tasks", "done_tasks", "decisions")
_MIN_QUOTE_CHARS = 10
# Пунктов в одном запросе проверки. Раньше все 30-40 шли одним запросом с
# потолком 3000 токенов — ответ обрезался, и ВСЕ пункты выходили «без
# подтверждения» (К2).
_VERIFY_BATCH = 15
# Мягкая дословность: цитата, в которой модель «починила» ошибку распознавания
# или пропустила метку говорящего между репликами, — всё ещё цитата.
_APPROX_RATIO = 0.85

# Грубое снятие русских окончаний. Раньше сравнивались первые 5 букв слов не
# короче 4 — и «теги»≠«тегов», «срок»≠«сроки», «цвет»≠«цвета»: честные цитаты
# отбрасывались как «не о том». Основа не короче 3 букв; список — от длинных
# окончаний к коротким.
_SUFFIXES = sorted((
    "иями", "ями", "ами", "ого", "его", "ому", "ему", "ыми", "ими", "ться",
    "ешь", "ете", "ить", "ать", "ять", "еть", "уть", "ыть", "ими", "ыми",
    "ах", "ях", "ов", "ев", "ей", "ой", "ий", "ый", "ая", "яя", "ое", "ее",
    "ые", "ие", "ам", "ям", "ом", "ем", "ия", "ию", "ии", "ет", "ут", "ют",
    "ат", "ят", "ил", "ла", "ли", "ло",
    "ы", "и", "а", "я", "у", "ю", "е", "о", "ь",
), key=len, reverse=True)


def _stem(w: str) -> str:
    for s in _SUFFIXES:
        if w.endswith(s) and len(w) - len(s) >= 3:
            return w[: len(w) - len(s)]
    return w


def _stems(text: str) -> set[str]:
    return {_stem(w) for w in _norm_for_match(text).split() if len(w) >= 3}


# Метка реплики: «[12:40] Кирилл Бубнов: » — структура, а не речь. Цитата,
# скопированная через границу двух реплик, метку не содержит; вырезаем её из
# фрагмента ПЕРЕД сравнением, иначе такая цитата «не дословна».
_LABEL_RE = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?\]\s*(?:[^:\n]{0,40}:)?\s*")


def _strip_labels(s: str) -> str:
    return _LABEL_RE.sub(" ", s or "")


def _quote_relevant(point_text: str, quote: str, fragment: str) -> bool:
    """A verbatim quote must also be ABOUT the point: a weak model happily
    attaches a real quote to an invented task, and verbatimness alone passes it.
    Demand a shared significant word (by stem) between the point and the quote
    or its surroundings in the fragment (±2 строки ловят анафору «я возьму это
    на себя», когда тема названа в предыдущей реплике)."""
    pw = _stems(point_text)
    if not pw:
        return True
    ctx = _stems(quote)
    qn = _norm_for_match(_strip_labels(quote))
    lines = fragment.splitlines()
    for i, line in enumerate(lines):
        if qn and qn in _norm_for_match(_strip_labels(line)):
            for j in range(max(0, i - 2), min(len(lines), i + 3)):
                ctx |= _stems(_strip_labels(lines[j]))
            break
    return bool(pw & ctx)


def _norm_for_match(s: str) -> str:
    """Дословность проверяем механически: цитата обязана быть подстрокой
    источника после нормализации (регистр/пробелы/пунктуация/ё)."""
    s = re.sub(r"[^\w\s]", " ", (s or "").lower().replace("ё", "е"))
    return re.sub(r"\s+", " ", s).strip()


class _Fragment:
    """Нормализованный фрагмент расшифровки + индекс слов для мягкого поиска."""

    def __init__(self, text: str):
        self.norm = _norm_for_match(_strip_labels(text))
        self.tokens = self.norm.split()
        self.index: dict[str, list[int]] = {}
        for i, t in enumerate(self.tokens):
            self.index.setdefault(t, []).append(i)

    def match(self, quote: str) -> str | None:
        """'verbatim' — подстрока; 'approx' — та же последовательность слов с
        небольшими расхождениями; None — цитаты в фрагменте нет."""
        qn = _norm_for_match(_strip_labels(quote))
        if len(qn) < _MIN_QUOTE_CHARS:
            return None
        if qn in self.norm:
            return "verbatim"
        qt = qn.split()
        if len(qt) < 4:
            return None
        starts: set[int] = set(self.index.get(qt[0], ()))
        starts |= {i - 1 for i in self.index.get(qt[1], ()) if i > 0}
        for s in starts:
            window = self.tokens[s:s + len(qt) + 1]
            if difflib.SequenceMatcher(None, qt, window).ratio() >= _APPROX_RATIO:
                return "approx"
        return None


def _collect_points(result: dict) -> list[tuple[str, int, str, str]]:
    """[(list_key, index, text, owner)] — all statements needing evidence."""
    points = []
    for key in _VERIFIED_LISTS:
        for idx, item in enumerate(result.get(key) or []):
            if isinstance(item, dict):
                text = str(item.get("task") or "").strip()
                owner = str(item.get("owner") or "").strip()
            else:
                text, owner = str(item).strip(), ""
            if text:
                points.append((key, idx, text, owner))
    return points


def verify_protocol(result: dict, transcript_text: str, user_notes: str = "",
                    provider: str | None = None, keys: dict | None = None,
                    on_progress=None, cancel_check=None) -> dict:
    """Ground the protocol in the transcript. Adds result["verification"]:

        {"mode": "strict",
         "tasks":       [{ok, quote, t, source, owner_ok} по индексу],
         "minor_tasks": [...], "done_tasks": [...], "decisions": [...]}

    and strips the owner of any task whose evidence does not support it.
    Old protocols without this key keep working — every consumer treats it as
    optional. Never raises: on engine failure the protocol ships unverified
    with verification.error explaining why."""
    points = _collect_points(result)
    ver: dict = {"mode": "strict"}
    for key in _VERIFIED_LISTS:
        ver[key] = [{"ok": False, "quote": "", "t": None, "source": None,
                     "owner_ok": False} for _ in (result.get(key) or [])]
    if not points:
        result["verification"] = ver
        return result

    backend = llm.get_provider_chain(provider, keys)
    # Long transcript → the verify chunks are big; a CPU-local engine would
    # chew each one for ~half an hour. Same rule as the analysis itself.
    if (len((transcript_text or "")) > 16000
            and (provider or "auto").split(":")[0] in ("auto", "ollama")
            and getattr(backend, "prefer_cloud", None)):
        backend.prefer_cloud()
    budget_chars = max(int(0.6 * _ctx_budget(backend)) * 3, 6000)

    # Sources in trust order: the participant's notes first (Д6), then the
    # transcript in chunks sized to the engine's window.
    sources: list[tuple[str, str]] = []
    if (user_notes or "").strip():
        sources.append(("notes", user_notes.strip()[:budget_chars]))
    # ТОЛЬКО речь. В тексте, который уходит в анализ, к расшифровке дописаны
    # блоки «УЧАСТНИКИ ЗВОНКА», «ПОСТОЯННЫЙ КОНТЕКСТ» и «ТЕКСТ С ЭКРАНА».
    # Если искать цитаты и в них, задача, выдуманная по контексту проекта,
    # получала «дословное подтверждение» из этого же контекста и помечалась
    # проверенной — то есть проверка подтверждала сама себя.
    text = _INJECTED_BLOCK_RE.sub(" ", transcript_text or "").strip()
    for i in range(0, len(text), budget_chars):
        sources.append(("transcript", text[i:i + budget_chars]))

    pending = {i: p for i, p in enumerate(points, 1)}
    calls = failed_calls = 0
    for si, (src_name, fragment) in enumerate(sources, 1):
        if not pending or ver.get("error"):
            break
        frag = _Fragment(fragment)
        items = sorted(pending.items())
        for bi in range(0, len(items), _VERIFY_BATCH):
            batch = [(n, p) for n, p in items[bi:bi + _VERIFY_BATCH] if n in pending]
            if not batch:
                continue
            if cancel_check and cancel_check():
                raise AnalysisCancelled()
            listing = "\n".join(
                f"{n}) [{key}] {text}" + (f" (ответственный: {owner})" if owner else "")
                for n, (key, idx, text, owner) in batch)
            prompt = _VERIFY_TEMPLATE.format(points=listing, fragment=fragment)
            stage = (f"Проверяю протокол по расшифровке ({si}/{len(sources)}, "
                     f"пункты {batch[0][0]}–{batch[-1][0]})…")
            calls += 1
            try:
                out = _complete_validated(backend, prompt, EvidenceList,
                                          min(4000, 150 * len(batch) + 300),
                                          on_progress, stage, cancel_check)
            except AnalysisCancelled:
                raise
            except Exception as e:  # noqa: BLE001 — verification must not kill the job
                failed_calls += 1
                ver["error"] = f"Проверка не завершена: {e}"
                break
            for item in out.get("items") or []:
                n = item.get("i")
                if n not in pending:
                    continue
                quote = str(item.get("quote") or "").strip()
                # The quote must ACTUALLY be in the transcript — an LLM paraphrase
                # is not evidence. This mechanical check keeps the pass honest.
                kind = frag.match(quote)
                if not kind:
                    continue
                # ...and it must be about THIS point, not just any real phrase.
                if not _quote_relevant(pending[n][2], quote, fragment):
                    continue
                key, idx, _text, _owner = pending.pop(n)
                ver[key][idx] = {"ok": True, "quote": quote[:300],
                                 "t": item.get("t"), "source": src_name,
                                 "owner_ok": bool(item.get("owner_ok")),
                                 "match": kind}

    ver["stats"] = {"checked": len(points), "confirmed": len(points) - len(pending),
                    "calls": calls, "failed_calls": failed_calls}
    # Acceptance rule: no owner without verbatim grounds. Unverified point →
    # flagged; verified point whose quote doesn't support the owner → owner «—».
    #
    # НО: если проверка сорвалась (движок недоступен, отмена, лимит), она ничего
    # не сказала о ещё не проверенных пунктах — снимать ответственных нельзя,
    # а помечать их «не нашлось подтверждения» — ложь. Такие пункты остаются
    # БЕЗ записи (None): интерфейс и Word показывают их как непроверенные, а не
    # как опровергнутые. Раньше сбой на первом же фрагменте оставлял все пункты
    # ok=False, и весь протокол уходил серым с «⚠ проверьте» на каждой строке.
    if ver.get("error"):
        ver["mode"] = "partial"
        for key, idx, _t, _o in pending.values():
            ver[key][idx] = None
    else:
        _strip_unfounded_owners(result, ver)
    result["verification"] = ver
    return result


def _strip_unfounded_owners(result: dict, ver: dict) -> None:
    for key in ("tasks", "minor_tasks", "done_tasks"):
        for idx, item in enumerate(result.get(key) or []):
            if not isinstance(item, dict) or not item.get("owner"):
                continue
            try:
                v = ver[key][idx]
            except (KeyError, IndexError, TypeError):
                continue                # проверки по этому пункту нет
            if not isinstance(v, dict):
                # Модель ответила не по схеме (в боевом протоколе от Yandex
                # Cloud здесь пришли строки). Снимать ответственного из-за
                # формы разметки нельзя — это молчаливая порча протокола.
                continue
            if not (v.get("ok") and v.get("owner_ok")):
                item["owner"] = ""


# --------------------------------------------------------------------------- #
# Д13: targeted re-generation and meeting Q&A
# --------------------------------------------------------------------------- #
def _window_for_topic(text: str, topic: dict, budget_chars: int) -> str:
    """Кусок расшифровки ВОКРУГ темы, а не её начало.

    Раньше бралось `text[:budget_chars]` — префикс. На длинной встрече темы
    второй половины переписывались по первой половине разговора: получалась
    вода, refill_empty_topics считал раздел пустым и УДАЛЯЛ его. То есть чем
    длиннее встреча, тем больше тем просто исчезало.

    Ищем место по словам заголовка и уже имеющегося текста темы, затем берём
    окно вокруг найденной позиции.
    """
    if len(text) <= budget_chars:
        return text
    needle = " ".join(str(topic.get(k) or "") for k in ("topic", "details"))
    words = [w for w in re.findall(r"[^\W\d_]{5,}", needle.lower(), re.UNICODE)][:12]
    low = text.lower()
    hits = [low.find(w) for w in words]
    hits = [h for h in hits if h >= 0]
    if not hits:
        return text[:budget_chars]
    centre = sorted(hits)[len(hits) // 2]          # медиана устойчивее среднего
    half = budget_chars // 2
    start = max(0, centre - half)
    return text[start:start + budget_chars]


def regen_topic_details(transcript_text: str, topic: dict,
                        provider: str | None = None, keys: dict | None = None,
                        user_notes: str = "", backend=None) -> dict:
    """Re-write ONE topic of the protocol (deeper/cleaner) without touching the
    rest. Returns {"topic", "details"}.

    `backend` позволяет переиспользовать уже выбранный движок: внутри разбора
    длинной встречи цепочка могла быть переключена на облако (prefer_cloud), и
    создавать её заново значило бы откатиться на медленный локальный движок."""
    backend = backend or llm.get_provider_chain(provider, keys)
    budget_chars = max(int(0.7 * _ctx_budget(backend)) * 3, 8000)
    text = _window_for_topic(transcript_text or "", topic, budget_chars)
    prompt = (
        "Ниже — расшифровка рабочей встречи (реплики с таймкодами [мм:сс]) и "
        "ОДНА тема её протокола. Перепиши раздел этой темы ЗАНОВО, подробнее и "
        "точнее: найди в расшифровке все места, где её обсуждали, и собери "
        "полное, самодостаточное описание на 10-14 предложений (контекст → что "
        "обсуждали, кто что предложил/возразил (по именам, только когда "
        "однозначно) → конкретика: числа, названия, сроки → чем закончилось). "
        "НЕ выдумывай ничего, чего нет в расшифровке. Таймкоды в текст не "
        "переноси. Верни ТОЛЬКО JSON {\"topic\": \"название\", \"details\": "
        "\"описание\"}.\n\n"
        f"Тема: {topic.get('topic', '')}\n"
        f"Текущее описание: {topic.get('details', '')}\n\n"
        + _with_notes("Расшифровка:\n" + text, user_notes))
    try:
        out = _complete_validated(backend, prompt, ProtoTopic, 3000,
                                  None, "Перегенерирую раздел…")
    except _SchemaMiss as e:
        out = {"topic": topic.get("topic", ""), "details": e.raw[:4000]}
    return {"topic": (out.get("topic") or topic.get("topic") or "").strip(),
            "details": (out.get("details") or "").strip()}


# Предел перегенераций за один протокол: каждая — отдельный вызов движка, а
# при упоре в лимит запросов страдает уже сам протокол. Обычно пустых разделов
# нет вовсе, так что предел срабатывает только на совсем плохих расшифровках.
_MAX_TOPIC_REGENS = int(os.getenv("VTX_MAX_TOPIC_REGENS", "6"))


def refill_empty_topics(result: dict, transcript_text: str,
                        provider: str | None = None, keys: dict | None = None,
                        user_notes: str = "", on_progress=None,
                        cancel_check=None, backend=None) -> dict:
    """Наполнить пустые разделы содержанием, а что не наполнилось — убрать.

    Пустой раздел («Обсуждаются задачи и бэклог. Решается вопрос о добавлении
    задач в бэклог») бесполезен читателю: он повторяет заголовок и не сообщает
    фактов. Пометить его мало — человек всё равно увидит воду. Поэтому раздел
    сначала переписывается по расшифровке заново, и только если и после этого
    в нём нечего сказать — он удаляется: значит тему лишь упомянули.

    Работает бережно: ошибка движка оставляет исходный раздел на месте, отмена
    пробрасывается наверх, число вызовов ограничено _MAX_TOPIC_REGENS.
    """
    topics = result.get("detailed") or []
    if not topics or not (transcript_text or "").strip():
        return result

    regenerated = dropped = 0
    kept: list[dict] = []
    for block in topics:
        topic = (block or {}).get("topic", "")
        details = (block or {}).get("details", "")
        if not protocol_quality.topic_is_empty(topic, details):
            kept.append(block)
            continue
        if regenerated >= _MAX_TOPIC_REGENS:
            kept.append(block)          # лимит исчерпан — оставляем как есть
            continue
        if cancel_check and cancel_check():
            raise AnalysisCancelled()
        if on_progress:
            on_progress(f"Дописываю раздел «{topic}»…", "")
        try:
            fresh = regen_topic_details(transcript_text, block, provider=provider,
                                        keys=keys, user_notes=user_notes,
                                        backend=backend)
        except AnalysisCancelled:
            raise
        except Exception:  # noqa: BLE001 — движок недоступен: раздел не теряем
            kept.append(block)
            continue
        regenerated += 1
        if protocol_quality.topic_is_empty(fresh.get("topic", topic),
                                           fresh.get("details", "")):
            dropped += 1               # в расшифровке действительно нет содержания
            continue
        kept.append({"topic": fresh.get("topic") or topic,
                     "details": fresh.get("details", "")})

    result["detailed"] = kept
    result["_refill"] = {"regenerated": regenerated, "dropped": dropped}
    return result


def ask_meeting(transcript_text: str, question: str,
                provider: str | None = None, keys: dict | None = None,
                user_notes: str = "") -> str:
    """Q&A over one meeting: pick the transcript windows relevant to the
    question, ask the LLM to answer WITH timecodes, admit when absent."""
    backend = llm.get_provider_chain(provider, keys)
    budget_chars = max(int(0.6 * _ctx_budget(backend)) * 3, 8000)
    text = (transcript_text or "").strip()

    # Cheap retrieval: score ~40-line windows by stem overlap with the question.
    lines = text.splitlines()
    win = 40
    q_stems = {w[:5] for w in _norm_for_match(question).split() if len(w) >= 4}
    windows: list[tuple[float, str]] = []
    for i in range(0, max(len(lines), 1), win // 2):
        chunk = "\n".join(lines[i:i + win])
        if not chunk.strip():
            continue
        cs = {w[:5] for w in _norm_for_match(chunk).split() if len(w) >= 4}
        score = len(q_stems & cs) / (len(q_stems) or 1)
        windows.append((score, chunk))
    windows.sort(key=lambda x: -x[0])
    picked, used = [], 0
    for score, chunk in windows:
        if used + len(chunk) > budget_chars:
            continue
        if score <= 0 and picked:
            break
        picked.append(chunk)
        used += len(chunk)
        if used >= budget_chars * 0.9:
            break
    fragments = "\n\n---\n\n".join(picked) or text[:budget_chars]

    prompt = (
        "Ниже — фрагменты расшифровки ОДНОЙ рабочей встречи (реплики с "
        "таймкодами [мм:сс], иногда с именем говорящего) и вопрос по этой "
        "встрече. Ответь по-русски КОНКРЕТНО и коротко (3-8 предложений), "
        "опираясь ТОЛЬКО на расшифровку"
        + (" и заметки участника" if (user_notes or "").strip() else "") + ". "
        "ОБЯЗАТЕЛЬНО укажи таймкод(ы) [мм:сс] мест, на которых основан ответ. "
        "Если в расшифровке ответа нет — прямо скажи «на встрече это не "
        "обсуждалось» и ничего не выдумывай.\n\n"
        + _with_notes("", user_notes)
        + f"Фрагменты расшифровки:\n{fragments}\n\nВопрос: {question}")
    return _stream_complete(backend, prompt, 1500, None, "Ищу ответ…",
                            force_json=False).strip()
