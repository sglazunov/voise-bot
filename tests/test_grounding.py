"""Д5+Д6: grounding-проход (дословные цитаты как основание пунктов протокола)
и заметки участника как приоритетный вход. LLM фейковый; тесты фиксируют
МЕХАНИКУ: перефраз не считается доказательством, owner без основания снимается,
заметки проверяются раньше расшифровки и попадают в промпт.
"""
import json

from app import analyze
from app.analyze import _norm_for_match, verify_protocol


class FakeBackend:
    name = "fake"

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def complete(self, prompt, max_tokens=2000, force_json=True, **kw):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError("больше ответов не заготовлено")
        return self.answers.pop(0)


def _result():
    return {
        "participants": [], "summary": "s", "detailed": [],
        "key_thoughts": [], "conclusions": [],
        "decisions": ["Оставляем базовые фильтры"],
        "done_tasks": [],
        "tasks": [{"task": "Ревизия фильтров", "owner": "Кирилл"},
                  {"task": "Выдуманная задача", "owner": "Мария"}],
        "minor_tasks": [],
    }


TRANSCRIPT = ("[01:20] Сергей: Кирилл, сделай ревизию фильтров до пятницы.\n"
              "[02:00] Кирилл: принял, беру на себя.\n"
              "[03:00] Мария: решили — оставляем базовые фильтры.")


def _ev(items):
    return json.dumps({"items": items}, ensure_ascii=False)


class TestQuoteRelevance:
    def test_real_quote_for_invented_task_rejected(self, monkeypatch):
        # The model attaches a REAL verbatim quote to a task the meeting never
        # discussed — verbatimness passes, relevance must fail it.
        backend = FakeBackend([_ev([
            {"i": 2, "quote": "Решили — оставляем базовые фильтры", "owner_ok": True},
        ])])
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: backend)
        res = verify_protocol(_result(), TRANSCRIPT)
        assert not res["verification"]["tasks"][1]["ok"]
        assert res["tasks"][1]["owner"] == ""

    def test_inflection_still_matches(self):
        # «ревизию фильтров» подтверждает задачу «Ревизия фильтров» (падежи).
        assert analyze._quote_relevant(
            "Ревизия фильтров", "Кирилл, сделай ревизию фильтров до пятницы", TRANSCRIPT)

    def test_anaphoric_quote_matches_via_line_context(self):
        frag = "[02:00] Кирилл: по ревизии фильтров — принял, беру на себя."
        assert analyze._quote_relevant("Ревизия фильтров", "принял, беру на себя", frag)


class TestNormForMatch:
    def test_case_punct_spaces_yo(self):
        assert _norm_for_match("Кирилл, сделай ревизию!") in _norm_for_match(
            "[01:20] Сергей:  кирилл сделай ревизию фильтров")

    def test_paraphrase_not_substring(self):
        assert _norm_for_match("поручили ревизию Кириллу") not in _norm_for_match(TRANSCRIPT)


class TestVerifyProtocol:
    def test_verbatim_quote_confirms_and_paraphrase_rejected(self, monkeypatch):
        # Point order = tasks (1, 2) then decisions (3).
        backend = FakeBackend([_ev([
            {"i": 1, "quote": "Кирилл, сделай ревизию фильтров до пятницы", "t": "01:20", "owner_ok": True},
            # пункт 2 «подтверждён» ПЕРЕФРАЗОМ — механика обязана отклонить
            {"i": 2, "quote": "Марии поручили выдуманную задачу", "t": "02:30", "owner_ok": True},
            {"i": 3, "quote": "оставляем базовые фильтры", "t": "03:00", "owner_ok": False},
        ])])
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: backend)
        res = verify_protocol(_result(), TRANSCRIPT)
        v = res["verification"]
        assert v["decisions"][0]["ok"] and v["decisions"][0]["t"] == "03:00"
        assert v["tasks"][0]["ok"] and v["tasks"][0]["owner_ok"]
        assert res["tasks"][0]["owner"] == "Кирилл"          # подтверждён — остался
        assert not v["tasks"][1]["ok"]                       # перефраз отклонён
        assert res["tasks"][1]["owner"] == ""                # owner без основания снят

    def test_verified_point_without_owner_ok_strips_owner(self, monkeypatch):
        backend = FakeBackend([_ev([
            {"i": 1, "quote": "сделай ревизию фильтров до пятницы", "owner_ok": False},
            {"i": 2, "quote": "оставляем базовые фильтры", "owner_ok": False},
            {"i": 3, "quote": "оставляем базовые фильтры", "owner_ok": False},
        ])])
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: backend)
        res = verify_protocol(_result(), TRANSCRIPT)
        assert res["verification"]["tasks"][0]["ok"]
        assert res["tasks"][0]["owner"] == ""    # пункт подтверждён, владелец — нет

    def test_notes_checked_first_and_marked_as_source(self, monkeypatch):
        notes = "Договорились: ревизия фильтров на Кирилле."
        backend = FakeBackend([
            _ev([{"i": 1, "quote": "ревизия фильтров на Кирилле", "owner_ok": True}]),  # notes pass
            _ev([{"i": 3, "quote": "оставляем базовые фильтры", "owner_ok": False}]),   # transcript pass
        ])
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: backend)
        res = verify_protocol(_result(), TRANSCRIPT, user_notes=notes)
        assert res["verification"]["tasks"][0]["source"] == "notes"
        assert res["verification"]["decisions"][0]["source"] == "transcript"
        assert notes in backend.prompts[0]      # the notes ARE the first fragment

    def test_engine_failure_ships_unverified_with_error(self, monkeypatch):
        class Dead:
            name = "fake"
            def complete(self, *a, **k): raise RuntimeError("503")
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: Dead())
        res = verify_protocol(_result(), TRANSCRIPT)
        assert "error" in res["verification"]
        assert res["tasks"][1]["owner"] == ""    # строгое правило действует и тут

    def test_empty_protocol_short_circuits(self, monkeypatch):
        called = []
        monkeypatch.setattr(analyze.llm, "get_provider_chain",
                            lambda *a, **k: called.append(1))
        res = verify_protocol({"tasks": [], "decisions": [], "done_tasks": [],
                               "minor_tasks": []}, TRANSCRIPT)
        assert res["verification"]["mode"] == "strict"
        assert not called    # ни одного LLM-вызова


class TestNotesInPrompt:
    def test_notes_block_reaches_single_pass_prompt(self, monkeypatch):
        backend = FakeBackend([json.dumps({
            "participants": [], "summary": "ок", "detailed": [], "key_thoughts": [],
            "conclusions": [], "decisions": [], "done_tasks": [], "tasks": [],
            "minor_tasks": []})])
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: backend)
        analyze.analyze_transcript("[00:01] короткая встреча",
                                   user_notes="решили выкатить в пятницу")
        p = backend.prompts[0]
        assert "ЗАМЕТКИ УЧАСТНИКА" in p and "решили выкатить в пятницу" in p
        assert p.index("ЗАМЕТКИ") < p.index("Транскрипция")
