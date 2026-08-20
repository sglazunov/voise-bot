"""Ротация ключей: минутные лимиты нескольких аккаунтов складываются, и при
одновременном остывании ВСЕХ ключей ротатор ждёт сброса окна (секунды), а не
бросает движок — весь протокол собирают одни и те же ключи Groq.
"""
import time

import pytest

from app import llm
from app import llm_nvidia
from app.llm import _RotatingProvider


def _make_cls(behaviour: dict):
    """behaviour: api_key -> list of ответов; 'RL' = 429, 'ERR' = обычная ошибка."""
    class Fake:
        name = "groq"

        def __init__(self, model=None, api_key=None, extra=None):
            self.api_key = api_key

        def complete(self, prompt, max_tokens=2000, force_json=True):
            step = behaviour[self.api_key].pop(0)
            if step == "RL":
                raise RuntimeError("HTTP 429: rate limit, try again in 0.2s")
            if step == "ERR":
                raise RuntimeError("HTTP 500: boom")
            return step
    return Fake


def _rot(behaviour):
    creds = [(k, "") for k in behaviour]
    return _RotatingProvider(_make_cls(behaviour), None, creds)


class TestKeySumming:
    def test_second_key_picks_up_when_first_limited(self):
        rot = _rot({"k1": ["RL"], "k2": ["ответ"]})
        assert rot.complete("p") == "ответ"

    def test_three_keys_share_a_burst(self):
        # Три запроса подряд расходятся по трём ключам (round-robin).
        rot = _rot({"k1": ["a"], "k2": ["b"], "k3": ["c"]})
        assert [rot.complete("p") for _ in range(3)] == ["a", "b", "c"]

    def test_all_keys_cooling_waits_and_resumes(self, monkeypatch):
        # Оба ключа получают 429 с «try again in 0.2s» → ротатор ЖДЁТ сброс и
        # доделывает запрос теми же ключами, НЕ отдавая его другому движку.
        monkeypatch.setattr(llm, "KEY_WAIT_SEC", 5.0)
        rot = _rot({"k1": ["RL", "ответ после сна"], "k2": ["RL"]})
        t0 = time.time()
        assert rot.complete("p") == "ответ после сна"
        assert time.time() - t0 >= 0.2          # реально подождал окно

    def test_single_key_also_waits_instead_of_bailing(self, monkeypatch):
        monkeypatch.setattr(llm, "KEY_WAIT_SEC", 5.0)
        rot = _rot({"k1": ["RL", "ok"]})
        assert rot.complete("p") == "ok"

    def test_long_cooldown_falls_through_to_chain(self, monkeypatch):
        # Сброс «через 999 с» — это не окно лимита, а серьёзная квота/авария:
        # ротатор сдаётся, и цепочка уходит к следующему движку.
        monkeypatch.setattr(llm, "KEY_WAIT_SEC", 1.0)
        monkeypatch.setattr(llm, "_cooldown_from", lambda e, default=60.0: 999.0)
        rot = _rot({"k1": ["RL"], "k2": ["RL"]})
        with pytest.raises(RuntimeError, match="упёрлись в лимит"):
            rot.complete("p")

    def test_real_error_raises_immediately(self):
        rot = _rot({"k1": ["ERR"], "k2": ["не должно понадобиться"]})
        with pytest.raises(RuntimeError, match="500"):
            rot.complete("p")


class TestПричинаОтката:
    """Человек выбрал DeepSeek, протокол собрал Gemini — и узнать почему было
    неоткуда. Причина пропуска движков теперь сохраняется на цепочке."""

    def test_причина_сохраняется_при_успешном_откате(self, monkeypatch):
        class Bad:
            name = "nvidia"
            model = "deepseek"

            def complete(self, *a, **k):
                raise RuntimeError("HTTP 429: rate limit")

        class Good:
            name = "gemini"
            model = "flash"

            def complete(self, *a, **k):
                return "ответ"

        chain = llm._FallbackChain([Bad(), Good()])
        assert chain.complete("тест") == "ответ"
        assert chain.skipped and "nvidia" in chain.skipped[0]
        assert "429" in chain.skipped[0]

    def test_без_отката_причин_нет(self):
        class Good:
            name = "groq"
            model = "llama"

            def complete(self, *a, **k):
                return "ok"

        chain = llm._FallbackChain([Good(), Good()])
        chain.complete("тест")
        assert chain.skipped == []


class TestТаймаутNvidia:
    """Причина, по которой DeepSeek «не работал»: таймаут 180 с резал
    генерацию протокола посередине («The read operation timed out»), и работу
    молча забирал запасной движок. Проверка моделей при этом проходила —
    она просит один токен и отвечает мгновенно."""

    def test_проба_не_ждёт_долго(self):
        assert llm_nvidia._nvidia_timeout(1) <= 180

    def test_полному_протоколу_дают_больше_прежних_180(self):
        assert llm_nvidia._nvidia_timeout(8000) > 180

    def test_потолок_ограничен_из_за_одного_воркера(self):
        """Повисший запрос задерживает всю очередь распознавания."""
        assert llm_nvidia._nvidia_timeout(1_000_000) <= 420
