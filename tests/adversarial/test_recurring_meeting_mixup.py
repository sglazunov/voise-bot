"""Атака: у повторяющейся встречи task_id один, а карточек несколько.

Ключ карточки — `f"{user}:{task_id}:{start}"` (scheduler.py:224), то есть у
еженедельной планёрки в памяти живут карточки за КАЖДОЕ прошедшее и будущее
время. При этом `Scheduler._find_state` (scheduler.py:1091) ищет ТОЛЬКО по
`task_id` и берёт ПЕРВОЕ совпадение в порядке словаря — то есть самую раннюю
из известных карточек.

Через `_find_state` работают три пользовательских действия:
  * `POST /api/automation/meetings/{task_id}/notes` — заметки участника,
    «скелет протокола» (Д6);
  * `GET  /api/automation/meetings/{task_id}/notes`;
  * `GET  /api/automation/meetings/{task_id}/live` — живая расшифровка (Д10).

Итог: человек пишет заметки во время СЕГОДНЯШНЕЙ встречи, а они приклеиваются
к карточке ПРОШЛОЙ — и в протокол сегодняшней не попадают. В окне «живая
расшифровка» по той же причине показывается текст прошлой встречи.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.automation.scheduler import MeetingState, scheduler


TASK = 777


@pytest.fixture
def two_occurrences():
    scheduler._states.clear()
    now = datetime.now(timezone.utc)
    old = MeetingState(key=f"alice:{TASK}:{(now - timedelta(days=7)).isoformat()}",
                       task_id=TASK, title="Планёрка",
                       url="https://telemost.yandex.ru/j/room1",
                       start=now - timedelta(days=7), owner="alice",
                       state="done", detail="Готово (прошлая неделя).",
                       job_id="oldjob00001")
    live = MeetingState(key=f"alice:{TASK}:{now.isoformat()}",
                        task_id=TASK, title="Планёрка",
                        url="https://telemost.yandex.ru/j/room1",
                        start=now, owner="alice", state="recording",
                        detail="Идёт запись…")
    scheduler._states[old.key] = old        # прошлая карточка появилась раньше
    scheduler._states[live.key] = live
    yield old, live
    scheduler._states.clear()


def test_notes_land_on_the_meeting_being_recorded(two_occurrences):
    old, live = two_occurrences

    scheduler.set_meeting_notes("alice", TASK, "решили переносить релиз на 25-е")

    assert live.live_notes == "решили переносить релиз на 25-е", (
        "заметки не попали в идущую встречу")
    assert old.live_notes == "", (
        f"заметки приклеились к прошлой карточке: {old.live_notes!r}")


def test_live_view_shows_the_meeting_being_recorded(two_occurrences):
    old, live = two_occurrences
    live.live_text = "[00:10] сегодняшний разговор"
    old.live_text = "[00:10] разговор недельной давности"

    res = scheduler.live_view("alice", TASK)

    assert res["text"] == "[00:10] сегодняшний разговор", (
        f"окно живой расшифровки показывает чужую встречу: {res['text']!r}")
