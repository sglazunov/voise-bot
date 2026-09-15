"""Стадии вызова модели и вход бота (docs/ТЗ-МЕТРИКИ.md §14.4, И6/И8/И36).

Две вещи, которые код знал и выбрасывал:

* **Чем занят движок.** Расход встречи был одним числом, и вопрос «за что
  заплатили» оставался без ответа. Проверка цитат на длинной встрече стоит
  больше половины входа — увидеть это можно, только разделив вызовы по
  стадиям (карта / уплотнение / сведение / проверка / перегенерация / вопрос).
* **Когда бот вошёл.** `bot.join()` отвечал «да/нет», и этим всё
  заканчивалось. «Явка бота» — не только «пришёл или нет»: бот, вошедший к
  середине, теряет начало, где обычно и ставят задачи; а «не пустили»
  (вёрстка Телемоста, вход в Яндекс) и «не пришёл» (планировщик) лечатся
  по-разному и сливались в один отказ.
"""
from __future__ import annotations

import time

import pytest

from app import events, stats, usage


@pytest.fixture
def team(tmp_path, monkeypatch):
    monkeypatch.setattr(stats.security, "team_of", lambda u: "team")
    monkeypatch.setattr(stats.security, "user_dir", lambda t: tmp_path)
    monkeypatch.setattr(stats.db, "enabled", lambda: False)
    monkeypatch.setattr(events.security, "team_of", lambda u: "team")
    monkeypatch.setattr(events.security, "user_dir", lambda t: tmp_path)
    monkeypatch.setattr(events.db, "enabled", lambda: False)


@pytest.fixture
def priced(monkeypatch):
    monkeypatch.setitem(usage._PRICES, "тест-модель", (10.0, 30.0))


# --------------------------------------------------------------------------- #
# 1. Стадия вызова
# --------------------------------------------------------------------------- #
class TestStage:
    def test_вызов_помечен_стадией(self):
        with usage.collect() as acc:
            with usage.stage(usage.VERIFY):
                usage.record("m", 100, 0, 20)
        assert acc["log"][0]["stage"] == usage.VERIFY

    def test_вне_стадии_поле_пустое(self):
        with usage.collect() as acc:
            usage.record("m", 100, 0, 20)
        assert acc["log"][0]["stage"] is None

    def test_ближайшая_стадия_побеждает(self):
        """Проверка цитат идёт ВНУТРИ сборки протокола — и это проверка."""
        with usage.collect() as acc:
            with usage.stage(usage.REDUCE):
                with usage.stage(usage.VERIFY):
                    usage.record("m", 1, 0, 1)
                usage.record("m", 1, 0, 1)
        assert [c["stage"] for c in acc["log"]] == [usage.VERIFY, usage.REDUCE]

    def test_неизвестная_стадия_не_записывается(self):
        """Опечатка в коде не должна заводить новую строку в отчёте."""
        with usage.collect() as acc:
            with usage.stage("вермишель"):
                usage.record("m", 1, 0, 1)
        assert acc["log"][0]["stage"] is None

    def test_цена_фиксируется_в_момент_вызова(self, priced, monkeypatch):
        """⚠️ Прайсы меняются. Себестоимость августа в сентябрьских ценах — не
        измерение, а домысел, поэтому цена считается сразу и не пересчитывается."""
        with usage.collect() as acc:
            usage.record("тест-модель", 1_000_000, 0, 0)
        was = acc["log"][0]["usd"]
        assert was == pytest.approx(10.0)
        monkeypatch.setitem(usage._PRICES, "тест-модель", (99.0, 99.0))
        assert acc["log"][0]["usd"] == pytest.approx(was)   # не пересчиталось

    def test_без_цены_деньги_остаются_пустыми(self):
        with usage.collect() as acc:
            usage.record("неизвестная-модель", 1000, 0, 100)
        assert acc["log"][0]["usd"] is None

    def test_вложенный_сбор_доливает_и_журнал(self):
        with usage.collect() as outer:
            with usage.collect():
                with usage.stage(usage.MAP):
                    usage.record("m", 5, 0, 5)
        assert [c["stage"] for c in outer["log"]] == [usage.MAP]

    def test_журнал_ограничен_сверху(self):
        """Задача, которую пересобирали весь день, не должна раздуть строку."""
        with usage.collect() as acc:
            for _ in range(usage._MAX_LOG + 25):
                usage.record("m", 1, 0, 1)
        assert len(acc["log"]) == usage._MAX_LOG
        assert acc["calls"] == usage._MAX_LOG + 25      # итог не теряется


# --------------------------------------------------------------------------- #
# 2. События вызовов и отчёт по стадиям
# --------------------------------------------------------------------------- #
class TestStageReport:
    def test_пачка_пишется_одной_операцией(self, team):
        rows = [{"model": "m", "stage": usage.MAP, "in": 10, "cached": 0,
                 "out": 2, "usd": 0.1} for _ in range(30)]
        assert events.record_many(rows, user="u", job_id="j1") == 30
        saved = events.load("team", 0)
        assert len(saved) == 30
        assert {r["kind"] for r in saved} == {events.LLM_CALL}
        # ⚠️ Служебное событие не человеческое действие: в метрики ценности
        # такие вызовы попасть не должны.
        assert events.human_rows(saved) == []

    def _built(self, jid: str):
        job = type("J", (), {
            "id": jid, "owner": "u", "filename": "встреча.mp4", "duration": 3600.0,
            "speakers": 2, "status": "done", "finished_at": time.time(),
            "analysis_error": None, "provider": "gemini", "preset": "planerka",
            "transcribe_sec": 2400.0, "analysis": {"tasks": [], "decisions": []},
            "llm_usage": {}, "stop_reason": "call_ended",
        })()
        stats.record(job)

    def test_расход_разложен_по_стадиям(self, team):
        self._built("j1")
        events.record_many(
            [{"model": "m", "stage": usage.VERIFY, "in": 8000, "out": 400, "usd": 0.2},
             {"model": "m", "stage": usage.VERIFY, "in": 8000, "out": 400, "usd": 0.2},
             {"model": "m", "stage": usage.MAP, "in": 3000, "out": 900, "usd": 0.05}],
            user="u", job_id="j1")
        by = {x["stage"]: x for x in stats.summary("u", days=30)["by_stage"]}
        assert by["verify"]["calls"] == 2 and by["verify"]["in"] == 16000
        assert by["verify"]["usd"] == pytest.approx(0.4)
        assert by["verify"]["label"] == "проверка цитат"
        # Самая дорогая стадия идёт первой — ради этого разбивка и нужна.
        assert stats.summary("u", days=30)["by_stage"][0]["stage"] == "verify"

    def test_стадия_без_цены_не_даёт_частичной_суммы(self, team):
        """Частичная сумма выглядит как полная и обманывает сильнее пустого места."""
        self._built("j1")
        events.record_many(
            [{"model": "m", "stage": usage.MAP, "in": 10, "out": 1, "usd": 0.5},
             {"model": "m", "stage": usage.MAP, "in": 10, "out": 1, "usd": None}],
            user="u", job_id="j1")
        by = {x["stage"]: x for x in stats.summary("u", days=30)["by_stage"]}
        assert by["map"]["usd"] is None and by["map"]["calls"] == 2


# --------------------------------------------------------------------------- #
# 3. Вход бота: не пришёл / не пустили / опоздал
# --------------------------------------------------------------------------- #
class TestJoin:
    def _state(self, **over):
        from app.automation.scheduler import MeetingState
        kw = dict(key="team:1:2026-09-15T10:00:00+00:00", task_id="1",
                  title="Планёрка", url="u", start=None, owner="team")
        kw.update(over)
        return MeetingState(**kw)

    def test_не_пустили_отделено_от_не_пришёл(self, team):
        stats.record_meeting(self._state(stop_reason="join_failed"), "rec_error")
        stats.record_meeting(self._state(key="k2"), "missed")
        s = stats.summary("u", days=30)
        assert s["join_failed"] == 1
        assert s["missed"] == 1 and s["rec_failed"] == 1

    def test_опоздание_видно_отдельно(self, team):
        stats.record_meeting(self._state(key="a", join_delay_sec=15.0), "recorded")
        stats.record_meeting(self._state(key="b", join_delay_sec=900.0), "recorded")
        s = stats.summary("u", days=30)
        assert s["late_joins"] == 1
        assert s["join_delay_median_sec"] == round((15.0 + 900.0) / 2)

    def test_опоздавший_не_считается_вовремя(self, team):
        """Бот, вошедший к середине, формально записал встречу — но начала, где
        обычно и ставят задачи, в записи нет. В «вовремя» он не идёт."""
        for i in range(20):
            stats.record_meeting(
                self._state(key=f"k{i}", join_delay_sec=(900.0 if i < 5 else 10.0)),
                "recorded")
        s = stats.summary("u", days=30)
        assert s["attendance"] == 1.0            # пришёл на все
        assert s["on_time"] == pytest.approx(round(15 / 20, 3))

    def test_задержка_переживает_перезапуск(self, tmp_path, monkeypatch):
        from app.automation import snapshots
        monkeypatch.setattr(snapshots.security, "user_dir", lambda u: tmp_path)
        monkeypatch.setattr(snapshots.db, "enabled", lambda: False)
        snap = snapshots.of_state(self._state(state="done", join_delay_sec=42.0))
        assert snap["join_delay_sec"] == 42.0


class TestRecorderResult:
    def test_отказ_входа_называет_причину(self, monkeypatch, tmp_path):
        """Раньше эта ветка не возвращала `reason`, и «не пустили» в метрике
        было неотличимо от любого другого сбоя записи."""
        from app.automation import recorder
        from app.automation.recorder import browser

        class _Bot:
            def __init__(self, cfg, on_log=None, display=None, sink=None):
                pass

            def join(self, url, should_stop=None):
                return False

            def screenshot(self, path):
                pass

            def close(self):
                pass

        monkeypatch.setattr(recorder, "_RECORDER_ENABLED", True)
        monkeypatch.setattr(browser, "TelemostBot", _Bot)
        slot = type("S", (), {"index": 0, "display": ":99", "sink": "meet0",
                              "source": "meet0.monitor"})()
        res = recorder.record_meeting("https://telemost/x",
                                      str(tmp_path / "a.mp4"), {}, slot=slot)
        assert res["ok"] is False and res["reason"] == "join_failed"


# --------------------------------------------------------------------------- #
# 4. Сквозь конвейер: стадии проставляются НАСТОЯЩИМ кодом анализа
# --------------------------------------------------------------------------- #
class TestPipelineStages:
    """Единичные тесты стадии проверяют контракт `usage.stage`. Здесь —
    что его действительно расставили по конвейеру: забытый `with` в одной
    ветке даёт стадию `None` и строку «прочее» в отчёте, а не ошибку."""

    def test_карта_сведение_и_проверка_помечены(self, monkeypatch):
        import json as _json

        from app import analyze

        class _Backend:
            name = "fake"
            model = "тест-модель"

            def __init__(self, answers):
                self.answers = list(answers)

            def complete(self, prompt, max_tokens=1000, force_json=True, **kw):
                usage.record(self.model, len(prompt) // 4, 0, 50)
                return self.answers.pop(0)

        monkeypatch.setattr(analyze, "MIN_SPEECH_WORDS", 0)
        monkeypatch.setattr(analyze, "_MAX_TOPIC_REGENS", 0)
        monkeypatch.setattr(analyze, "_MAX_CHARS", 50)
        monkeypatch.setattr(analyze, "_CHUNK_CHARS", 120)
        monkeypatch.setattr(analyze, "_CHUNK_OVERLAP", 0)
        text = ("[00:00:00] " + "обсуждение первой темы " * 4 + "\n"
                + "[00:10:00] " + "обсуждение второй темы " * 4)
        notes = _json.dumps({"topics": [{"topic": "Т", "details": "д"}],
                             "tasks": [], "decisions": [], "statuses": []},
                            ensure_ascii=False)
        proto = _json.dumps({"participants": [], "summary": "с",
                             "detailed": [{"topic": "Т", "details": "д"}],
                             "key_thoughts": [], "conclusions": [],
                             "decisions": [], "done_tasks": [], "tasks": [],
                             "minor_tasks": [], "statuses": []},
                            ensure_ascii=False)
        backend = _Backend([notes, notes, proto])
        monkeypatch.setattr(analyze.llm, "get_provider_chain",
                            lambda *a, **k: backend)
        with usage.collect() as acc:
            analyze.analyze_transcript(text)
        stages = [c["stage"] for c in acc["log"]]
        assert stages == [usage.MAP, usage.MAP, usage.REDUCE]

    def test_проверка_цитат_помечена(self, monkeypatch):
        import json as _json

        from app import analyze

        class _Backend:
            name = "fake"
            model = "тест-модель"

            def complete(self, prompt, max_tokens=1000, force_json=True, **kw):
                usage.record(self.model, 10, 0, 5)
                return _json.dumps({"items": [{"i": 1, "quote": "я сделаю отчёт",
                                               "t": "00:00", "owner_ok": True}]},
                                   ensure_ascii=False)

        monkeypatch.setattr(analyze.llm, "get_provider_chain",
                            lambda *a, **k: _Backend())
        result = {"tasks": [{"task": "сделать отчёт", "owner": "Зоя"}],
                  "decisions": [], "done_tasks": [], "minor_tasks": []}
        with usage.collect() as acc:
            analyze.verify_protocol(result, "[00:00:00] Зоя: я сделаю отчёт")
        assert acc["log"] and set(c["stage"] for c in acc["log"]) == {usage.VERIFY}
