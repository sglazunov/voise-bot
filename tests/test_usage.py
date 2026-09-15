"""Учёт расхода модели по командам.

Разбор 14.09.2026: учёта токенов в проекте не было вовсе — `usage` из ответов
провайдеров выбрасывался, и «сколько потратила команда» узнать было нечем.
"""
from __future__ import annotations

import time

import pytest

from app import stats, usage


class TestCollect:
    def test_расход_копится_внутри_блока(self):
        with usage.collect() as acc:
            usage.record("gemini/x", 1000, 200, 300)
            usage.record("gemini/x", 500, 0, 100)
        assert (acc["calls"], acc["in"], acc["cached"], acc["out"]) == (2, 1500, 200, 400)
        assert acc["by_model"]["gemini/x"]["calls"] == 2

    def test_вне_блока_ничего_не_ломается(self):
        usage.record("gemini/x", 10, 0, 1)      # просто некуда записать

    def test_вложенный_сбор_доливается_во_внешний(self):
        # Пересборка внутри прогона не должна «терять» свои токены.
        with usage.collect() as outer:
            usage.record("a", 100, 0, 10)
            with usage.collect() as inner:
                usage.record("a", 50, 0, 5)
            assert inner["calls"] == 1
        assert outer["calls"] == 2 and outer["in"] == 150

    def test_потоки_не_путают_расход(self):
        import threading
        seen = {}

        def worker(name, n):
            with usage.collect() as acc:
                usage.record(name, n, 0, n)
                time.sleep(0.01)
                seen[name] = acc

        ts = [threading.Thread(target=worker, args=(f"m{i}", i * 100))
              for i in range(1, 4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert [seen[f"m{i}"]["in"] for i in (1, 2, 3)] == [100, 200, 300]


class TestPrices:
    def test_цена_ищется_по_началу_имени(self):
        assert usage.price_of("anthropic/claude-sonnet-5") == (2.0, 10.0)
        assert usage.price_of("claude-opus-5-20260401") == (5.0, 25.0)

    def test_неизвестная_модель_без_цены(self):
        assert usage.price_of("gemini-3.1-flash-lite") is None

    def test_деньги_не_считаются_если_цена_неизвестна(self):
        # Частичная сумма выглядит как полная и обманывает сильнее пустоты.
        assert usage.cost_usd({"gemini-3.1-flash-lite": {"in": 1000, "out": 100}}) is None
        assert usage.cost_usd({"claude-sonnet-5": {"in": 1000, "out": 100},
                               "gemini-x": {"in": 10, "out": 1}}) is None

    def test_кэш_считается_дешевле_входа(self):
        full = usage.cost_usd({"claude-sonnet-5": {"in": 1_000_000, "cached": 0, "out": 0}})
        cached = usage.cost_usd({"claude-sonnet-5": {"in": 1_000_000,
                                                     "cached": 1_000_000, "out": 0}})
        assert full == pytest.approx(2.0)
        assert cached < full

    def test_цена_задаётся_переменной(self, monkeypatch):
        monkeypatch.setenv("VTX_MODEL_PRICES", '{"gemini-3.1": [0.1, 0.4]}')
        usage._load_extra_prices()
        try:
            assert usage.price_of("gemini/gemini-3.1-flash-lite") == (0.1, 0.4)
        finally:
            usage._PRICES.pop("gemini-3.1", None)

    def test_кривая_переменная_не_роняет_старт(self, monkeypatch):
        monkeypatch.setenv("VTX_MODEL_PRICES", "не json")
        usage._load_extra_prices()      # только предупреждение в лог


class _Job:
    id = "j1"
    owner = "alice"
    filename = "встреча.mp4"
    duration = 3600.0
    speakers = 4
    status = "done"
    finished_at = time.time()
    analysis = {"tasks": [1, 2], "decisions": [1], "participants": [1, 2]}
    llm_usage = {"calls": 8, "in": 77400, "cached": 5000, "out": 14600,
                 "by_model": {"claude-sonnet-5": {"calls": 8, "in": 77400,
                                                  "cached": 5000, "out": 14600}}}


class TestStatsRow:
    """Расход обязан пережить суточный ретеншн задач — значит живёт в метриках."""

    def test_расход_попадает_в_метрики_и_суммируется(self, tmp_path, monkeypatch):
        monkeypatch.setattr(stats.security, "team_of", lambda u: "alice")
        monkeypatch.setattr(stats.security, "user_dir", lambda t: tmp_path)
        monkeypatch.setattr(stats.db, "enabled", lambda: False)
        stats.record(_Job())
        s = stats.summary("alice", days=7)
        assert s["llm_calls"] == 8
        assert s["tokens_in"] == 77400 and s["tokens_out"] == 14600
        assert s["tokens_per_meeting"] == 92000
        assert s["usd"] == pytest.approx(0.30, abs=0.02)
        assert s["usd_meetings"] == 1
        assert s["engines"] == ["claude-sonnet-5"]

    def test_без_цены_деньги_пустые_а_токены_на_месте(self, tmp_path, monkeypatch):
        monkeypatch.setattr(stats.security, "team_of", lambda u: "bob")
        monkeypatch.setattr(stats.security, "user_dir", lambda t: tmp_path)
        monkeypatch.setattr(stats.db, "enabled", lambda: False)
        job = _Job()
        job.llm_usage = dict(job.llm_usage,
                             by_model={"gemini-3.1-flash-lite": {"in": 100, "out": 10}})
        stats.record(job)
        s = stats.summary("bob", days=7)
        assert s["usd"] is None and s["usd_meetings"] == 0
        assert s["tokens_in"] == 77400

    def test_задача_без_расхода_не_ломает_метрики(self, tmp_path, monkeypatch):
        monkeypatch.setattr(stats.security, "team_of", lambda u: "carol")
        monkeypatch.setattr(stats.security, "user_dir", lambda t: tmp_path)
        monkeypatch.setattr(stats.db, "enabled", lambda: False)
        job = _Job()
        job.llm_usage = {}
        stats.record(job)
        s = stats.summary("carol", days=7)
        assert s["llm_calls"] == 0 and s["usd"] is None


class TestGroqPrices:
    """Цены Groq взяты из его каталога моделей (14.09.2026) — это самый дешёвый
    движок в проекте, и на нём держится вся арифметика подписки."""

    def test_имя_с_косой_чертой_находит_цену(self):
        # Провайдер сообщает «groq/openai/gpt-oss-120b» — имя модели само
        # содержит косую черту, и разбор не должен на ней спотыкаться.
        assert usage.price_of("groq/openai/gpt-oss-120b") == (0.15, 0.60)
        assert usage.price_of("groq/openai/gpt-oss-20b") == (0.075, 0.30)

    def test_часовая_встреча_стоит_копейки(self):
        # 77 400 токенов входа и 14 600 выхода — замер часовой встречи.
        c = usage.cost_usd({"groq/openai/gpt-oss-120b":
                            {"in": 77400, "cached": 0, "out": 14600}})
        assert 0.015 < c < 0.03, f"ожидали пару центов, получили {c}"
