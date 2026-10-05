"""Одна постоянная комната Телемоста на все встречи команды (05.10.2026).

Три встречи подряд по задачам Weeek с одной ссылкой: бот записал 10:00, а
11:00 и 12:03 получили «Не записываем: бот уже в этом звонке» — вердикт
выносился за две минуты до начала, пока предыдущая запись ещё шла, и больше
не пересматривался. Теперь до начала карточка ЖДЁТ, с наступлением времени
бот ПЕРЕДАЁТ комнату следующей встрече (исход next_meeting).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import stats
from app.automation import scheduler as sched_mod
from app.automation.scheduler import MeetingState, Scheduler

URL = "https://telemost.yandex.ru/j/78283935125180"


@pytest.fixture
def sched(monkeypatch):
    monkeypatch.setenv("VTX_RECORDER_ENABLED", "1")
    outcomes = []
    monkeypatch.setattr(stats, "record_meeting", lambda st, o: outcomes.append((st.task_id, o)))
    monkeypatch.setattr(sched_mod.snapshots, "save", lambda st: None)
    monkeypatch.setattr(sched_mod.auto_settings, "load", lambda u: {"lookahead_min": 2})
    s = Scheduler()
    s._boot_time = 0
    s.outcomes = outcomes
    s.launched = []
    monkeypatch.setattr(sched_mod.recorder, "acquire_slot",
                        lambda: s.launched.append(1) or object())
    monkeypatch.setattr(sched_mod.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda self: None})())
    return s


def _pair(s, start_offset_min: float, busy_task="100", next_task="200"):
    now = datetime.now(timezone.utc)
    busy = MeetingState(key=f"t:{busy_task}:old", task_id=busy_task, title="ОД редизайн",
                        url=URL, start=now - timedelta(minutes=60), owner="t",
                        state="recording")
    nxt = MeetingState(key=f"t:{next_task}:new", task_id=next_task, title="ЭМО Встреча",
                       url=URL, start=now + timedelta(minutes=start_offset_min),
                       owner="t", state="scheduled")
    with s._lock:
        s._states[busy.key] = busy
        s._states[nxt.key] = nxt
    return busy, nxt


class TestОжиданиеКомнаты:
    def test_до_начала_карточка_ждёт_а_не_пропускается(self, sched):
        busy, nxt = _pair(sched, start_offset_min=1.5)
        sched._maybe_trigger("t", {"lookahead_min": 2})
        assert nxt.state == "scheduled"
        assert "Ждём" in nxt.detail and "ОД редизайн" in nxt.detail
        assert busy.stop_flag is False and busy.handoff_to is None
        assert sched.launched == [] and sched.outcomes == []

    def test_та_же_задача_по_прежнему_дубль(self, sched):
        busy, nxt = _pair(sched, start_offset_min=0, busy_task="100", next_task="100")
        sched._maybe_trigger("t", {"lookahead_min": 2})
        assert nxt.state == "skipped" and "уже в этом звонке" in nxt.detail
        assert busy.stop_flag is False
        assert sched.outcomes == [("100", "skipped")]

    def test_после_освобождения_комнаты_бот_идёт_сам(self, sched):
        busy, nxt = _pair(sched, start_offset_min=1)
        sched._maybe_trigger("t", {"lookahead_min": 2})
        assert nxt.state == "scheduled"
        busy.state = "uploading"                     # запись закончилась
        sched._maybe_trigger("t", {"lookahead_min": 2})
        assert nxt.state == "recording" and sched.launched == [1]


class TestПередачаКомнаты:
    def test_в_час_начала_текущей_записи_ставится_флаг_остановки(self, sched):
        busy, nxt = _pair(sched, start_offset_min=-0.1)
        sched._maybe_trigger("t", {"lookahead_min": 2})
        assert busy.stop_flag is True and busy.handoff_to == "ЭМО Встреча"
        assert any("передаю комнату" in m for m in busy.logs)
        assert nxt.state == "scheduled" and "прошу бота закончить" in nxt.detail
        assert sched.launched == [] and sched.outcomes == []

    def test_флаг_ставится_один_раз_и_лог_не_дублируется(self, sched):
        busy, nxt = _pair(sched, start_offset_min=-0.1)
        for _ in range(3):
            sched._maybe_trigger("t", {"lookahead_min": 2})
        assert sum("передаю комнату" in m for m in busy.logs) == 1

    def test_запись_по_ссылке_тоже_уступает_расписанию(self, sched):
        busy, nxt = _pair(sched, start_offset_min=-0.1, busy_task="link-20261005-ab12")
        sched._maybe_trigger("t", {"lookahead_min": 2})
        assert busy.stop_flag is True and busy.handoff_to == "ЭМО Встреча"

    def test_причина_остановки_next_meeting_а_не_вручную(self):
        st = MeetingState(key="k", task_id="1", title="x", url=URL, start=None,
                          handoff_to="ЭМО Встреча")
        assert Scheduler._effective_stop_reason(st, "stopped") == "next_meeting"
        assert Scheduler._effective_stop_reason(st, "silence") == "silence"
        plain = MeetingState(key="k2", task_id="2", title="y", url=URL, start=None)
        assert Scheduler._effective_stop_reason(plain, "stopped") == "stopped"
        assert "следующей встрече" in stats.stop_reason_ru("next_meeting")

    def test_комната_не_освободилась_за_окно_опоздания(self, sched):
        busy, nxt = _pair(sched, start_offset_min=-11)
        sched._maybe_trigger("t", {"lookahead_min": 2})
        assert nxt.state == "missed"
        assert "занята записью «ОД редизайн»" in nxt.detail
        assert sched.outcomes == [("200", "missed")]
