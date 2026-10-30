"""Похожие обсуждения на прошлых встречах — по СОДЕРЖАНИЮ текущей (фаза 2
RAG, п. 7): ретривер `meeting_index.related_block`, ключевые слова без модели
`salient_queries`, хук `retrieve` в `analyze_transcript` и его место в
промпте (одиночный проход и сведение — да, карта — нет)."""
from __future__ import annotations

import json
import time

import pytest

from app import analyze, db, meeting_index as mi
from app.analyze import MapNotes, TaskNote, TopicNote, DecisionNote


# --------------------------------------------------------------------------- #
# Ретривер
# --------------------------------------------------------------------------- #
def _row(doc, idx, text, *, series="гранты внутренняя", date="01.09.2026",
         ts=1.0, rank=0.5, title="Гранты — внутренняя"):
    return {"doc_id": doc, "idx": idx, "text": text, "series_key": series,
            "meeting_date": date, "meeting_ts": ts, "rank": rank, "title": title,
            "kind": "protocol", "ts": "", "is_draft": False}


class _Search:
    """Подмена db.chunks_search: пишет каждый вызов, отдаёт по сценарию
    (ключ — series_key или None)."""

    def __init__(self, by_scope):
        self.by_scope = by_scope
        self.calls = []

    def __call__(self, team, q, limit=24, series_key=None, kinds=None):
        self.calls.append({"team": team, "q": q, "limit": limit,
                           "series_key": series_key, "kinds": kinds})
        return [dict(r) for r in self.by_scope.get(series_key, [])]


class TestRelatedBlock:
    def test_сначала_серия_потом_команда_и_формулировка_запроса(self, monkeypatch):
        s = _Search({"гранты внутренняя": [_row("a", 0, "О чём шёл разговор: про бюджет сайта.")],
                     None: [_row("b", 1, "Решения и договорённости:\n- Бюджет сайта 500 тысяч",
                                 series="встреча лидеров", title="Встреча лидеров")]})
        monkeypatch.setattr(db, "chunks_search", s)
        blk = mi.related_block("alice", "гранты внутренняя",
                               ["Бюджет сайта и сроки редизайна"], exclude_doc="cur")
        assert [c["series_key"] for c in s.calls] == ["гранты внутренняя", None]
        assert all(c["kinds"] == ("protocol",) and c["team"] == "alice" for c in s.calls)
        assert s.calls[0]["q"] == "бюджет or сайта or сроки or редизайна"
        assert blk.startswith(mi.RELATED_HEADER)
        assert "НЕ переноси в решения/задачи" in blk
        assert "• Гранты — внутренняя, 01.09.2026: О чём шёл разговор: про бюджет сайта." in blk
        # Переносы строк куска схлопнуты — одна встреча-кусок = одна строка.
        assert "• Встреча лидеров, 01.09.2026: Решения и договорённости: - Бюджет сайта 500 тысяч" in blk

    def test_серии_хватает_двух_встреч_без_поиска_по_команде(self, monkeypatch):
        s = _Search({"серия": [_row("a", 0, "раз"), _row("b", 0, "два")],
                     None: [_row("c", 0, "чужое")]})
        monkeypatch.setattr(db, "chunks_search", s)
        blk = mi.related_block("alice", "серия", ["тема"])
        assert [c["series_key"] for c in s.calls] == ["серия"]
        assert "чужое" not in blk

    def test_без_серии_ищется_только_по_команде(self, monkeypatch):
        s = _Search({None: [_row("a", 0, "раз")]})
        monkeypatch.setattr(db, "chunks_search", s)
        assert "раз" in mi.related_block("alice", "", ["тема"])
        assert [c["series_key"] for c in s.calls] == [None]

    def test_текущая_встреча_исключается_и_не_считается_найденной(self, monkeypatch):
        # В серии нашлась только сама текущая встреча → идём по команде.
        s = _Search({"серия": [_row("cur", 0, "это мы сами")],
                     None: [_row("cur", 1, "и это мы"), _row("old", 0, "старое")]})
        monkeypatch.setattr(db, "chunks_search", s)
        blk = mi.related_block("alice", "серия", ["тема"], exclude_doc="cur")
        assert "мы" not in blk and "старое" in blk
        assert len(s.calls) == 2

    def test_дедуп_по_doc_id_не_больше_двух_кусков_и_четырёх_встреч(self, monkeypatch):
        rows = []
        for d in range(6):                      # шесть встреч по три куска
            for i in range(3):
                rows.append(_row(f"d{d}", i, f"встреча {d} кусок {i}", ts=100 - d))
        s = _Search({"серия": rows})
        monkeypatch.setattr(db, "chunks_search", s)
        blk = mi.related_block("alice", "серия", ["тема первая", "тема вторая"],
                               budget_chars=100_000)
        lines = [ln for ln in blk.splitlines() if ln.startswith("• ")]
        assert len(lines) == 8                  # 4 встречи × 2 куска
        docs = {ln.split("кусок")[0].split("встреча ")[1].strip() for ln in lines}
        assert docs == {"0", "1", "2", "3"}     # при равном ранге — самые свежие
        assert blk.count("кусок 2") == 0        # третий кусок встречи отброшен

    def test_кусок_найденный_двумя_запросами_выше_и_свежая_при_равном_ранге(self, monkeypatch):
        calls = {"n": 0}

        def search(team, q, limit=24, series_key=None, kinds=None):
            calls["n"] += 1
            if "сайт" in q:
                return [_row("old", 0, "старый сайт", ts=1.0, rank=0.9),
                        _row("new", 0, "новый сайт", ts=9.0, rank=0.9)]
            return [_row("old", 0, "старый сайт", ts=1.0, rank=0.1)]
        monkeypatch.setattr(db, "chunks_search", search)
        blk = mi.related_block("alice", "", ["сайт", "бюджет"])
        lines = [ln for ln in blk.splitlines() if ln.startswith("• ")]
        assert "старый" in lines[0] and "новый" in lines[1]   # два попадания выше
        blk = mi.related_block("alice", "", ["сайт"])
        lines = [ln for ln in blk.splitlines() if ln.startswith("• ")]
        assert "новый" in lines[0]                              # равный ранг — свежая выше

    def test_бюджет_символов_и_обрезка_куска(self, monkeypatch):
        s = _Search({None: [_row("a", 0, "слово " * 200), _row("b", 0, "второй")]})
        monkeypatch.setattr(db, "chunks_search", s)
        blk = mi.related_block("alice", "", ["тема"], budget_chars=570)
        lines = [ln for ln in blk.splitlines() if ln.startswith("• ")]
        assert len(lines) == 1 and lines[0].endswith("…")
        assert len(lines[0]) <= 400 + 60 and len(blk) <= 570
        # Бюджет, в который не влезает ни одна строка, → пустой блок, не шапка.
        assert mi.related_block("alice", "", ["тема"], budget_chars=120) == ""

    def test_пусто_и_служебные_запросы(self, monkeypatch):
        s = _Search({None: []})
        monkeypatch.setattr(db, "chunks_search", s)
        assert mi.related_block("alice", "", ["тема"]) == ""
        s.calls.clear()
        # «что решили» — одни стоп-слова: к базе не ходим вовсе.
        assert mi.related_block("alice", "", ["что решили", ""]) == ""
        assert s.calls == []

    def test_ошибка_базы_даёт_пустоту_и_предупреждение(self, monkeypatch, caplog):
        def boom(*a, **k):
            raise RuntimeError("база недоступна")
        monkeypatch.setattr(db, "chunks_search", boom)
        with caplog.at_level("WARNING", logger="vtx.meeting_index"):
            assert mi.related_block("alice", "серия", ["тема"]) == ""
        assert "Похожие обсуждения не собраны" in caplog.text

    def test_не_больше_восьми_запросов(self, monkeypatch):
        s = _Search({None: []})
        monkeypatch.setattr(db, "chunks_search", s)
        mi.related_block("alice", "", [f"тема{i} уникальная" for i in range(20)])
        assert len(s.calls) == mi._RELATED_MAX_QUERIES


# --------------------------------------------------------------------------- #
# Ключевые слова без модели
# --------------------------------------------------------------------------- #
TRANSCRIPT = "\n".join([
    "[00:00:10] Зоя Р: Коллеги, давайте по бюджету сайта. Мы решили, что редизайн делает Кирилл.",
    "[00:00:40] Кирилл Бубнов: Да, в Weeek задача есть, интеграция с CRM через API, это важно.",
    "[00:01:10] Зоя Р: Бюджет 500 тысяч на редизайн, Кирилл сделает макет. Это встреча про сайт.",
    "[00:01:40] Иван: Что по DeepSeek? Переформулировка не ловится, нужен пересчёт эмбеддингов.",
    "",
    "=== ТЕКСТ С ЭКРАНА ===",
    "Kubernetes Kubernetes Kubernetes Kubernetes",
])


class TestSalientQueries:
    def test_латиница_имена_числа_и_длинные_слова(self):
        q = mi.salient_queries(TRANSCRIPT, limit=12)
        assert {"Weeek", "CRM", "API", "DeepSeek"} <= set(q)
        assert "500 тысяч" in q
        assert "Кирилл" in q                       # заглавная не в начале предложения
        assert "переформулировка" in q             # редкое длинное слово
        for bad in ("что", "это", "встреча", "давайте", "решили", "Коллеги", "Зоя"):
            assert bad not in q and bad.lower() not in q
        assert "Kubernetes" not in q               # служебный блок — не речь

    def test_лимит_и_частота(self):
        q = mi.salient_queries(TRANSCRIPT, limit=3)
        assert len(q) == 3
        assert "Кирилл" in q                       # встречается чаще других

    def test_пусто(self):
        assert mi.salient_queries("") == []
        assert mi.salient_queries("[00:01] да. нет. ну.") == []


# --------------------------------------------------------------------------- #
# Хук в analyze_transcript — с фейковым движком, как в test_analyze_pipeline
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _mechanics_only(monkeypatch):
    monkeypatch.setattr(analyze, "MIN_SPEECH_WORDS", 0)
    monkeypatch.setattr(analyze, "_MAX_TOPIC_REGENS", 0)


class FakeBackend:
    name = "fake"

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def complete(self, prompt, max_tokens=2000, force_json=True, **kw):
        self.prompts.append(prompt)
        return self.answers.pop(0)


def _protocol_answer():
    return json.dumps({"participants": [], "summary": "s",
                       "detailed": [{"topic": "Тема 1", "details": "d1"}],
                       "key_thoughts": [], "conclusions": [], "decisions": [],
                       "done_tasks": [], "tasks": [], "minor_tasks": []},
                      ensure_ascii=False)


def _map_answer(i):
    return json.dumps(MapNotes(
        time_range=f"{i*10:02d}:00–{i*10+10:02d}:00",
        topics=[TopicNote(t=f"{i*10:02d}:01", topic=f"Тема {i} про бюджет", details="…" * 10)],
        decisions=[DecisionNote(text=f"Решение {i}")],
        tasks=[TaskNote(task=f"Задача {i}")],
    ).model_dump(), ensure_ascii=False)


def _two_chunk_text(monkeypatch):
    monkeypatch.setattr(analyze, "_MAX_CHARS", 50)
    monkeypatch.setattr(analyze, "_CHUNK_CHARS", 120)
    monkeypatch.setattr(analyze, "_CHUNK_OVERLAP", 0)
    return ("[00:00] " + "обсуждение первой темы " * 4 + "\n"
            + "[10:00] " + "обсуждение второй темы " * 4)


RELATED = (mi.RELATED_HEADER + " (справка) ===\n"
           "• Встреча лидеров, 01.09.2026: Бюджет сайта 500 тысяч утвердили.")
CONTEXT = "=== ПОСТОЯННЫЙ КОНТЕКСТ ===\nпроект Ромашка"


class TestRetrieveHook:
    def test_короткая_встреча_хук_с_None_и_блок_после_контекста_перед_текстом(self, monkeypatch):
        backend = FakeBackend([_protocol_answer()])
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: backend)
        got = []

        def retrieve(queries):
            got.append(queries)
            return RELATED
        analyze.analyze_transcript("[00:01] короткая встреча", context=CONTEXT,
                                   retrieve=retrieve)
        assert got == [None]
        p = backend.prompts[0]
        assert p.index(CONTEXT) < p.index(mi.RELATED_HEADER) < p.index("Транскрипция:")
        assert "Бюджет сайта 500 тысяч утвердили" in p

    def test_длинная_встреча_хук_с_темами_заметок_только_в_сведении(self, monkeypatch):
        text = _two_chunk_text(monkeypatch)
        backend = FakeBackend([_map_answer(1), _map_answer(2), _protocol_answer()])
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: backend)
        got = []

        def retrieve(queries):
            got.append(queries)
            return RELATED
        analyze.analyze_transcript(text, context=CONTEXT, retrieve=retrieve)
        assert len(got) == 1 and got[0] is not None
        assert got[0] == ["Тема 1 про бюджет", "Решение 1", "Задача 1",
                          "Тема 2 про бюджет", "Решение 2", "Задача 2"]
        assert len(backend.prompts) == 3
        for p in backend.prompts[:2]:                 # карта — без блока
            assert mi.RELATED_HEADER not in p and CONTEXT in p
        reduce_prompt = backend.prompts[-1]
        assert (reduce_prompt.index(CONTEXT) < reduce_prompt.index(mi.RELATED_HEADER)
                < reduce_prompt.index("Заметки по частям:"))

    def test_хук_с_исключением_не_ломает_протокол(self, monkeypatch, caplog):
        backend = FakeBackend([_protocol_answer()])
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: backend)

        def boom(queries):
            raise RuntimeError("база недоступна")
        with caplog.at_level("WARNING", logger="vtx.analyze"):
            res = analyze.analyze_transcript("[00:01] короткая встреча", retrieve=boom)
        assert res["summary"] == "s"
        assert mi.RELATED_HEADER not in backend.prompts[0]
        assert "Похожие обсуждения не подобраны" in caplog.text

    def test_без_хука_и_с_пустым_ответом_промпт_прежний(self, monkeypatch):
        a = FakeBackend([_protocol_answer()])
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a_, **k: a)
        analyze.analyze_transcript("[00:01] короткая встреча", context=CONTEXT)
        b = FakeBackend([_protocol_answer()])
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a_, **k: b)
        analyze.analyze_transcript("[00:01] короткая встреча", context=CONTEXT,
                                   retrieve=lambda q: "")
        assert a.prompts == b.prompts

    def test_экспертный_промпт_тоже_получает_блок(self, monkeypatch):
        backend = FakeBackend([_protocol_answer()])
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: backend)
        analyze.analyze_transcript("[00:01] короткая встреча", custom_prompt="Собери протокол",
                                   retrieve=lambda q: RELATED)
        p = backend.prompts[0]
        assert p.index("Собери протокол") < p.index(mi.RELATED_HEADER) < p.index("Транскрипция:")


class TestNotesQueries:
    def test_порядок_дедуп_и_лимит(self):
        maps = [{"topics": [{"topic": "  Бюджет   сайта "}, {"topic": "бюджет сайта"}],
                 "decisions": [{"text": "x" * 300}], "tasks": [{"task": ""}, {"task": "Сделать"}]}]
        q = analyze._notes_queries(maps, limit=3)
        assert q == ["Бюджет сайта", "x" * 120, "Сделать"]


# --------------------------------------------------------------------------- #
# Блок — не речь и не источник цитат
# --------------------------------------------------------------------------- #
class TestInjectedBlock:
    def test_regex_вырезает_блок(self):
        text = "[00:10] Обсудили сроки.\n\n" + RELATED + "\n\n=== ТЕКСТ С ЭКРАНА ===\nэкран"
        clean = analyze._INJECTED_BLOCK_RE.sub(" ", text)
        assert "Обсудили сроки" in clean
        assert "500 тысяч утвердили" not in clean and "Встреча лидеров" not in clean

    def test_ensure_analysable_не_считает_блок_речью(self, monkeypatch):
        monkeypatch.setattr(analyze, "MIN_SPEECH_WORDS", 5)
        long_block = mi.RELATED_HEADER + " ===\n" + "• Встреча, 01.09.2026: " + "слово " * 50
        assert analyze.speech_words(long_block) == 0
        with pytest.raises(analyze.NoTranscript):
            analyze.ensure_analysable("[00:01] привет\n\n" + long_block)

    def test_check_topics_не_опирается_на_блок(self):
        speech = "\n".join(f"[00:{i:02d}:00] говорим про уникальноеслово{i} подробно"
                           for i in range(40))
        block = (mi.RELATED_HEADER + " ===\n• Встреча лидеров, 01.09.2026: "
                 "Kubernetes Grafana Prometheus Terraform Ansible Jenkins Gitlab")
        res = {"detailed": [{"topic": "Инфраструктура",
                             "details": "Kubernetes Grafana Prometheus Terraform Ansible Jenkins Gitlab"}]}
        out = analyze.check_topics(res, speech + "\n\n" + block)
        assert out["detailed"] == [] and out["_dropped_topics"] == ["Инфраструктура"]


# --------------------------------------------------------------------------- #
# Привязка в jobs: хук строится из памяти встреч
# --------------------------------------------------------------------------- #
class TestJobsRetriever:
    def _job(self):
        from app.jobs import STATUS_DONE, Job
        return Job(id="job-cur", filename="28.09.2026, 09-00. - Гранты - внутренняя.mp4",
                   audio_path="", language="ru", diarize=False, owner="alice",
                   analyze=True, status=STATUS_DONE, created_at=time.time())

    def test_без_памяти_встреч_хука_нет(self, monkeypatch):
        from app import jobs
        monkeypatch.setattr(mi, "enabled", lambda: False)
        assert jobs._related_retriever(self._job(), "текст") is None

    def test_хук_зовёт_ретривер_с_командой_серией_и_документом(self, monkeypatch):
        from app import jobs
        monkeypatch.setattr(mi, "enabled", lambda: True)
        calls = []

        def related(team, key, queries, exclude_doc=None, budget_chars=2500):
            calls.append((team, key, list(queries), exclude_doc))
            return RELATED
        monkeypatch.setattr(mi, "related_block", related)
        monkeypatch.setattr(mi, "salient_queries", lambda txt, limit=6: ["из расшифровки: " + txt])
        hook = jobs._related_retriever(self._job(), "речь встречи")
        assert hook(["тема карты"]) == RELATED
        assert hook(None) == RELATED
        assert calls == [("alice", "гранты внутренняя", ["тема карты"], "job-cur"),
                         ("alice", "гранты внутренняя", ["из расшифровки: речь встречи"], "job-cur")]

    def test_protocol_pass_передаёт_хук(self, monkeypatch):
        from app import jobs
        monkeypatch.setattr(mi, "enabled", lambda: True)
        seen = {}

        def fake(txt, **kw):
            seen["retrieve"] = kw.get("retrieve")
            return {"summary": "s", "tasks": []}
        monkeypatch.setattr(analyze, "analyze_transcript", fake)
        job = self._job()
        job.analysis_prompt = "эксперт"      # проверка цитат пропускается
        jobs.store._jobs[job.id] = job
        try:
            jobs.store._protocol_pass(job, "текст", context="")
        finally:
            jobs.store._jobs.pop(job.id, None)
        assert callable(seen["retrieve"])
