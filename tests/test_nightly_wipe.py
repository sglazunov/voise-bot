"""Поля Weeek чистятся ночью, а не в момент старта записи.

Повторяющаяся задача Weeek — ОДНА задача, и её поля «Видео встречи» и «Протокол
встречи» несут ссылки прошлого проведения. Чистить их надо, иначе вчерашнее
видео выглядит как сегодняшнее.

Раньше это делалось при старте записи — и прошлые ссылки исчезали ровно тогда,
когда встреча начиналась, то есть в момент, когда к ним чаще всего и
обращаются («что решили в прошлый раз»). Ночной проход оставляет весь рабочий
день на прошлые ссылки и убирает их задолго до новой встречи.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.automation import scheduler as sched_mod
from app.automation.scheduler import scheduler


@pytest.fixture
def stub(monkeypatch):
    """Часы, Weeek и чистка — под контролем теста."""
    state = {"wiped": [], "saved": {}, "now": None}

    monkeypatch.setattr(sched_mod.weeek, "upcoming_meetings",
                        lambda *a, **k: [SimpleNamespace(task_id=1),
                                         SimpleNamespace(task_id=2)])
    monkeypatch.setattr(sched_mod.delivery, "wipe_stale_links",
                        lambda task_id, cfg, log: state["wiped"].append(task_id))
    monkeypatch.setattr(sched_mod.auto_settings, "save",
                        lambda user, values: state["saved"].update(values))

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return state["now"].astimezone(tz) if tz else state["now"]

    monkeypatch.setattr(sched_mod, "datetime", _DT)
    return state


def _at(hour: int) -> datetime:
    return datetime(2026, 9, 1, hour, 30, tzinfo=timezone.utc)


CFG = {"weeek_token": "tok", "timezone": "UTC"}


def test_днём_не_чистит(stub):
    """Днём поля трогать нельзя: ссылки на прошлую встречу ещё нужны.

    Одной отметки «сегодня уже чистили» мало: сервис, запущенный впервые днём,
    её не имеет — и стёр бы ссылки, записанные утренней встречей.
    """
    stub["now"] = _at(12)
    scheduler._nightly_wipe("alice", dict(CFG))
    assert stub["wiped"] == []


def test_окно_закончилось_ждём_следующей_ночи(stub):
    """Пропустили окно (сервис был выключен) — чистить днём хуже, чем не
    почистить вовсе."""
    stub["now"] = _at(7)
    scheduler._nightly_wipe("alice", dict(CFG))
    assert stub["wiped"] == []


def test_до_окна_не_чистит(stub):
    stub["now"] = _at(2)
    scheduler._nightly_wipe("alice", dict(CFG))
    assert stub["wiped"] == []


def test_ночью_чистит_все_встречи(stub):
    stub["now"] = _at(4)
    scheduler._nightly_wipe("alice", dict(CFG))
    assert stub["wiped"] == [1, 2]
    assert stub["saved"]["last_field_wipe"] == "2026-09-01"


def test_второй_раз_за_сутки_не_чистит(stub):
    """Перезапуск внутри самого окна не должен чистить дважды."""
    stub["now"] = _at(5)
    scheduler._nightly_wipe("alice", {**CFG, "last_field_wipe": "2026-09-01"})
    assert stub["wiped"] == []


def test_без_токена_молчит(stub):
    stub["now"] = _at(4)
    scheduler._nightly_wipe("alice", {"timezone": "UTC"})
    assert stub["wiped"] == [] and not stub["saved"]


def test_сбой_одной_задачи_не_рвёт_проход(stub, monkeypatch):
    """Одна недоступная задача не должна оставить остальные с прошлыми ссылками."""
    def flaky(task_id, cfg, log):
        if task_id == 1:
            raise RuntimeError("Weeek 500")
        stub["wiped"].append(task_id)

    monkeypatch.setattr(sched_mod.delivery, "wipe_stale_links", flaky)
    stub["now"] = _at(4)
    scheduler._nightly_wipe("alice", dict(CFG))
    assert stub["wiped"] == [2]
    assert stub["saved"]["last_field_wipe"] == "2026-09-01"


def test_старт_записи_поля_не_трогает():
    """Чистка ушла из _run: иначе она сработала бы дважды за день."""
    import inspect
    src = inspect.getsource(scheduler._run)
    assert "wipe_stale_links" not in src
