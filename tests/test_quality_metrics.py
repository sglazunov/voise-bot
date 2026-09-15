"""Качество протокола переживает ретеншн.

Всё, что здесь проверяется, УЖЕ считалось на каждой встрече и жило ровно сутки
внутри `job.analysis`: доля пунктов с цитатой-основанием, дефекты `_quality`,
удалённые галлюцинации, выброшенный шум, откат на запасной движок, следы ручной
правки. Разбор — docs/ТЗ-МЕТРИКИ.md §3.
"""
from __future__ import annotations

import time

import pytest

from app import analyze, stats


@pytest.fixture(autouse=True)
def _file_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(stats.security, "team_of", lambda u: "team")
    monkeypatch.setattr(stats.security, "user_dir", lambda t: tmp_path)
    monkeypatch.setattr(stats.db, "enabled", lambda: False)


def _job(**over):
    a = {
        "tasks": [{"task": "a", "owner": "Кирилл"}, {"task": "b", "owner": "—"}],
        "decisions": ["d"], "participants": [1],
        "detailed": [{"topic": "т1", "details": "…"}],
        "_quality": {"topics": 3, "empty_topics": [2], "summary_is_toc": True},
        "_dropped_topics": ["выдуманная тема"],
        "_dropped": ["написать Виктору за доступом", "микрофон"],
        "_fallback": ["gemini: 429"],
        "_edited": True,
        "_provider": "gemini",
        "verification": {"mode": "strict",
                         "stats": {"checked": 10, "confirmed": 7, "calls": 1,
                                   "failed_calls": 0,
                                   "version": analyze.VERIFY_VERSION}},
    }
    a.update(over.pop("analysis", {}))
    j = type("J", (), {
        "id": over.pop("id", "j1"), "owner": "u", "filename": "встреча.mp4",
        "duration": 3600.0, "speakers": 3, "status": "done",
        "finished_at": time.time(), "analysis_error": None, "provider": "gemini",
        "preset": "planerka", "transcribe_sec": 2520.0, "analysis": a,
        "llm_usage": {"calls": 8, "in": 1000, "cached": 0, "out": 200,
                      "by_model": {"gemini-3.1-flash-lite":
                                   {"calls": 8, "in": 1000, "cached": 0, "out": 200}}},
    })()
    for k, v in over.items():
        setattr(j, k, v)
    return j


class TestRow:
    def test_проверка_цитат_переживает_ретеншн(self):
        stats.record(_job())
        r = stats._file_load("team")[0]
        assert (r["verify_checked"], r["verify_confirmed"]) == (10, 7)
        assert r["verify_mode"] == "strict"

    def test_версия_правил_проверки_пишется_рядом(self):
        """Доля подтверждённых сравнима во времени только внутри одной версии:
        ослабить порог — и она прыгнет без единой правки протоколов."""
        stats.record(_job())
        assert stats._file_load("team")[0]["verify_version"] == analyze.VERIFY_VERSION

    def test_дефекты_и_выброшенное_сохраняются(self):
        stats.record(_job())
        r = stats._file_load("team")[0]
        assert r["topics"] == 3 and r["empty_topics"] == 1
        assert r["dropped_topics"] == 1 and r["dropped_items"] == 2
        assert r["summary_is_toc"] is True and r["edited"] is True
        assert r["fallback"] is True and r["engine"] == "gemini"

    def test_расход_по_движкам_с_числами(self):
        # Раньше сюда уходила только строка имён — сравнить движки было нечем.
        stats.record(_job())
        r = stats._file_load("team")[0]
        assert r["tokens_by_model"]["gemini-3.1-flash-lite"]["in"] == 1000

    def test_время_распознавания_и_тип_встречи(self):
        stats.record(_job())
        r = stats._file_load("team")[0]
        assert r["transcribe_sec"] == 2520.0 and r["preset"] == "planerka"


class TestProtocolOk:
    """«Задача завершилась» и «протокол готов» — разные вещи."""

    def test_несобравшийся_протокол_не_успех(self):
        stats.record(_job(analysis_error="Движок не ответил"))
        r = stats._file_load("team")[0]
        assert r["ok"] is True, "сама задача завершилась"
        assert r["protocol_ok"] is False, "а протокол не собрался"
        s = stats.summary("u", days=7)
        assert s["protocol_failed"] == 1

    def test_обычный_протокол_успех(self):
        stats.record(_job())
        assert stats.summary("u", days=7)["protocol_failed"] == 0


class TestCancelled:
    def test_отменённая_задача_попадает_в_метрики(self):
        # Раньше запись шла только на done/error, и отмена исчезала бесследно.
        stats.record(_job(status="cancelled", id="c1"))
        s = stats.summary("u", days=7)
        assert s["cancelled"] == 1


class TestSummary:
    def test_доля_подтверждённых_идёт_в_паре_с_абсолютом(self):
        """Саму долю легко «улучшить», выбросив все неподтверждённые пункты —
        поэтому рядом всегда абсолютное число на час встречи."""
        stats.record(_job())
        s = stats.summary("u", days=7)
        assert s["confirmed_ratio"] == 0.7
        assert s["confirmed_per_hour"] == 7.0

    def test_доля_задач_с_ответственным(self):
        stats.record(_job())
        s = stats.summary("u", days=7)
        assert s["tasks_with_owner"] == 1
        assert s["owner_ratio"] == pytest.approx(0.5)

    def test_несколько_версий_проверки_видно_в_сводке(self):
        stats.record(_job(id="a"))
        j = _job(id="b")
        j.analysis["verification"]["stats"]["version"] = "2020-01-01"
        stats.record(j)
        assert stats.summary("u", days=7)["verify_versions"] == \
            sorted(["2020-01-01", analyze.VERIFY_VERSION])

    def test_расход_по_движкам_суммируется(self):
        stats.record(_job(id="a"))
        stats.record(_job(id="b"))
        by = stats.summary("u", days=7)["by_engine"]
        assert by[0]["model"] == "gemini-3.1-flash-lite"
        assert by[0]["in"] == 2000 and by[0]["meetings"] == 2

    def test_коэффициент_распознавания(self):
        stats.record(_job())
        assert stats.summary("u", days=7)["transcribe_ratio"] == 0.7

    def test_кривой_протокол_не_роняет_метрику(self, caplog):
        # Строка всё равно должна появиться, а исключение — попасть в лог.
        stats.record(_job(analysis={"tasks": [1, 2, 3]}))
        assert stats._file_load("team"), "строка обязана быть записана"
