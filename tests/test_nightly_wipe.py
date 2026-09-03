"""Ночной проход по полям Weeek: страховка, а не основной механизм.

Унаследованные ссылки снимает опрос (см. tests/test_future_links.py) — ночной
проход остаётся для задач, до которых опрос не добрался. Здесь проверяется
именно он: окно, отбор задач и отметка «за сегодня уже чистили».

ВНИМАНИЕ: у фикстур обязан быть атрибут `start`. Раньше его не было, и весь
отбор по дате не покрывался ни одним тестом — переписать фильтр можно было
как угодно, тесты оставались зелёными.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.automation import scheduler as sched_mod
from app.automation.scheduler import scheduler


def _meeting(task_id, start, fields=None, title="Планёрка", **raw):
    """Встреча так, как её отдаёт опрос Weeek: с датой и сырой задачей."""
    task = {"id": task_id, "title": title,
            "customFields": [{"id": f"f{i}", "name": n, "value": v}
                             for i, (n, v) in enumerate(
                                 (fields or {"Видео встречи": "https://old/v"}
                                  ).items())]}
    task.update(raw)
    return SimpleNamespace(task_id=task_id, title=title, start=start,
                           url="https://telemost.yandex.ru/j/abc", raw=task)


@pytest.fixture
def stub(monkeypatch):
    """Часы, Weeek и чистка — под контролем теста."""
    state = {"wiped": [], "saved": {}, "now": None, "meetings": None}

    monkeypatch.setattr(sched_mod.weeek, "upcoming_meetings",
                        lambda *a, **k: state["meetings"])
    monkeypatch.setattr(sched_mod.delivery, "wipe_stale_links",
                        lambda task_id, cfg, log, task=None: (
                            state["wiped"].append(task_id)
                            or {"ok": True, "cleared": ["Видео встречи"],
                                "errors": []}))
    monkeypatch.setattr(sched_mod.auto_settings, "save",
                        lambda user, values: state["saved"].update(values))
    monkeypatch.setattr(sched_mod.snapshots, "load", lambda user: {})
    scheduler._states.clear()

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return state["now"].astimezone(tz) if tz else state["now"]

    monkeypatch.setattr(sched_mod, "datetime", _DT)
    # По умолчанию — две сегодняшние встречи впереди (ночь, они днём).
    state["meetings"] = [_meeting(1, _at(11)), _meeting(2, _at(15))]
    return state


def _at(hour: int, minute: int = 30, day: int = 1) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=timezone.utc)


CFG = {"weeek_token": "tok", "timezone": "UTC",
       "weeek_video_field": "Видео встречи",
       "weeek_protocol_field": "Протокол встречи"}


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


def test_ночью_чистит_сегодняшние_встречи(stub):
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


def test_задача_без_даты_не_чистится(stub):
    """Т8. Задача без даты — не встреча в расписании, а карточка, куда ссылку
    чаще всего прикрепляют руками. Раньше её чистило каждую ночь, и
    прикреплённое исчезало снова и снова."""
    stub["now"] = _at(4)
    stub["meetings"] = [_meeting(7, None)]
    scheduler._nightly_wipe("alice", dict(CFG))
    assert stub["wiped"] == []


def test_чужой_день_не_трогается(stub):
    """Ночной проход — только по сегодняшним: разовая встреча следующей недели
    теряла прикреплённые ссылки просто потому, что попалась в выдаче."""
    stub["now"] = _at(4)
    stub["meetings"] = [_meeting(3, _at(11, day=8))]
    scheduler._nightly_wipe("alice", dict(CFG))
    assert stub["wiped"] == []


def test_сутки_считаются_по_часовому_поясу_команды(stub):
    """Т13. Встреча в 00:25 по Москве относится к СВОИМ суткам, а не к
    предыдущим: по UTC это ещё вчерашний вечер, и проход 1 сентября счёл бы её
    сегодняшней."""
    stub["now"] = _at(1, 30)                      # 04:30 по Москве
    stub["meetings"] = [_meeting(4, _at(21, 25))]  # 00:25 второго по Москве
    scheduler._nightly_wipe("alice", {**CFG, "timezone": "Europe/Moscow"})
    assert stub["wiped"] == []


def test_прошедшая_сегодня_встреча_не_чистится(stub):
    """Встреча, прошедшая ночью и записанная под утро, теряла в 04:00 свежие
    ссылки: дата у неё сегодняшняя. Прошедшее не трогаем вовсе."""
    stub["now"] = _at(4)
    stub["meetings"] = [_meeting(5, _at(0, 25))]
    scheduler._nightly_wipe("alice", dict(CFG))
    assert stub["wiped"] == []


def test_сбой_одной_задачи_не_рвёт_проход(stub, monkeypatch):
    """Одна недоступная задача не должна оставить остальные с прошлыми ссылками.

    Т19: отметка «за сегодня чистили» при этом НЕ ставится — иначе половина
    задач ждала бы следующей ночи.
    """
    def flaky(task_id, cfg, log, task=None):
        if task_id == 1:
            return {"ok": False, "cleared": [], "errors": ["Weeek 500"]}
        stub["wiped"].append(task_id)
        return {"ok": True, "cleared": ["Видео встречи"], "errors": []}

    monkeypatch.setattr(sched_mod.delivery, "wipe_stale_links", flaky)
    stub["now"] = _at(4)
    scheduler._nightly_wipe("alice", dict(CFG))
    assert stub["wiped"] == [2]
    assert "last_field_wipe" not in stub["saved"]


def test_исключение_в_задаче_не_рвёт_проход(stub, monkeypatch):
    """Тот же сюжет, но сбой прилетает исключением из глубины клиента (Т20)."""
    def boom(task_id, cfg, log, task=None):
        if task_id == 1:
            raise RuntimeError("Weeek 500")
        stub["wiped"].append(task_id)
        return {"ok": True, "cleared": ["Видео встречи"], "errors": []}

    monkeypatch.setattr(sched_mod.delivery, "wipe_stale_links", boom)
    stub["now"] = _at(4)
    scheduler._nightly_wipe("alice", dict(CFG))
    assert stub["wiped"] == [2]
    assert "last_field_wipe" not in stub["saved"]


def test_старт_записи_поля_не_трогает():
    """Чистка ушла из _run: иначе она сработала бы дважды за день."""
    import inspect
    src = inspect.getsource(scheduler._run)
    assert "wipe_stale_links" not in src


def test_нижняя_граница_суток_не_съедает_вечернюю_встречу(stub):
    """Вечерняя сегодняшняя встреча — та самая, ради которой проход и нужен."""
    stub["now"] = _at(4)
    stub["meetings"] = [_meeting(6, _at(4) + timedelta(hours=15))]
    scheduler._nightly_wipe("alice", dict(CFG))
    assert stub["wiped"] == [6]
