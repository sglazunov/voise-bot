"""Юнит-экономика и отчёты (docs/ТЗ-МЕТРИКИ.md §7, §11).

Себестоимость встречи складывается из трёх компонентов — модель (по событиям
llm_call с ценой на момент вызова), распознавание (машино-час × доля ядер ×
секунды) и запись (час слота × длительность). Правила, которые здесь
закреплены:

* компонент считается ТОЛЬКО при заданной цене, встреча получает итог только
  при известных всех трёх — частичная себестоимость врёт в сторону «дёшево»;
* никаких средних: медиана и 90-й перцентиль (И29);
* вызовы на ключе клиента для владельца бесплатны и считаются отдельно (И34);
* пересборки — повторная себестоимость той же встречи (И27/И32);
* отчёт клиенту не содержит себестоимости и движков (И59).
"""
from __future__ import annotations

import time

import pytest

from app import events, stats, usage


@pytest.fixture
def team(tmp_path, monkeypatch):
    for mod in (stats, events):
        monkeypatch.setattr(mod.security, "team_of", lambda u: "team")
        monkeypatch.setattr(mod.security, "user_dir", lambda t: tmp_path)
        monkeypatch.setattr(mod.db, "enabled", lambda: False)
    from app.automation import settings as auto_settings
    monkeypatch.setattr(auto_settings, "load", lambda t: {})


@pytest.fixture
def prices(monkeypatch):
    monkeypatch.setattr(stats, "COST_CPU_HOUR_USD", 0.10)
    monkeypatch.setattr(stats, "COST_SLOT_HOUR_USD", 0.02)
    monkeypatch.setattr(stats, "USD_RUB", 100.0)
    monkeypatch.setattr(stats, "SUPPORT_HOUR_RUB", 1000.0)
    monkeypatch.setattr(stats, "_cores_share", lambda: 0.5)


def _meeting(jid: str, hours: float = 1.0, transcribe_sec: float = 1800.0,
             calls=None, tasks=None, statuses=None, carried=None, silent=False):
    now = time.time()
    a = {"tasks": tasks if tasks is not None else [{"task": "a", "owner": "Зоя"}],
         "decisions": ["d"]}
    if statuses:
        a["statuses"] = statuses
    if carried:
        a["_carried"] = {"items": carried}
    job = type("J", (), {
        "id": jid, "owner": "u", "filename": "встреча.mp4", "duration": hours * 3600,
        "speakers": 2, "status": "done", "finished_at": now, "analysis_error": None,
        "provider": "gemini", "preset": "planerka", "transcribe_sec": transcribe_sec,
        "analysis": a, "llm_usage": {}, "stop_reason": "call_ended",
    })()
    stats.record(job)
    st = type("S", (), {"key": f"k-{jid}", "owner": "team", "title": "Планёрка",
                        "recorded_sec": hours * 3600, "stop_reason": "call_ended",
                        "upload_error": None, "detail": "", "join_delay_sec": 10.0,
                        "audio_warning": silent, "job_id": jid})()
    stats.record_meeting(st, "recorded")
    if calls:
        events.record_many(calls, user="u", job_id=jid)


def _call(usd, key="service", rerun=False, stage="map"):
    return {"model": "m", "stage": stage, "in": 1000, "out": 100, "usd": usd,
            "key": key, "rerun": rerun}


class TestPercentile:
    def test_ближайший_ранг(self):
        assert stats._pct([1, 2, 3, 4, 10], 0.9) == 10
        assert stats._pct([1, 2, 3, 4, 10], 0.5) == 3
        assert stats._pct([], 0.5) is None


class TestCostComponents:
    def test_без_цен_себестоимости_нет(self, team):
        _meeting("j1", calls=[_call(0.5)])
        s = stats.summary("u", 30)
        assert s["econ_cost_total_usd"] is None
        assert "VTX_COST_CPU_HOUR_USD" in s["econ_cost_missing"]

    def test_три_компонента_складываются(self, team, prices):
        # модель 0.5; распознавание 1800/3600 × 0.10 × 0.5 = 0.025; запись 0.02
        _meeting("j1", calls=[_call(0.3), _call(0.2)])
        s = stats.summary("u", 30)
        assert s["econ_cost_llm_usd"] == pytest.approx(0.5)
        assert s["econ_cost_recog_usd"] == pytest.approx(0.025)
        assert s["econ_cost_record_usd"] == pytest.approx(0.02)
        assert s["econ_cost_total_usd"] == pytest.approx(0.545)
        assert s["econ_priced_meetings"] == 1

    def test_встреча_с_вызовом_без_цены_не_получает_итога(self, team, prices):
        """Частичная себестоимость выглядит как полная и врёт в сторону «дёшево»."""
        _meeting("j1", calls=[_call(0.3), _call(None)])
        _meeting("j2", calls=[_call(0.4)])
        s = stats.summary("u", 30)
        assert s["econ_priced_meetings"] == 1
        assert s["econ_cost_llm_usd"] == pytest.approx(0.4)

    def test_ключ_клиента_не_идёт_в_себестоимость_сервиса(self, team, prices):
        _meeting("j1", calls=[_call(0.3, key="team"), _call(0.1)])
        s = stats.summary("u", 30)
        assert s["econ_cost_llm_usd"] == pytest.approx(0.1)
        assert s["econ_cost_client_key_usd"] == pytest.approx(0.3)

    def test_доля_пересборок(self, team, prices):
        _meeting("j1", calls=[_call(0.6), _call(0.2, rerun=True), _call(0.2, rerun=True)])
        assert stats.summary("u", 30)["econ_rerun_cost_share"] == pytest.approx(0.4)

    def test_медиана_и_перцентиль_а_не_среднее(self, team, prices):
        """Одна четырёхчасовая встреча из десяти не должна тянуть «типичную»
        себестоимость: среднее выросло бы впятеро, медиана и 90-й перцентиль —
        нет. Итог за период при этом её содержит."""
        for i in range(9):
            _meeting(f"j{i}", calls=[_call(0.1)])
        _meeting("big", hours=4.0, calls=[_call(5.0)])
        s = stats.summary("u", 30)
        assert s["econ_cost_median_usd"] < 0.2
        assert s["econ_cost_p90_usd"] < 0.2
        assert s["econ_cost_total_usd"] > 5.0
        # А когда тяжёлых встреч уже каждая пятая — 90-й перцентиль их показывает.
        _meeting("big2", hours=4.0, calls=[_call(5.0)])
        assert stats.summary("u", 30)["econ_cost_p90_usd"] > 4.0

    def test_себестоимость_минуты_и_часа(self, team, prices):
        _meeting("j1", hours=2.0, transcribe_sec=0.0, calls=[_call(1.2)])
        s = stats.summary("u", 30)
        # 1.2 + 0 + 2ч × 0.02 = 1.24 на 120 минут
        assert s["econ_cost_per_minute_usd"] == pytest.approx(1.24 / 120, rel=1e-3)
        assert s["econ_cost_per_hour_usd"] == pytest.approx(0.62, rel=1e-3)


class TestTeamEconomics:
    def test_подписка_даёт_отношение_и_безубыточность(self, team, prices, monkeypatch):
        from app.automation import settings as auto_settings
        monkeypatch.setattr(auto_settings, "load",
                            lambda t: {"subscription_rub": 10000, "support_hours_month": 1})
        _meeting("j1", hours=1.0, transcribe_sec=0.0, calls=[_call(0.98)])
        s = stats.summary("u", 30)
        # подписка $100; команда 1 ч × 1000 ₽ = $10; встреча 0.98 + 0.02 = $1.00
        assert s["econ_team_cost_usd"] == pytest.approx(10.0)
        assert s["econ_cost_to_price"] == pytest.approx(0.11)
        # (100 − 10) / $1 за час = 90 часов встреч до убытка
        assert s["econ_breakeven_hours"] == pytest.approx(90.0)

    def test_без_подписки_отношения_нет(self, team, prices):
        _meeting("j1", calls=[_call(0.5)])
        s = stats.summary("u", 30)
        assert s["econ_cost_to_price"] is None and s["econ_breakeven_hours"] is None

    def test_объяснения_маржи(self, team, prices):
        _meeting("short", hours=1.0, calls=[_call(0.1)])
        _meeting("long", hours=3.0, calls=[_call(0.1)], silent=True)
        s = stats.summary("u", 30)
        assert s["econ_long_minutes_share"] == pytest.approx(0.75)
        assert s["econ_silent_share"] == pytest.approx(0.5)
        assert s["econ_duration_p95_min"] == 180


class TestClientReport:
    def test_отчёт_без_себестоимости_и_с_формулой(self, team):
        _meeting("j1", tasks=[{"task": "a", "owner": "Зоя"}, {"task": "b", "owner": "—"}],
                 statuses=[{"item": "нашли?", "status": "без ответа"}],
                 carried=[{"task": "t", "status": "без упоминания"}])
        r = stats.client_report("u", 30)
        text = r["text"]
        assert "Обработано встреч: 1" in text
        assert "× 0.5" in text and "не замер" in text        # формула у оценки
        assert "задач без ответственного — 1" in text
        assert "вопросов повисло без ответа — 1" in text
        assert "о которых не вспомнили, — 1" in text
        assert "Бот пришёл на 1 встречу из 1" in text
        # И59: себестоимость, токены и движки клиенту не показываются.
        for banned in ("$", "токен", "gemini", "себестоимость"):
            assert banned not in text.lower()

    def test_склонение(self):
        assert stats._plural(1, "встречу", "встречи", "встреч") == "встречу"
        assert stats._plural(3, "встречу", "встречи", "встреч") == "встречи"
        assert stats._plural(11, "встречу", "встречи", "встреч") == "встреч"
        assert stats._plural(21, "встречу", "встречи", "встреч") == "встречу"


class TestOwnerReport:
    def test_спящая_платящая_команда_в_списке(self, team, monkeypatch, prices):
        from app.automation import settings as auto_settings
        monkeypatch.setattr(stats.security, "list_teams", lambda: ["team"])
        monkeypatch.setattr(auto_settings, "load", lambda t: {"subscription_rub": 5000})
        _meeting("j1", calls=[_call(0.2)])          # протокол есть, никто не открыл
        r = stats.owner_report(days=7)
        assert r["teams"][0]["team"] == "team" and r["teams"][0]["paying"] is True
        assert r["sleeping"] and r["sleeping"][0]["team"] == "team"

    def test_прочитанная_команда_не_спит(self, team, monkeypatch, prices):
        from app.automation import settings as auto_settings
        monkeypatch.setattr(stats.security, "list_teams", lambda: ["team"])
        monkeypatch.setattr(auto_settings, "load", lambda t: {"subscription_rub": 5000})
        _meeting("j1", calls=[_call(0.2)])
        events.record(events.OPENED, user="зоя", job_id="j1")
        assert stats.owner_report(days=7)["sleeping"] == []

    def test_неплатящая_не_спит_по_определению(self, team, monkeypatch):
        monkeypatch.setattr(stats.security, "list_teams", lambda: ["team"])
        _meeting("j1")
        r = stats.owner_report(days=7)
        assert r["sleeping"] == [] and r["teams"][0]["paying"] is False


class TestOwnerEndpoint:
    def test_только_основателю(self, client):
        from conftest import register, login
        register(client, "alice")                 # первый аккаунт = основатель
        login(client, "alice")
        assert client.get("/api/stats/owner").status_code == 200
        from starlette.testclient import TestClient
        from app.main import app
        other = TestClient(app)
        assert register(other, "bob", phone="+79995556677").status_code == 200
        assert login(other, "bob").status_code == 200
        assert other.get("/api/stats/owner").status_code == 403
        assert other.get("/api/stats/report").status_code == 200
