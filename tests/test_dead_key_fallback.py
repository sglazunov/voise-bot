"""Протухшее подключение не должно уводить встречу к запасному движку.

Боевой случай (протокол от 01.09.2026). Под «своим ключом» лежали ДВА разных
подключения: старый ключ OpenRouter и Yandex Cloud из .env. Выбран был движок
Yandex, но пул ключей перебирался по порядку: первый ключ ответил
«HTTP 401 Missing Authentication header», обёртка ротации сделала raise — и
весь провайдер «custom» объявили отказавшим. Яндекс не попробовали НИ РАЗУ,
протокол собрал Gemini, а в шапке написали «выбранный движок не ответил».

Ошибка 401 — про КЛЮЧ, а не про движок. Соседний ключ в пуле может вести на
совсем другой сервис.
"""
from __future__ import annotations

import json

import pytest

from app import config, llm


class _Rejecting:
    """Ключ, который сервис не принимает."""

    name = "custom"

    def __init__(self, model=None, api_key="", extra=""):
        self.model, self.api_key = model, api_key

    def complete(self, *a, **k):
        raise RuntimeError("HTTP 401 от https://openrouter.ai/api/v1/"
                           "chat/completions: Missing Authentication header")


class _Working:
    name = "custom"

    def __init__(self, model=None, api_key="", extra=""):
        self.model, self.api_key = model, api_key

    def complete(self, *a, **k):
        return "протокол"


class _Switching:
    """Отвечает или отказом, или делом — смотря какой ключ."""

    name = "custom"
    calls: list[str] = []

    def __init__(self, model=None, api_key="", extra=""):
        self.model, self.api_key = model, api_key

    def complete(self, *a, **k):
        _Switching.calls.append(self.api_key)
        if self.api_key == "мёртвый":
            raise RuntimeError("HTTP 401: Missing Authentication header")
        return "протокол"


class TestОтвергнутыйКлюч:
    def test_после_401_пробуется_следующий_ключ(self):
        _Switching.calls = []
        rot = llm._RotatingProvider(_Switching, None,
                                    [("мёртвый", ""), ("живой", "")])
        assert rot.complete("текст") == "протокол"
        assert _Switching.calls == ["мёртвый", "живой"]

    def test_мёртвый_ключ_больше_не_дёргают(self):
        _Switching.calls = []
        rot = llm._RotatingProvider(_Switching, None,
                                    [("мёртвый", ""), ("живой", "")])
        rot.complete("раз")
        rot.complete("два")
        # Второй вызов идёт сразу на живой ключ: мёртвый отложен надолго.
        assert _Switching.calls == ["мёртвый", "живой", "живой"]

    def test_когда_все_ключи_отвергнуты_причина_названа(self):
        rot = llm._RotatingProvider(_Rejecting, None, [("a", ""), ("b", "")])
        with pytest.raises(RuntimeError, match="отвергнуты сервисом"):
            rot.complete("текст")

    def test_единственный_ключ_падает_как_прежде(self):
        """С одним ключом откладывать нечего — ошибка должна дойти до цепочки,
        чтобы сработал запасной ДВИЖОК."""
        rot = llm._RotatingProvider(_Rejecting, None, [("a", "")])
        with pytest.raises(RuntimeError, match="401"):
            rot.complete("текст")

    def test_лимит_остаётся_лимитом(self):
        """429 — это не отказ ключа: ключ жив, ждать имеет смысл."""
        assert not llm.is_key_rejected(RuntimeError("HTTP 429: rate limit"))
        assert llm.is_key_rejected(RuntimeError("HTTP 403: permission denied"))


class TestКлючПодМодель:
    """Ключ OpenRouter не имеет отношения к модели Yandex Cloud."""

    def _keys(self):
        yc = json.dumps({"base_url": "https://ai.api.cloud.yandex.net/v1",
                         "hint": "Yandex Cloud AI Studio",
                         "models": ["gpt://folder/deepseek-v4/latest"]})
        orr = json.dumps({"base_url": "https://openrouter.ai/api/v1",
                          "hint": "OpenRouter", "models": ["some/other-model"]})
        return {"custom": [{"key": "ключ-openrouter", "extra": orr},
                           {"key": "ключ-яндекса", "extra": yc}]}

    def test_берётся_подключение_с_этой_моделью(self, monkeypatch):
        monkeypatch.setitem(llm._PROVIDERS, "custom", _Working)
        p = llm.get_provider("custom:gpt://folder/deepseek-v4/latest", self._keys())
        # Один подходящий ключ — значит и обёртки ротации быть не должно.
        assert p.api_key == "ключ-яндекса"

    def test_незнакомая_модель_не_отсекает_ключи(self, monkeypatch):
        """Список моделей мог устареть — тогда работаем как раньше, по всем."""
        monkeypatch.setitem(llm._PROVIDERS, "custom", _Working)
        p = llm.get_provider("custom:совсем/другая", self._keys())
        assert isinstance(p, llm._RotatingProvider)
