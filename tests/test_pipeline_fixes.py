"""Регрессии по ревью конвейера 01.09.2026: оборванный JSON, ложные дубли,
стемминг цитат, мягкая дословность, сбой проверки не красит весь протокол,
контекст перед текстом в каждом запросе."""
import json

import pytest

from app import analyze
from app.analyze import (TruncatedAnswer, _Fragment, _dedup_maps, _extract_json,
                         _parse_for, _quote_relevant, _stems, MapNotes, Protocol,
                         merge_similar_topics, verify_protocol)
from app.analyze_schemas import norm_owner


class TestTruncated:
    TRUNC = ('{"participants": [{"name": "Аня", "role": "дизайнер"}], '
             '"summary": "о встрече", "detailed": [{"topic": "Т", "details": "d"}], '
             '"decisions": ["реш')

    def test_truncated_protocol_is_an_error_not_a_nested_object(self):
        with pytest.raises(TruncatedAnswer):
            _extract_json(self.TRUNC, expected_keys=Protocol.model_fields.keys())
        with pytest.raises(TruncatedAnswer):
            _parse_for(Protocol, self.TRUNC)

    def test_truncated_evidence_and_notes(self):
        with pytest.raises(TruncatedAnswer):
            _parse_for(analyze.EvidenceList, '{"items": [{"i": 1, "quote": "abc')
        with pytest.raises(TruncatedAnswer):
            _parse_for(MapNotes, '{"time_range": "0-1", "topics": [{"t": "00:01", "topic": "x"')

    def test_object_after_prose_with_expected_keys_still_accepted(self):
        raw = 'Вот ответ {"summary": "итог", "tasks": []}'
        assert _parse_for(Protocol, raw)["summary"] == "итог"

    def test_unrelated_dict_rejected(self):
        with pytest.raises(ValueError):
            _parse_for(Protocol, '{"foo": 1, "bar": 2}')

    def test_lenient_path_refuses_empty_protocol(self):
        with pytest.raises(RuntimeError):
            analyze._lenient_protocol('{"name": "Аня"}')
        assert analyze._lenient_protocol('{"summary": "s"}')["_schema_miss"] is True


class TestDedup:
    def _maps(self, t1, t2, a, b, o1=None, o2=None):
        return [{"tasks": [{"t": t1, "task": a, "owner": o1, "owner_evidence": None,
                            "done": False}], "decisions": []},
                {"tasks": [{"t": t2, "task": b, "owner": o2, "owner_evidence": None,
                            "done": False}], "decisions": []}]

    def test_substituted_object_is_not_a_duplicate(self):
        maps = _dedup_maps(self._maps("10:00", "10:20",
                                      "Поменять цвет кнопки на синий",
                                      "Поменять цвет кнопки на красный"))
        assert len(maps[0]["tasks"]) == 1 and len(maps[1]["tasks"]) == 1

    def test_different_addressee_is_not_a_duplicate(self):
        maps = _dedup_maps(self._maps("10:00", "10:20",
                                      "Выдать доступ к Figma Ивану",
                                      "Выдать доступ к Figma Марии"))
        assert len(maps[1]["tasks"]) == 1

    def test_refinement_still_merges(self):
        maps = _dedup_maps(self._maps("10:00", "10:20",
                                      "Переименовать теги карточек",
                                      "Переименовать теги карточек товара"))
        assert maps[1]["tasks"] == []

    def test_no_timecode_needs_near_identical_text(self):
        maps = _dedup_maps(self._maps(None, "40:00",
                                      "Обновить документацию по API",
                                      "Обновить документацию по API v2"))
        assert len(maps[1]["tasks"]) == 1
        maps = _dedup_maps(self._maps(None, None,
                                      "Обновить документацию по API",
                                      "Обновить документацию по API"))
        assert maps[1]["tasks"] == []

    def test_two_owners_are_two_tasks(self):
        maps = _dedup_maps(self._maps("10:00", "10:10", "Подготовить макет",
                                      "Подготовить макет", "Алсу", "Мухаммад"))
        assert len(maps[1]["tasks"]) == 1


class TestStems:
    @pytest.mark.parametrize("point,quote", [
        ("Настроить теги карточек", "тегов не хватает, настроим"),
        ("Согласовать сроки релиза", "срок — до пятницы"),
        ("Обновить цвет кнопки", "цвета поменяем"),
        ("Сайт ОД", "на сайте ОД"),
    ])
    def test_short_words_inflect(self, point, quote):
        assert _stems(point) & _stems(quote), (_stems(point), _stems(quote))
        assert _quote_relevant(point, quote, quote)

    def test_anaphora_on_separate_line_uses_neighbouring_lines(self):
        frag = ("[01:20] Сергей:\nКирилл, сделай ревизию фильтров до пятницы.\n"
                "[02:00] Кирилл:\nя возьму это на себя")
        assert _quote_relevant("Ревизия фильтров", "я возьму это на себя", frag)


class TestFragmentMatch:
    FRAG = ("[01:20] Сергей: Кирилл, сделай ревизию фильтров до пятницы.\n\n"
            "[02:00] Кирилл:\nсделаю, беру на себя эту ревизию.")

    def test_verbatim(self):
        assert _Fragment(self.FRAG).match("сделай ревизию фильтров до пятницы") == "verbatim"

    def test_cross_turn_quote_ignores_speaker_label(self):
        assert _Fragment(self.FRAG).match(
            "ревизию фильтров до пятницы сделаю, беру на себя") == "verbatim"

    def test_model_fixed_a_recognition_typo(self):
        frag = _Fragment("[00:10] Надо Сдолать ревизию фильтров карточек до пятницы вечером")
        assert frag.match("Надо сделать ревизию фильтров карточек до пятницы вечером") == "approx"

    def test_paraphrase_is_not_a_match(self):
        assert _Fragment(self.FRAG).match("Кириллу поручили провести ревизию фильтров") is None


class FakeBackend:
    name = "fake"

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def complete(self, prompt, max_tokens=2000, force_json=True, **kw):
        self.prompts.append(prompt)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def _proto(n_tasks=3):
    return {"participants": [], "summary": "s", "detailed": [], "key_thoughts": [],
            "conclusions": [], "decisions": [], "done_tasks": [], "minor_tasks": [],
            "tasks": [{"task": f"Задача номер {i}", "owner": "Кирилл"} for i in range(n_tasks)]}


class TestVerifyFailure:
    def test_engine_failure_leaves_points_unchecked_not_refuted(self, monkeypatch):
        backend = FakeBackend([RuntimeError("503")])
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: backend)
        res = verify_protocol(_proto(), "[00:01] задача номер 0 — Кирилл, сделай")
        ver = res["verification"]
        assert ver["error"] and ver["mode"] == "partial"
        assert ver["tasks"] == [None, None, None]
        # ответственные не сняты — проверка ничего не сказала
        assert all(t["owner"] == "Кирилл" for t in res["tasks"])

    def test_docx_does_not_flag_unchecked_points(self, monkeypatch, tmp_path):
        backend = FakeBackend([RuntimeError("503")])
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: backend)
        res = verify_protocol(_proto(), "[00:01] задача номер 0")
        pytest.importorskip("docx")
        from docx import Document
        from app.docx_export import generate_report
        out = tmp_path / "p.docx"
        generate_report(out_path=out, filename="x.mp4", segments=[], analysis=res)
        text = "\n".join(p.text for p in Document(str(out)).paragraphs)
        assert "не нашлось дословного подтверждения" not in text
        assert "Требуют проверки" not in text
        assert "выполнена не полностью" in text

    def test_points_are_verified_in_batches(self, monkeypatch):
        answers = [json.dumps({"items": []}) for _ in range(3)]
        backend = FakeBackend(answers)
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: backend)
        res = verify_protocol(_proto(40), "[00:01] речь речь речь")
        assert res["verification"]["stats"]["calls"] == 3
        assert all(p.count(") [tasks]") <= 15 for p in backend.prompts)


class TestTopicsAndOwners:
    def test_short_generic_titles_stay_separate(self):
        out = merge_similar_topics([{"topic": "Сроки", "details": "a"},
                                    {"topic": "Сроки релиза", "details": "b"},
                                    {"topic": "Сроки оплаты подрядчику", "details": "c"}])
        assert len(out) == 3

    def test_norm_owner_single_rule(self):
        for bad in ("null", "None", "TBD", "не указан", "—", "-", "?", ""):
            assert norm_owner(bad) == ""
        assert norm_owner(" Кирилл ") == "Кирилл"


class TestContextPlacement:
    """Контекст идёт ПЕРЕД текстом в каждом запросе, а не хвостом последнего
    фрагмента; в чанках он не режется как речь."""

    def test_context_prefix_in_every_map_call(self, monkeypatch):
        monkeypatch.setattr(analyze, "MIN_SPEECH_WORDS", 0)
        monkeypatch.setattr(analyze, "_MAX_TOPIC_REGENS", 0)
        monkeypatch.setattr(analyze, "_MAX_CHARS", 200)
        monkeypatch.setattr(analyze, "_CHUNK_CHARS", 150)
        notes = json.dumps({"time_range": "00:00–00:01", "participants": [],
                            "topics": [], "decisions": [], "tasks": []})
        proto = json.dumps(_proto(0))

        class Responder(FakeBackend):
            def complete(self, prompt, max_tokens=2000, force_json=True, **kw):
                self.prompts.append(prompt)
                return proto if "Заметки по частям" in prompt else notes

        backend = Responder([])
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: backend)
        text = "\n".join(f"[00:{i:02d}] реплика номер {i} про редизайн сайта" for i in range(30))
        ctx = ("=== КОНТЕКСТ СЕРИИ ВСТРЕЧ «ОД» ===\nЗоя Р — руководитель.\n\n"
               "=== ПРОШЛАЯ ВСТРЕЧА ЭТОЙ СЕРИИ (24.08.2026) ===\nРешили тогда:\n- запуск 15.09")
        analyze.analyze_transcript(text, context=ctx)
        maps = [p for p in backend.prompts if "Фрагмент:" in p]
        assert len(maps) >= 2
        for p in maps:
            assert p.index("Зоя Р — руководитель") < p.index("Фрагмент:")
            assert "ПРОШЛАЯ ВСТРЕЧА" not in p          # память — только в сведении
        reduce = backend.prompts[-1]
        assert "ПРОШЛАЯ ВСТРЕЧА" in reduce and reduce.index("Зоя Р") < reduce.index("Заметки по частям")
