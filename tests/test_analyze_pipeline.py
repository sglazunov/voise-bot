"""Д1+Д2: structured map-reduce with timecodes, overlap dedup, chronology and
context-budget control. All LLM calls are faked — these tests pin the MECHANICS:
chunk dedup (±2 мин + difflib ≥ 0.85), chronological reduce input, hierarchical
merge triggering, schema-validation retry and graceful degradation.
"""
import json

import pytest

from app import analyze, llm
from app.analyze import (MapNotes, Protocol, TaskNote, TopicNote, _ctx_budget,
                         _dedup_maps, _mech_merge, _notes_blob, _parse_ts)


@pytest.fixture(autouse=True)
def _no_speech_gate(monkeypatch):
    """Здесь проверяется МЕХАНИКА конвейера, а не защита от пустой записи:
    транскрипты нарочно крошечные («[00:01] короткая встреча»), чтобы арифметика
    чанков читалась глазами. Гейт минимальной речи для них отключён — он покрыт
    отдельно в tests/test_no_transcript.py."""
    monkeypatch.setattr(analyze, "MIN_SPEECH_WORDS", 0)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
class TestParseTs:
    def test_mm_ss(self):
        assert _parse_ts("05:30") == 330

    def test_hh_mm_ss(self):
        assert _parse_ts("1:02:03") == 3723

    def test_garbage_and_empty(self):
        assert _parse_ts(None) is None
        assert _parse_ts("") is None
        assert _parse_ts("вчера") is None


class TestDedup:
    def _maps(self, t1, t2, txt1, txt2):
        return [
            {"tasks": [{"t": t1, "task": txt1, "owner": None,
                        "owner_evidence": None, "done": False}], "decisions": []},
            {"tasks": [{"t": t2, "task": txt2, "owner": "Кирилл",
                        "owner_evidence": "я возьму", "done": False}], "decisions": []},
        ]

    def test_overlap_duplicate_dropped_and_owner_enriched(self):
        # Same task re-extracted from the overlap zone, 40 s apart, slightly
        # different wording -> one survives, and it LEARNS the owner from the dup.
        maps = _dedup_maps(self._maps(
            "10:00", "10:40",
            "Переименовать теги карточек товара",
            "Переименовать теги карточек товаров"))
        assert len(maps[0]["tasks"]) == 1
        assert maps[1]["tasks"] == []
        assert maps[0]["tasks"][0]["owner"] == "Кирилл"
        assert maps[0]["tasks"][0]["owner_evidence"] == "я возьму"

    def test_far_apart_similar_tasks_are_kept(self):
        # Similar text but 10 minutes apart — different discussions, keep both.
        maps = _dedup_maps(self._maps(
            "10:00", "20:00",
            "Обновить документацию по API",
            "Обновить документацию по API"))
        assert len(maps[0]["tasks"]) == 1 and len(maps[1]["tasks"]) == 1

    def test_different_tasks_close_in_time_are_kept(self):
        maps = _dedup_maps(self._maps(
            "10:00", "10:30",
            "Переименовать теги карточек",
            "Выкатить релиз на прод в пятницу"))
        assert len(maps[0]["tasks"]) == 1 and len(maps[1]["tasks"]) == 1

    def test_decisions_deduped_too(self):
        maps = [
            {"tasks": [], "decisions": [{"t": "05:00", "text": "Оставить базовые фильтры"}]},
            {"tasks": [], "decisions": [{"t": "05:30", "text": "Оставить базовые фильтры."}]},
        ]
        out = _dedup_maps(maps)
        assert len(out[0]["decisions"]) == 1 and out[1]["decisions"] == []


class TestMechMerge:
    def test_lossless_concat(self):
        a = {"time_range": "00:00–10:00", "participants": ["Аня"],
             "topics": [{"topic": "т1"}], "decisions": [], "tasks": [{"task": "x"}]}
        b = {"time_range": "10:00–20:00", "participants": ["Аня", "Борис"],
             "topics": [{"topic": "т2"}], "decisions": [{"text": "д"}], "tasks": []}
        m = _mech_merge(a, b)
        assert m["time_range"] == "00:00–20:00"
        assert m["participants"] == ["Аня", "Борис"]
        assert [t["topic"] for t in m["topics"]] == ["т1", "т2"]
        assert len(m["tasks"]) == 1 and len(m["decisions"]) == 1


class TestBudget:
    def test_groq_budget_is_tpm(self):
        class B: name = "groq:llama-3.3-70b"
        assert _ctx_budget(B()) == 12000

    def test_ollama_budget_is_num_ctx(self):
        class B: name = "ollama:qwen2.5:7b"
        assert _ctx_budget(B()) == llm.OLLAMA_NUM_CTX

    def test_cloud_budget_is_large(self):
        class B: name = "gemini"
        assert _ctx_budget(B()) >= 100_000


# --------------------------------------------------------------------------- #
# The pipeline with a fake backend
# --------------------------------------------------------------------------- #
def _map_answer(i, tasks=None):
    return json.dumps(MapNotes(
        time_range=f"{i*10:02d}:00–{i*10+10:02d}:00",
        participants=[f"Спикер {i}"],
        topics=[TopicNote(t=f"{i*10:02d}:01", topic=f"Тема {i}", details="…" * 10)],
        tasks=tasks or [],
    ).model_dump(), ensure_ascii=False)


class FakeBackend:
    """Scripted provider: returns queued answers, records every prompt."""
    name = "fake"

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def complete(self, prompt, max_tokens=2000, force_json=True, **kw):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError("больше ответов не заготовлено")
        return self.answers.pop(0)


def _protocol_answer(**over):
    p = {"participants": [{"name": "Аня", "role": ""}], "summary": "s",
         "detailed": [{"topic": "Тема 1", "details": "d1"},
                      {"topic": "Тема 2", "details": "d2"}],
         "key_thoughts": [], "conclusions": [], "decisions": [],
         "done_tasks": [], "tasks": [{"task": "x", "owner": "—"}],
         "minor_tasks": []}
    p.update(over)
    return json.dumps(p, ensure_ascii=False)


def _two_chunk_text(monkeypatch):
    """Deterministic 2-chunk split: two ~100-char lines, chunk=120, overlap off."""
    monkeypatch.setattr(analyze, "_MAX_CHARS", 50)
    monkeypatch.setattr(analyze, "_CHUNK_CHARS", 120)
    monkeypatch.setattr(analyze, "_CHUNK_OVERLAP", 0)
    return ("[00:00] " + "обсуждение первой темы " * 4 + "\n"
            + "[10:00] " + "обсуждение второй темы " * 4)


class TestPipeline:
    def test_map_reduce_chronological_notes(self, monkeypatch):
        text = _two_chunk_text(monkeypatch)
        backend = FakeBackend([_map_answer(1), _map_answer(2), _protocol_answer()])
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: backend)

        res = analyze.analyze_transcript(text)
        assert res["_provider"] == "fake"
        assert [d["topic"] for d in res["detailed"]] == ["Тема 1", "Тема 2"]
        # The reduce prompt carries the parts in chronological order with ranges.
        reduce_prompt = backend.prompts[-1]
        assert reduce_prompt.index("Часть 1") < reduce_prompt.index("Часть 2")
        assert "10:00–20:00" in reduce_prompt or "20:00–30:00" in reduce_prompt

    def test_schema_retry_then_degrade_keeps_chunk(self, monkeypatch):
        # Chunk 1: both map answers invalid -> the chunk survives as a flat topic.
        text = _two_chunk_text(monkeypatch)
        backend = FakeBackend([
            "это вообще не json", "и это тоже не json",   # map 1 + retry
            _map_answer(2),                                # map 2 ok
            _protocol_answer(),
        ])
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: backend)
        res = analyze.analyze_transcript(text)
        reduce_prompt = backend.prompts[-1]
        assert "Фрагмент 1" in reduce_prompt          # degraded chunk kept
        assert "не json" in reduce_prompt             # its raw content preserved
        assert res["detailed"]

    def test_hierarchical_merge_triggers_on_small_budget(self, monkeypatch):
        text = _two_chunk_text(monkeypatch)
        # Budget so small that two note-sets must be merged pairwise first.
        monkeypatch.setattr(analyze, "_ctx_budget", lambda b: 30)
        merged = _map_answer(9)
        backend = FakeBackend([_map_answer(1), _map_answer(2),
                               merged,                # pairwise merge call
                               _protocol_answer()])
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: backend)
        analyze.analyze_transcript(text)
        assert any("Отрезок A" in p for p in backend.prompts)   # merge happened
        assert "Тема 9" in backend.prompts[-1]                  # reduce got merged notes

    def test_single_pass_uses_protocol_schema_validation(self, monkeypatch):
        backend = FakeBackend(["мусор без json", _protocol_answer(summary="ок")])
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: backend)
        res = analyze.analyze_transcript("[00:01] короткая встреча")
        assert res["summary"] == "ок"                 # retry fixed it
        assert "не прошёл проверку схемы" in backend.prompts[1]

    def test_owner_null_does_not_trigger_retry_and_normalises(self, monkeypatch):
        backend = FakeBackend([_protocol_answer(
            tasks=[{"task": "Настроить теги", "owner": None}])])
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: backend)
        res = analyze.analyze_transcript("[00:01] короткая встреча")
        assert len(backend.prompts) == 1              # no needless retry on null
        assert res["tasks"][0]["task"] == "Настроить теги"
        assert res["tasks"][0]["owner"] == ""         # null -> unassigned, not a guess


class TestCloudPreference:
    """Долгая встреча не должна молоть часами на CPU-Ollama, когда есть облако."""

    def test_chain_fallback_order_puts_ollama_last(self, monkeypatch):
        from app import config, llm
        monkeypatch.setattr(config, "available_providers",
                            lambda keys=None: ["ollama", "groq", "gemini"])
        made = []
        real_get = llm.get_provider
        def fake_get(name, keys=None):
            made.append(name)
            class B: pass
            b = B(); b.name = name or "?"; b.complete = lambda *a, **k: ""
            return b
        monkeypatch.setattr(llm, "get_provider", fake_get)
        chain = llm.get_provider_chain("groq", None)
        # порядок: primary groq -> облако gemini -> ollama ПОСЛЕДНИМ
        assert made == ["groq", "gemini", "ollama"]

    def test_prefer_cloud_moves_cursor_past_ollama(self):
        from app import llm
        class Cloud: name = "groq"
        oll = llm.OllamaProvider(model="x")
        chain = llm._FallbackChain([oll, Cloud()])
        assert chain.name.startswith("ollama")
        assert chain.prefer_cloud() is True
        assert chain.name == "groq"

    def test_prefer_cloud_noop_when_only_ollama(self):
        from app import llm
        chain = llm._FallbackChain([llm.OllamaProvider(model="x"),
                                    llm.OllamaProvider(model="y")])
        assert chain.prefer_cloud() is False

    def test_long_auto_meeting_prefers_cloud(self, monkeypatch):
        calls = {"prefer": 0}
        class FakeChain:
            name = "fake"
            def prefer_cloud(self):
                calls["prefer"] += 1
                return True
            def complete(self, prompt, max_tokens=2000, force_json=True, **kw):
                return _protocol_answer()
        monkeypatch.setattr(analyze, "_MAX_CHARS", 50)
        monkeypatch.setattr(analyze, "_CHUNK_CHARS", 10_000)  # 1 chunk
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: FakeChain())
        analyze.analyze_transcript("[00:01] " + "долгая встреча " * 20)
        assert calls["prefer"] >= 1

    def test_short_meeting_stays_local(self, monkeypatch):
        calls = {"prefer": 0}
        class FakeChain:
            name = "fake"
            def prefer_cloud(self):
                calls["prefer"] += 1
                return True
            def complete(self, prompt, max_tokens=2000, force_json=True, **kw):
                return _protocol_answer()
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: FakeChain())
        analyze.analyze_transcript("[00:01] короткая встреча")
        assert calls["prefer"] == 0
