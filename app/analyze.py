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

import difflib
import json
import os
import re
import time

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import config, llm, protocol_quality


# --------------------------------------------------------------------------- #
# Pydantic contract for the LLM answers. One schema serves every provider:
# Ollama gets it as a structured-output grammar (can't produce invalid JSON),
# cloud providers get their answer validated against it with one retry.
# Lenient by design (extra fields ignored, everything defaulted) — a weak
# model's imperfect answer should degrade, not explode.
# --------------------------------------------------------------------------- #
class TopicNote(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    t: str | None = None
    topic: str = ""
    details: str = ""
    quotes: list[str] = Field(default_factory=list)


class DecisionNote(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    t: str | None = None
    text: str = ""
    quote: str | None = None


class TaskNote(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    t: str | None = None
    task: str = ""
    owner: str | None = None
    owner_evidence: str | None = None
    done: bool = False


class MapNotes(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    time_range: str = ""
    participants: list[str] = Field(default_factory=list)
    topics: list[TopicNote] = Field(default_factory=list)
    decisions: list[DecisionNote] = Field(default_factory=list)
    tasks: list[TaskNote] = Field(default_factory=list)


# Final-protocol models are deliberately null-tolerant: a weak cloud model
# answering owner:null must be normalised to «—» downstream, not bounced into
# a needless retry round.
class ProtoParticipant(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    name: str | None = ""
    role: str | None = ""


class ProtoTopic(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    topic: str | None = ""
    details: str | None = ""


class ProtoTask(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    task: str | None = ""
    owner: str | None = "—"


class Protocol(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    participants: list[ProtoParticipant] = Field(default_factory=list)
    summary: str | None = ""
    detailed: list[ProtoTopic] = Field(default_factory=list)
    key_thoughts: list[str | None] = Field(default_factory=list)
    conclusions: list[str | None] = Field(default_factory=list)
    decisions: list[str | None] = Field(default_factory=list)
    done_tasks: list[ProtoTask] = Field(default_factory=list)
    tasks: list[ProtoTask] = Field(default_factory=list)
    minor_tasks: list[ProtoTask] = Field(default_factory=list)

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
    '  "decisions": ["содержательное решение — ЧТО выбрали/утвердили/отклонили/отложили '
    '(не пересказ задач и не «кто что сделает»)", "..."],\n'
    '  "done_tasks": [{{"task": "что уже сделано/выполнено", "owner": "кто сделал, или —"}}],\n'
    '  "tasks": [{{"task": "крупная задача, которую нужно сделать", "owner": "ответственный (имя/роль), или —"}}],\n'
    '  "minor_tasks": [{{"task": "мелкая задача/доработка", "owner": "ответственный, или —"}}]\n'
    "}}"
)

_RULES = (
    "ГЛАВНОЕ — ТОЧНОСТЬ И ПОЛНОТА. Опирайся СТРОГО на текст: ничего не выдумывай и "
    "не додумывай. КАЖДОЕ утверждение протокола должно опираться на конкретные слова "
    "из расшифровки или на блок «ТЕКСТ С ЭКРАНА» — никаких предположений, домыслов, "
    "обобщений «от себя» и шаблонных фраз, которых не было на встрече. "
    "Пройди по ВСЕЙ встрече от начала до конца и не упусти ни одной "
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
    "- participants: если в конце текста есть блок «УЧАСТНИКИ ЗВОНКА (распознано с "
    "видео)» — это ДОСТОВЕРНЫЙ список подключённых к звонку, считанный с подписей "
    "плиток. В participants перечисли РОВНО этих людей: никого не добавляй, никого не "
    "выбрасывай и НЕ угадывай участников по речи. Исключение: пропусти строку, которая "
    "очевидно не имя (бот записи, название встречи, элемент интерфейса). Роли — только "
    "если ясны из разговора. Если такого блока НЕТ — определи участников по тексту: по "
    "обращениям («Кирилл, …»), самопредставлениям и упоминаниям; если имя не названо — "
    "пиши «Спикер 1», «Спикер 2».\n"
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
    "- Если в тексте есть блок «ПОСТОЯННЫЙ КОНТЕКСТ» — это заданные пользователем "
    "сведения об участниках, их ролях, названиях и сути проектов. Используй его, "
    "чтобы правильно понимать, КТО есть кто, что означают названия проектов/систем и "
    "о чём они, и не путать роли — особенно если протокол читает человек, которого не "
    "было на встрече. НО это справочный контекст, а НЕ содержание встречи: не переноси "
    "его в решения/задачи и не выдумывай на его основе того, чего на встрече не было.\n"
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
    "  ЗАПРЕЩЕНО делать из summary ОГЛАВЛЕНИЕ. Не перечисляй названия тем через запятую "
    "и не пиши «обсуждались различные темы, включая…». Такой текст бесполезен: он "
    "сообщает, о чём ГОВОРИЛИ, но не сообщает, ЧТО СКАЗАЛИ. Пиши по существу: в чём была "
    "проблема, какие решения приняли, что меняется дальше — с конкретикой (названия, "
    "цифры, сроки). Плохо: «Встреча была посвящена обсуждению различных тем, включая "
    "повестку дня, бэклог и задачи, поиск дизайнеров». Хорошо: «Разбирали, почему "
    "тренажёры не проходят проверку доступности: у изображений нет альтернативного "
    "текста, а часть кнопок недоступна с клавиатуры. Решили сначала закрыть alt-тексты, "
    "клавиатурную навигацию вынесли в отдельную задачу на следующую неделю».\n"
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
    "открытым). ОБЯЗАТЕЛЬНО перечисли ВСЕ шаги и действия, которые проговаривались по "
    "этой теме: что уже сделано, что и в каком порядке решили делать дальше — так, чтобы "
    "по описанию было ясно, ЧТО СДЕЛАНО и ЧТО НАДО ДЕЛАТЬ. Авторов реплик подставляй "
    "только там, где это однозначно ясно (см. правило про авторство). Пиши так, чтобы, "
    "прочитав ТОЛЬКО этот абзац, человек, которого не было на встрече, полностью понял "
    "тему. НЕ обобщай и НЕ сокращай — это главная часть протокола.\n"
    "      ЗАПРЕЩЕНО пересказывать заголовок вместо содержания. Конструкции вида "
    "«Обсуждается X. Решается вопрос о X», «Затрагивается тема Y», «Обсуждаются вопросы, "
    "связанные с Z» — это пустышки: они повторяют название темы и не сообщают ни одного "
    "факта. В КАЖДОМ details должно быть что-то, чего НЕТ в topic: что конкретно "
    "прозвучало, какие цифры/сроки/названия назвали, какие были доводы и чем кончилось. "
    "Плохо: «Бэклог и задачи» → «Обсуждаются задачи и бэклог. Решается вопрос о "
    "добавлении задач в бэклог». Хорошо: «Бэклог и задачи» → «Часть задач со стендапа не "
    "попадала в бэклог, поэтому терялась. Договорились, что задачу заводит тот, кто её "
    "озвучил, сразу на встрече; разбор бэклога вынесли на пятницу».\n"
    "      Если по теме в расшифровке НЕТ содержания (её только упомянули) — НЕ создавай "
    "для неё отдельный пункт detailed. Лучше меньше тем, но по каждой есть что сказать.\n"
    "- key_thoughts: 8-15 ключевых тезисов и важных формулировок (по возможности — "
    "с указанием, кто их высказал).\n"
    "- conclusions: ВЫВОДЫ и итоги — к чему в целом пришла команда, оценка статуса/"
    "ситуации, общие заключения (4-8 пунктов).\n"
    "- decisions: решения ПО СУЩЕСТВУ — что выбрали, утвердили, отклонили или "
    "отложили (вариант дизайна, границы функционала, «X откладываем до Y», "
    "«оставляем такой-то подход»). НЕ дублируй сюда задачи-поручения: «Кирилл "
    "сделает Z» — это tasks, а НЕ решение. Итоговое резюме встречи («итого: кто "
    "что делает») — тоже НЕ решения, не переписывай его сюда. Пустой список, "
    "если содержательных решений не было.\n"
    "- done_tasks: что УЖЕ СДЕЛАНО/выполнено к моменту встречи (готово, закрыто, "
    "починено). owner — кто это сделал, ТОЛЬКО если в тексте прямо сказано, кто именно "
    "(«я починил», «Сергей выкатил»). Иначе owner: '—'.\n"
    "- ПРИОРИТЕТ ПРИ НЕХВАТКЕ МЕСТА: задачи, решения и договорённости — самое ценное в "
    "протоколе, ради них его и читают. НИКОГДА не выбрасывай их ради краткости. Если "
    "чувствуешь, что ответ становится слишком длинным, — сокращай описания тем "
    "(detailed), но списки decisions / done_tasks / tasks / minor_tasks выписывай "
    "ПОЛНОСТЬЮ. Пустой список задач по рабочей встрече почти всегда означает, что их "
    "просто не выписали, а не что их не было.\n"
    "- tasks: что НУЖНО СДЕЛАТЬ — крупные задачи на будущее. owner ставь ТОЛЬКО при "
    "явном указании: человек сам взял задачу («я возьму / беру / сделаю / за мной») или "
    "её поручили конкретному человеку по имени («Кирилл, сделай…», «давай это на тебе»). "
    "НЕ назначай ответственным того, кто просто предложил задачу, рассказал о ней или "
    "активно её обсуждал. Если в тексте нет однозначного «кто делает» — owner: '—'. "
    "Пустой owner — это нормально и правильно, когда ответственного не назначили вслух.\n"
    "- minor_tasks: ОБЯЗАТЕЛЬНО выпиши и мелкие, второстепенные задачи и доработки "
    "(мелкие правки UI, договорённости об именовании — названия тегов/кнопок/сущностей, "
    "кто кому что скинет/даст доступ). owner — по тем же строгим правилам (или '—'). "
    "После основного разбора ПРОЙДИ ПО РЕПЛИКАМ ЕЩЁ РАЗ в поисках именно МИМОХОДНЫХ "
    "поручений («выдам доступ», «скину файл», «переименуйте…») — они теряются чаще "
    "всего; каждое такое обещание или просьба — отдельная задача.\n"
    "- owner — это конкретный человек или роль (например: «Сергей», «Кирилл», "
    "«дизайнер», «бэкенд»). НЕ выдумывай имена и НЕ угадывай — бери только из текста и "
    "только при явном назначении. Сомневаешься — ставь '—'.\n"
    "- Не путай сделанное с тем, что нужно сделать: done_tasks — прошедшее время, "
    "tasks и minor_tasks — будущее.\n"
    "- Метки времени [мм:сс] в расшифровке/заметках — служебные: используй их, чтобы "
    "сохранить ХРОНОЛОГИЮ (порядок тем в detailed = порядок по времени встречи), но "
    "НЕ переноси сами таймкоды в текст протокола.\n"
    "- Всё на русском языке. Верни ТОЛЬКО JSON, без markdown и пояснений."
)

_PROMPT_TEMPLATE = (
    "Проанализируй транскрипцию рабочей встречи (автоматическая расшифровка, возможны "
    "ошибки распознавания имён и терминов) и верни ответ ТОЛЬКО в виде JSON.\n\n"
    "Транскрипция:\n{transcript}\n\n"
    "Формат ответа:\n" + _SCHEMA + "\n\n" + _RULES
)

# Map step: extract one chunk into STRUCTURED notes with timecodes. Structure
# (instead of flat prose) is what lets the reduce step keep the meeting's
# chronology, dedup overlap-zone tasks mechanically and never lose late tasks.
_MAP_SCHEMA = (
    "{{\n"
    '  "time_range": "мм:сс–мм:сс — какой отрезок встречи покрывает фрагмент (по меткам [мм:сс])",\n'
    '  "participants": ["имена, проявившиеся в этом фрагменте"],\n'
    '  "topics": [{{"t": "мм:сс — таймкод начала темы", "topic": "информативное название темы",\n'
    '    "details": "подробное описание на 8-12 предложений: контекст, что обсуждали, кто что '
    'предложил/возразил (по именам, только когда однозначно), конкретика (числа, названия, сроки), '
    'к чему пришли", "quotes": ["1-2 дословные ключевые фразы из фрагмента"]}}],\n'
    '  "decisions": [{{"t": "мм:сс", "text": "что решили/договорились", '
    '"quote": "дословная фраза-основание"}}],\n'
    '  "tasks": [{{"t": "мм:сс", "task": "что сделать (включая мелкие задачи)", '
    '"owner": "имя, ТОЛЬКО если явно взял/поручили, иначе null", '
    '"owner_evidence": "дословная фраза, из которой видно, КТО берёт задачу, иначе null", '
    '"done": false}}]\n'
    "}}"
)

_MAP_TEMPLATE = (
    "Это часть {i} из {n} расшифровки рабочей встречи (автоматическая, возможны "
    "ошибки распознавания). Реплики размечены таймкодами вида [мм:сс] (иногда с "
    "именем говорящего — тогда это ДОСТОВЕРНЫЙ автор реплики, считанный с видео). "
    "Извлеки из ЭТОГО фрагмента структурированные заметки и верни ТОЛЬКО JSON:\n\n"
    + _MAP_SCHEMA + "\n\n"
    "Правила:\n"
    "- Таймкоды t бери из меток [мм:сс] рядом с местом, где тема/решение/задача "
    "прозвучали. time_range — от первой до последней метки фрагмента.\n"
    "- topics: раздели фрагмент на реальные темы В ХРОНОЛОГИЧЕСКОМ ПОРЯДКЕ; details "
    "пиши подробно, НЕ сжимай до одной строки — по ним будут восстанавливать тему.\n"
    "- tasks: выпиши ВСЕ задачи, включая мелкие (правки UI, названия тегов/кнопок, "
    "кто кому что скинет). done=true — если задача уже СДЕЛАНА (прошедшее время).\n"
    "- owner: ТОЛЬКО при явном назначении в тексте («я возьму», «за мной», «Кирилл, "
    "сделай»). owner_evidence — та самая дословная фраза. Нет фразы → owner=null, "
    "owner_evidence=null. НЕ назначай того, кто просто рассказывал о задаче.\n"
    "- Если во фрагменте есть «ТЕКСТ С ЭКРАНА» — отрази показанное в topics.\n"
    "- Сохраняй дословно имена, числа, проценты, сроки, названия, термины. Ничего "
    "не выдумывай. Всё существенное из фрагмента должно попасть в заметки.\n"
    "- Верни ТОЛЬКО валидный JSON без markdown.\n\n"
    "Фрагмент:\n{chunk}"
)

# Pairwise merge for the hierarchical reduce: when all the map notes together
# don't fit the engine's context, neighbours are first merged pairwise (same
# schema, chronology preserved) until the final reduce fits.
_MERGE_TEMPLATE = (
    "Ниже — структурированные заметки по ДВУМ ПОСЛЕДОВАТЕЛЬНЫМ отрезкам одной "
    "рабочей встречи (JSON). Объедини их в ОДИН JSON ТОЙ ЖЕ схемы:\n"
    "- time_range — от начала первого до конца второго;\n"
    "- topics — все темы обоих отрезков в хронологическом порядке (по t); одну и ту "
    "же тему, продолжившуюся во втором отрезке, слей в одну (t — начало), сохранив "
    "детали обеих частей; details можно уплотнять, но БЕЗ потери фактов, имён, "
    "чисел и договорённостей;\n"
    "- decisions и tasks — ВСЕ пункты обоих отрезков (кроме точных дублей), с их t, "
    "owner и owner_evidence как есть; НИЧЕГО не выбрасывай и не сокращай;\n"
    "- participants — объединение.\n"
    "Верни ТОЛЬКО валидный JSON без markdown.\n\n"
    "Отрезок A:\n{a}\n\nОтрезок B:\n{b}"
)

# Reduce step: merge the structured chunk notes into the final protocol JSON.
_REDUCE_TEMPLATE = (
    "Ниже — структурированные заметки (JSON) по последовательным частям одной "
    "рабочей встречи, В ХРОНОЛОГИЧЕСКОМ ПОРЯДКЕ, с таймкодами t. Дубликаты задач "
    "из зон перекрытия частей уже удалены. Собери из них ЕДИНЫЙ ПОДРОБНЫЙ протокол:\n"
    "- порядок тем в detailed = ХРОНОЛОГИЯ встречи (по t из заметок); темы, "
    "продолжавшиеся в нескольких частях, слей в одну, сохранив детали всех частей;\n"
    "- СОХРАНИ ВСЕ решения и задачи из заметок (включая мелкие) — каждая задача из "
    "заметок должна попасть в tasks / minor_tasks / done_tasks (done=true → "
    "done_tasks); НЕ выбрасывай и не сокращай содержательные пункты;\n"
    "- owner бери из заметок ТОЛЬКО там, где есть owner_evidence; owner без "
    "owner_evidence считай неназначенным (owner: '—');\n"
    "- таймкоды и поля quotes/owner_evidence — служебные: используй их для порядка "
    "и проверки, но В ТЕКСТ протокола не переноси;\n"
    "- объединяй сведения об участниках из всех частей.\n"
    "Верни ответ ТОЛЬКО в виде JSON.\n\n"
    "Заметки по частям:\n{notes}\n\n"
    "Формат ответа:\n" + _SCHEMA + "\n\n" + _RULES
)


# Д11: protocol presets — the same JSON schema, but the emphasis matches the
# meeting type. Picked per job, or auto-detected from the meeting title.
PROTOCOL_PRESETS: dict[str, dict] = {
    "universal": {"label": "Универсальный", "rules": ""},
    "planerka": {
        "label": "Планёрка / статус",
        "keywords": ("планёрк", "планерк", "стендап", "стэндап", "standup",
                     "статус", "еженедельн", "синк", "sync"),
        "rules": (
            "ТИП ВСТРЕЧИ: ПЛАНЁРКА (статус-встреча). Приоритет протокола — "
            "ЗАДАЧИ И СТАТУСЫ: по каждому участнику, который отчитывался, — что "
            "СДЕЛАНО (done_tasks), что В РАБОТЕ и что он ВОЗЬМЁТ дальше (tasks), "
            "и какие у него БЛОКЕРЫ (вынеси блокеры отдельными пунктами в "
            "key_thoughts с пометкой «Блокер:»). Сроки фиксируй дословно. "
            "Длинные обсуждения сворачивай — здесь важнее полный список задач, "
            "чем детальный пересказ дискуссий."),
    },
    "design": {
        "label": "Обсуждение / дизайн",
        "keywords": ("дизайн", "обсужден", "проектирован", "архитектур",
                     "брейншторм", "мастерская", "review", "ревью"),
        "rules": (
            "ТИП ВСТРЕЧИ: ОБСУЖДЕНИЕ/ДИЗАЙН. Приоритет — РЕШЕНИЯ И АРГУМЕНТЫ: "
            "в detailed по каждой теме разверни рассмотренные ВАРИАНТЫ, кто "
            "какие аргументы «за/против» приводил, и почему выбрали то, что "
            "выбрали. ОТКРЫТЫЕ ВОПРОСЫ (не решили, отложили, надо исследовать) "
            "вынеси отдельными пунктами в conclusions с пометкой «Открыто:». "
            "decisions — только зафиксированные выборы."),
    },
    "demo": {
        "label": "Демо / показ",
        "keywords": ("демо", "demo", "показ", "презентац"),
        "rules": (
            "ТИП ВСТРЕЧИ: ДЕМО. Приоритет: ЧТО ПОКАЗЫВАЛИ (по шагам, в "
            "detailed), какие ВОПРОСЫ И РЕАКЦИИ были у смотревших (key_thoughts, "
            "с именами, где однозначно), и какие ДОГОВОРЁННОСТИ и доработки из "
            "этого родились (decisions/tasks). Замечания «поправить/изменить» — "
            "каждое отдельной задачей."),
    },
    "one_on_one": {
        "label": "1:1",
        "keywords": ("1:1", "1-1", "один на один", "one-on-one", "тет-а-тет"),
        "rules": (
            "ТИП ВСТРЕЧИ: 1:1 (разговор двоих). Приоритет — ДОГОВОРЁННОСТИ: "
            "что решили и кто что делает к следующей встрече. Темы разговора "
            "перечисли кратко (detailed по 3-5 предложений, без развёрнутых "
            "пересказов — разговор личный). Оценочные и чувствительные "
            "формулировки смягчай до фактов."),
    },
}


def preset_rules(name: str | None, custom: dict | None = None) -> str:
    """The extra prompt rules of a preset; '' for universal/unknown.
    `custom` is the team's own presets {name: rules} — they win over builtins."""
    name = (name or "").strip()
    if custom and name in custom:
        return str(custom[name] or "")
    return str((PROTOCOL_PRESETS.get(name) or {}).get("rules") or "")


def preset_for_title(title: str | None) -> str:
    """Auto-pick a preset from the meeting title (scheduler's «авто» mode)."""
    low = (title or "").lower()
    for name, p in PROTOCOL_PRESETS.items():
        if any(k in low for k in p.get("keywords") or ()):
            return name
    return "universal"


# Д6: the participant's own live notes are the most trustworthy input — a human
# wrote them DURING the meeting. They become the protocol's skeleton; the
# transcript fills in the details.
_NOTES_BLOCK = (
    "=== ЗАМЕТКИ УЧАСТНИКА ВСТРЕЧИ ===\n"
    "Написаны человеком ВО ВРЕМЯ встречи — это САМЫЙ ДОСТОВЕРНЫЙ источник. "
    "При противоречии с расшифровкой приоритет у заметок (расшифровка — "
    "автоматическая и может ошибаться). Используй заметки как СКЕЛЕТ протокола: "
    "каждая тема, решение и задача из заметок ОБЯЗАНА попасть в протокол; "
    "расшифровка служит для деталей, подробностей и формулировок.\n"
    "{notes}\n"
    "=== КОНЕЦ ЗАМЕТОК ===\n\n"
)


def _with_notes(prompt: str, user_notes: str) -> str:
    notes = (user_notes or "").strip()
    if not notes:
        return prompt
    return _NOTES_BLOCK.format(notes=notes[:8000]) + prompt


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
                    low = txt.lower()
                    for s2, low2, prev in seen:
                        close = (sec is None or s2 is None
                                 or abs(sec - s2) <= _DEDUP_WINDOW_SEC)
                        if close and difflib.SequenceMatcher(
                                None, low, low2).ratio() >= _DEDUP_RATIO:
                            dup = prev
                            break
                if dup is not None:
                    if not dup.get("owner") and it.get("owner"):
                        dup["owner"] = it["owner"]
                        dup["owner_evidence"] = it.get("owner_evidence")
                    if it.get("done"):
                        dup["done"] = True
                    continue
                kept.append(it)
                seen.append((sec, txt.lower(), it))
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
    r"\n*===\s*(УЧАСТНИКИ ЗВОНКА|ПОСТОЯННЫЙ КОНТЕКСТ|ТЕКСТ С ЭКРАНА)"
    r"[\s\S]*?(?=\n\s*===|\Z)", re.IGNORECASE)
# Timecodes are structure, not content: "[00:12]" must not count as speech.
_TIMECODE_RE = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?\]")
# Minimum genuinely spoken words before a protocol may be generated. A meeting
# worth a protocol always clears this; failed recognition never does.
MIN_SPEECH_WORDS = int(os.getenv("VTX_MIN_SPEECH_WORDS", "40"))


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
        return model_cls.model_validate(_extract_json(raw)).model_dump()
    except (ValidationError, ValueError) as e:
        err = str(e)[:600]
    retry = (prompt + "\n\nТвой прошлый ответ не прошёл проверку схемы: "
             + err + "\nВерни ИСПРАВЛЕННЫЙ ответ строго по требуемой JSON-схеме, "
             "без markdown и пояснений.")
    raw = _stream_complete(backend, retry, max_tokens, on_progress, stage,
                           cancel_check=cancel_check, json_schema=schema)
    try:
        return model_cls.model_validate(_extract_json(raw)).model_dump()
    except (ValidationError, ValueError) as e:
        raise _SchemaMiss(f"Ответ модели не прошёл валидацию схемы: {e}", raw) from e


class _SchemaMiss(ValueError):
    """Both attempts failed validation; carries the last raw answer so the
    caller can degrade gracefully instead of losing the model's work."""

    def __init__(self, msg: str, raw: str = ""):
        super().__init__(msg)
        self.raw = raw or ""


def analyze_transcript(transcript_text: str, provider: str | None = None,
                       extra_instructions: str = "", custom_prompt: str = "",
                       on_progress=None, cancel_check=None, keys: dict | None = None,
                       user_notes: str = "") -> dict:
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
    if len(text) <= _MAX_CHARS:
        if custom:
            prompt = _with_notes(custom + "\n\nТранскрипция:\n" + text, user_notes)
            raw = _stream_complete(backend, _with_extra(prompt, extra_instructions),
                                   10000, on_progress, "Генерация протокола…",
                                   cancel_check=cancel_check)
            result = _extract_json(raw)
        else:
            prompt = _with_extra(
                _with_notes(_PROMPT_TEMPLATE.format(transcript=text), user_notes),
                extra_instructions)
            try:
                result = _complete_validated(backend, prompt, Protocol, 10000,
                                             on_progress, "Генерация протокола…",
                                             cancel_check)
            except _SchemaMiss as e:
                result = _extract_json(e.raw)  # lenient legacy path
    else:
        chunks = _split_chunks(text)
        n = len(chunks)
        maps: list[dict] = []
        for i, chunk in enumerate(chunks, 1):
            _ck()  # cancel between chunks
            stage = f"Читаю встречу: часть {i} из {n}…"
            try:
                m = _complete_validated(
                    backend, _MAP_TEMPLATE.format(i=i, n=n, chunk=chunk),
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
                custom + "\n\nНиже — структурированные заметки (JSON) по "
                "последовательным частям встречи, в хронологическом порядке; "
                "объедини их в итог по требованиям выше:\n" + notes, user_notes)
            raw = _stream_complete(backend, _with_extra(prompt, extra_instructions),
                                   10000, on_progress, "Свожу протокол…",
                                   cancel_check=cancel_check)
            result = _extract_json(raw)
        else:
            prompt = _with_extra(
                _with_notes(_REDUCE_TEMPLATE.format(notes=notes), user_notes),
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
                result = _extract_json(e.raw)  # lenient legacy path

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
    # Читаемость протокола — измеримая величина, а не ощущение. Пустые разделы
    # («Обсуждается X. Решается вопрос о X») и summary-оглавление кладём в
    # результат: видно в UI, считается на эталонах, не даёт дефекту тихо расти.
    result["_quality"] = protocol_quality.report(result)
    if warning:
        result["_warning"] = warning
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
_STEM_LEN = 5   # crude RU stemming: compare word prefixes, so «фильтры»≈«фильтров»


def _stems(text: str) -> set[str]:
    return {w[:_STEM_LEN] for w in _norm_for_match(text).split() if len(w) >= 4}


def _quote_relevant(point_text: str, quote: str, fragment: str) -> bool:
    """A verbatim quote must also be ABOUT the point: a weak model happily
    attaches a real quote to an invented task, and verbatimness alone passes it.
    Demand a shared significant word (by stem) between the point and the quote
    or its line in the fragment (the line catches anaphoric «я возьму это»)."""
    pw = _stems(point_text)
    if not pw:
        return True
    ctx = _stems(quote)
    qn = _norm_for_match(quote)
    for line in fragment.splitlines():
        if qn in _norm_for_match(line):
            ctx |= _stems(line)
            break
    return bool(pw & ctx)


def _norm_for_match(s: str) -> str:
    """Дословность проверяем механически: цитата обязана быть подстрокой
    источника после нормализации (регистр/пробелы/пунктуация/ё)."""
    s = re.sub(r"[^\w\s]", " ", (s or "").lower().replace("ё", "е"))
    return re.sub(r"\s+", " ", s).strip()


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
    text = (transcript_text or "").strip()
    for i in range(0, len(text), budget_chars):
        sources.append(("transcript", text[i:i + budget_chars]))

    pending = {i: p for i, p in enumerate(points, 1)}
    for si, (src_name, fragment) in enumerate(sources, 1):
        if not pending:
            break
        if cancel_check and cancel_check():
            raise AnalysisCancelled()
        listing = "\n".join(
            f"{n}) [{key}] {text}" + (f" (ответственный: {owner})" if owner else "")
            for n, (key, idx, text, owner) in sorted(pending.items()))
        prompt = _VERIFY_TEMPLATE.format(points=listing, fragment=fragment)
        stage = f"Проверяю протокол по расшифровке ({si}/{len(sources)})…"
        try:
            out = _complete_validated(backend, prompt, EvidenceList, 3000,
                                      on_progress, stage, cancel_check)
        except AnalysisCancelled:
            raise
        except Exception as e:  # noqa: BLE001 — verification must not kill the job
            ver["error"] = f"Проверка не завершена: {e}"
            break
        frag_norm = _norm_for_match(fragment)
        for item in out.get("items") or []:
            n = item.get("i")
            if n not in pending:
                continue
            quote = str(item.get("quote") or "").strip()
            # The quote must ACTUALLY be verbatim — an LLM paraphrase is not
            # evidence. This mechanical check is what makes the pass honest.
            if (len(quote) < _MIN_QUOTE_CHARS
                    or _norm_for_match(quote) not in frag_norm):
                continue
            # ...and it must be about THIS point, not just any real phrase.
            if not _quote_relevant(pending[n][2], quote, fragment):
                continue
            key, idx, _text, _owner = pending.pop(n)
            ver[key][idx] = {"ok": True, "quote": quote[:300],
                             "t": item.get("t"), "source": src_name,
                             "owner_ok": bool(item.get("owner_ok"))}

    # Acceptance rule: no owner without verbatim grounds. Unverified point →
    # flagged; verified point whose quote doesn't support the owner → owner «—».
    for key in ("tasks", "minor_tasks", "done_tasks"):
        for idx, item in enumerate(result.get(key) or []):
            if not isinstance(item, dict) or not item.get("owner"):
                continue
            v = ver[key][idx]
            if not (v["ok"] and v["owner_ok"]):
                item["owner"] = ""
    result["verification"] = ver
    return result


# --------------------------------------------------------------------------- #
# Д13: targeted re-generation and meeting Q&A
# --------------------------------------------------------------------------- #
def regen_topic_details(transcript_text: str, topic: dict,
                        provider: str | None = None, keys: dict | None = None,
                        user_notes: str = "") -> dict:
    """Re-write ONE topic of the protocol (deeper/cleaner) without touching the
    rest. Returns {"topic", "details"}."""
    backend = llm.get_provider_chain(provider, keys)
    budget_chars = max(int(0.7 * _ctx_budget(backend)) * 3, 8000)
    text = (transcript_text or "")[:budget_chars]
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
