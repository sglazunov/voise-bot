"""Groq gpt-oss — рассуждающая модель: пинг ключа и видимость отключённых.

После перехода Groq на openai/gpt-oss-120b подключение ключа падало с «модель
вернула пустой ответ»: проверочный пинг просил ответить одним словом в ПЯТЬ
токенов, а gpt-oss тратит лимит сначала на рассуждения — `content` оставался
пустым. Вторая находка того же дня: провайдер, отключённый через
VTX_PROVIDER_DISABLED, исчезал из интерфейса молча — «пропавший Gemini» искали
в коде, а он был выключен строкой в .env.
"""
from __future__ import annotations

import pytest

from app import config, llm


class TestGroqReasoning:
    def _capture(self, monkeypatch):
        seen = {}

        def fake_post(url, payload, headers, timeout=0, max_retries=0):
            seen.update(payload)
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 1}}

        monkeypatch.setattr(llm, "_http_post_json", fake_post)
        return seen

    def test_gpt_oss_получает_низкое_усилие_рассуждений(self, monkeypatch):
        seen = self._capture(monkeypatch)
        llm.GroqProvider(model="openai/gpt-oss-120b", api_key="k").complete(
            "ok?", max_tokens=64, force_json=False)
        assert seen["reasoning_effort"] == "low"

    def test_другим_моделям_параметр_не_шлётся(self, monkeypatch):
        """Чужой параметр — это 400 у поставщика; шлём только тем, кто его знает."""
        seen = self._capture(monkeypatch)
        llm.GroqProvider(model="llama-3.1-8b-instant", api_key="k").complete(
            "ok?", max_tokens=64, force_json=False)
        assert "reasoning_effort" not in seen

    def test_пустой_ответ_остаётся_внятной_ошибкой(self, monkeypatch):
        monkeypatch.setattr(llm, "_http_post_json", lambda *a, **k: {
            "choices": [{"message": {"content": None, "reasoning": "думаю…"},
                         "finish_reason": "length"}]})
        with pytest.raises(RuntimeError, match="пустой ответ"):
            llm.GroqProvider(model="openai/gpt-oss-120b", api_key="k").complete(
                "ok?", max_tokens=5, force_json=False)


class TestPingBudget:
    def test_пинг_подключения_даёт_место_рассуждениям(self):
        """5 токенов хватало Llama; рассуждающей модели нужен запас."""
        import inspect
        from app import main
        src = inspect.getsource(main.connect_provider)
        assert "max_tokens=64" in src and "max_tokens=5," not in src


class TestDisabledVisible:
    def test_отключённые_провайдеры_видны_в_ответе(self, client, monkeypatch):
        from conftest import login, register
        monkeypatch.setattr(config, "PROVIDER_DISABLED", {"gemini"})
        register(client, "alice")
        login(client, "alice")
        r = client.get("/api/providers")
        assert r.status_code == 200
        assert r.json()["disabled"] == ["gemini"]
