"""Пустой ответ модели не должен ронять провайдера непонятной ошибкой.

Боевой случай (протокол от 01.09.2026): в шапке стояло
«custom: 'NoneType' object has no attribute 'strip'». Причина — прямое
`out["choices"][0]["message"]["content"].strip()`: у рассуждающих моделей
`content` приходит null, а текст лежит в `reasoning_content`. По такому
сообщению в протоколе понять было нечего.
"""
from __future__ import annotations

import json

import pytest

from app import llm_custom
from app.llm_custom import CustomProvider, text_of

CFG = json.dumps({"base_url": "https://ai.api.cloud.yandex.net/v1",
                  "auth": "bearer", "model": "gpt://folder/deepseek/latest"})


def _answer(message: dict, finish: str = "stop") -> dict:
    return {"choices": [{"message": message, "finish_reason": finish}]}


class TestРазборОтвета:
    def test_обычная_строка(self):
        assert text_of(_answer({"content": " {\"a\": 1} "}))[0] == '{"a": 1}'

    def test_ответ_кусками(self):
        out = _answer({"content": [{"type": "text", "text": "{\"a\":"},
                                   {"type": "text", "text": " 1}"}]})
        assert text_of(out)[0] == '{"a": 1}'

    def test_рассуждающая_модель_кладёт_текст_в_reasoning(self):
        out = _answer({"content": None, "reasoning_content": "{\"a\": 1}"})
        assert text_of(out)[0] == '{"a": 1}'

    def test_пусто_остаётся_пустым(self):
        assert text_of(_answer({"content": None}))[0] == ""
        assert text_of({})[0] == ""
        assert text_of({"choices": []})[0] == ""

    def test_finish_reason_доносится(self):
        assert text_of(_answer({"content": None}, "length"))[1] == "length"


class TestПустойОтвет:
    def _provider(self, monkeypatch, out):
        p = CustomProvider(api_key="k", extra=CFG)
        monkeypatch.setattr(llm_custom, "_http_post_json",
                            lambda *a, **k: out)
        return p

    def test_вместо_NoneType_понятная_причина(self, monkeypatch):
        p = self._provider(monkeypatch, _answer({"content": None}))
        with pytest.raises(RuntimeError, match="вернула пустой ответ"):
            p.complete("текст")

    def test_обрыв_по_лимиту_назван_отдельно(self, monkeypatch):
        p = self._provider(monkeypatch, _answer({"content": None}, "length"))
        with pytest.raises(RuntimeError, match="лимит токенов"):
            p.complete("текст")

    def test_рассуждения_принимаются_за_ответ(self, monkeypatch):
        p = self._provider(monkeypatch, _answer(
            {"content": None, "reasoning_content": '{"topics": []}'}))
        assert p.complete("текст") == '{"topics": []}'


class TestПовторБезСтрогогоJSON:
    """Часть шлюзов принимает response_format и отвечает пустотой."""

    def test_повторяем_без_режима_и_получаем_ответ(self, monkeypatch):
        sent: list[dict] = []

        def fake(url, payload, headers, **kw):
            sent.append(payload)
            if "response_format" in payload:
                return _answer({"content": None})
            return _answer({"content": '{"topics": []}'})

        monkeypatch.setattr(llm_custom, "_http_post_json", fake)
        p = CustomProvider(api_key="k", extra=CFG)
        assert p.complete("текст") == '{"topics": []}'
        assert len(sent) == 2 and "response_format" not in sent[1]


class TestОстальныеПровайдеры:
    """Groq и GigaChat разбирают ответ тем же способом — и падали так же."""

    def test_groq_не_падает_на_none(self, monkeypatch):
        from app import llm
        monkeypatch.setattr(llm, "_http_post_json",
                            lambda *a, **k: _answer({"content": None}))
        with pytest.raises(RuntimeError, match="пустой ответ"):
            llm.GroqProvider(api_key="gsk_x").complete("текст")
