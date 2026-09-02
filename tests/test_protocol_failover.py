"""Ответ без протокола не должен стоить встречи (боевой случай 02.09.2026).

«Встреча лидеров» 28.08, 67 минут, DeepSeek через Yandex Cloud: карта встречи
собрана и оплачена, а на сведении модель дважды вернула текст без JSON —
рассуждения до конца лимита. В карточке: «Не удалось собрать протокол: В ответе
модели нет JSON-объекта: line 1 column 1 (char 0)». Запасной движок не
пробовался: цепочка переключается только на исключение, а ответ-то пришёл.
"""
from __future__ import annotations

import json

import pytest

from app import analyze, llm, llm_custom
from app.llm_custom import CustomProvider, text_of

CFG = json.dumps({"base_url": "https://ai.api.cloud.yandex.net/v1",
                  "auth": "bearer", "model": "gpt://folder/deepseek-v4-flash/latest"})
PROSE = ("Итак, нужно свести заметки по встрече. Сначала участники: Зоя Р, "
         "Кирилл Бубнов… Потом решения. Нужно аккуратно перечислить задачи")


def _answer(message: dict, finish: str = "stop") -> dict:
    return {"choices": [{"message": message, "finish_reason": finish}]}


class TestРассужденияБезОтвета:
    def test_голые_рассуждения_не_принимаются_за_ответ(self):
        out = _answer({"content": None, "reasoning_content": PROSE}, "length")
        assert text_of(out) == ("", "length")

    def test_объект_внутри_рассуждений_по_прежнему_берётся(self):
        out = _answer({"content": None, "reasoning_content": 'думаю… {"a": 1}'})
        assert text_of(out)[0] == 'думаю… {"a": 1}'

    def test_обрыв_по_лимиту_повторяется_с_удвоенным_лимитом(self, monkeypatch):
        sent: list[dict] = []

        def fake(url, payload, headers, **kw):
            sent.append(dict(payload))
            if len(sent) == 1:
                return _answer({"content": None, "reasoning_content": PROSE}, "length")
            return _answer({"content": '{"summary": "итог"}'})

        monkeypatch.setattr(llm_custom, "_http_post_json", fake)
        p = CustomProvider(api_key="k", extra=CFG)
        assert p.complete("текст", max_tokens=10000) == '{"summary": "итог"}'
        assert len(sent) == 2
        assert sent[1]["max_tokens"] == 20000 and "response_format" not in sent[1]

    def test_повтор_не_бесконечный_и_причина_названа(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            llm_custom, "_http_post_json",
            lambda url, payload, headers, **kw: calls.append(payload) or _answer(
                {"content": None, "reasoning_content": PROSE}, "length"))
        p = CustomProvider(api_key="k", extra=CFG)
        with pytest.raises(RuntimeError, match="рассуждени"):
            p.complete("текст", max_tokens=10000)
        assert len(calls) == 2


class _Engine:
    """Движок-заглушка: заметки на карту, свой ответ на сведение."""
    accepts_should_stop = False

    def __init__(self, name: str, reduce_answer: str):
        self.name = name
        self.model = name + "-model"
        self.reduce_answer = reduce_answer
        self.prompts: list[str] = []

    def complete(self, prompt, max_tokens=2000, force_json=True, **kw):
        self.prompts.append(prompt)
        if "Заметки по частям" in prompt:
            return self.reduce_answer
        return json.dumps({"time_range": "00:00–00:01", "participants": [],
                           "topics": [], "decisions": [], "tasks": []})


def _proto() -> str:
    return json.dumps({"participants": [], "summary": "итог встречи", "detailed": [],
                       "key_thoughts": [], "conclusions": [], "decisions": ["решили"],
                       "done_tasks": [], "minor_tasks": [],
                       "tasks": [{"task": "сделать", "owner": "Кирилл"}]})


@pytest.fixture
def long_meeting(monkeypatch):
    monkeypatch.setattr(analyze, "MIN_SPEECH_WORDS", 0)
    monkeypatch.setattr(analyze, "_MAX_TOPIC_REGENS", 0)
    monkeypatch.setattr(analyze, "_MAX_CHARS", 200)
    monkeypatch.setattr(analyze, "_CHUNK_CHARS", 150)
    return "\n".join(f"[00:{i:02d}] реплика номер {i} про запуск проекта" for i in range(30))


class TestСведениеПереходитКСледующемуДвижку:
    def test_карта_не_пересчитывается_а_сведение_делает_запасной(self, monkeypatch, long_meeting):
        a, b = _Engine("custom", PROSE), _Engine("gemini", _proto())
        chain = llm._FallbackChain([a, b])
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *x, **k: chain)
        res = analyze.analyze_transcript(long_meeting)
        assert res["summary"] == "итог встречи"
        assert res["_provider"] == "gemini" and res["_model"] == "gemini-model"
        maps_a = [p for p in a.prompts if "Фрагмент:" in p]
        assert len(maps_a) >= 2
        # у первого: карта + две попытки сведения; у второго — ТОЛЬКО сведение
        assert len(a.prompts) == len(maps_a) + 2
        assert len(b.prompts) == 1 and "Заметки по частям" in b.prompts[0]
        assert any("custom: дважды ответил не протоколом" in s for s in res["_fallback"])

    def test_снятый_движок_не_возвращается_в_цепочку(self):
        a, b = _Engine("custom", PROSE), _Engine("gemini", _proto())
        chain = llm._FallbackChain([a, b])
        assert chain.demote("custom: не JSON") is True
        assert chain.name == "gemini"
        chain.complete("Заметки по частям")
        assert a.prompts == [] and len(b.prompts) == 1
        assert chain.demote("gemini: тоже") is False

    def test_без_запасного_движка_ошибка_называет_модель_и_ответ(self, monkeypatch, long_meeting):
        a = _Engine("custom", PROSE)
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *x, **k: a)
        with pytest.raises(RuntimeError) as e:
            analyze.analyze_transcript(long_meeting)
        msg = str(e.value)
        assert "custom (custom-model)" in msg and "Итак, нужно свести" in msg
        assert "Пересобрать" in msg
        assert "нет JSON-объекта" not in msg

    def test_короткая_встреча_тоже_переходит(self, monkeypatch):
        monkeypatch.setattr(analyze, "MIN_SPEECH_WORDS", 0)
        monkeypatch.setattr(analyze, "_MAX_TOPIC_REGENS", 0)

        class Short(_Engine):
            def complete(self, prompt, max_tokens=2000, force_json=True, **kw):
                self.prompts.append(prompt)
                return self.reduce_answer

        a, b = Short("custom", PROSE), Short("gemini", _proto())
        chain = llm._FallbackChain([a, b])
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *x, **k: chain)
        res = analyze.analyze_transcript("[00:01] короткая встреча про запуск")
        assert res["_provider"] == "gemini" and len(a.prompts) == 2 and len(b.prompts) == 1
