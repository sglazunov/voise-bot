"""Атака: кнопка «Подключиться» на карточке со СЛОМАННЫМ протоколом
перезаписывает уже сделанную запись встречи.

Как складывается:
  1. Бот записал встречу, файл лежит в `data/users/<команда>/recordings/
     «17.09.2026, 10:00. - Планёрка.mp4»`, задача распознавания создана.
  2. Протокол не собрался (движок отказал) — `_await_and_upload_protocol`
     ставит карточке `state="error"` с текстом «…«Пересобрать» допоставит его
     в Weeek. Запись в облаке» (scheduler.py:711). Ровно так же карточка
     краснеет, если упало САМО распознавание: `_enrich_from_job` переводит
     её в `error` по статусу задачи (scheduler.py:775).
  3. Фронтенд для состояния `error` показывает кнопку «Подключиться»
     (frontend/src/pages/Meetings.tsx:14 — `JOINABLE = [... "error"]`).
  4. `Scheduler.run_now` (scheduler.py:1124) проверяет ТОЛЬКО «уже
     записывается» и «эта же комната занята». Про то, что запись у встречи
     уже есть, он не знает — и запускает `_run` заново.
  5. `_run` собирает имя файла из `st.start` и названия задачи (scheduler.py:
     446-455) — оно ДЕТЕРМИНИРОВАНО, то есть совпадает с прежним. ffmpeg
     открывает тот же путь на запись.

Итог: единственный локальный экземпляр записи затирается, а вместе с ним
пропадает возможность «Повторить» распознавание. Человек нажал кнопку, которую
ему предложил интерфейс.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from app import security
from app.automation import scheduler as sched_mod
from app.automation.scheduler import MeetingState, scheduler


TASK = 4242


@pytest.fixture
def recorded_but_failed(tmp_path):
    scheduler._states.clear()
    start = datetime(2026, 9, 17, 7, 0, tzinfo=timezone.utc)
    rec_dir = security.user_dir("alice") / "recordings"
    rec_dir.mkdir(parents=True, exist_ok=True)
    out = rec_dir / "17.09.2026, 10:00. - Планёрка.mp4"
    out.write_bytes(b"\x00" * (3 * 1024 * 1024))     # час встречи
    st = MeetingState(key=f"alice:{TASK}:{start.isoformat()}", task_id=TASK,
                      title="Планёрка", url="https://telemost.yandex.ru/j/room1",
                      start=start, owner="alice", state="error",
                      detail="Протокол не собрался — job abc. «Пересобрать» "
                             "допоставит его в Weeek.",
                      job_id="abc123456789", out_path=str(out))
    scheduler._states[st.key] = st
    yield st, out
    scheduler._states.clear()


def test_run_now_lets_the_bot_rejoin_without_touching_the_recording(
        recorded_but_failed, monkeypatch):
    """21.09.2026: владелец — «должна быть возможность подключиться, даже
    если бот заходил» (бот выпал из звонка, встреча идёт). Повторный заход
    разрешён; прежняя запись цела, потому что путь новой части берётся
    через `_free_path` (второй тест), а карточка об этом предупреждает."""
    st, out = recorded_but_failed
    before = out.read_bytes()
    launched = []
    monkeypatch.setattr(sched_mod.recorder, "acquire_slot", lambda: "slot0")
    monkeypatch.setattr(sched_mod.recorder, "release_slot", lambda s: None)
    monkeypatch.setattr(type(scheduler), "_run",
                        lambda self, st, slot, manual=False: launched.append(st))

    res = scheduler.run_now("alice", TASK)

    assert res.get("ok") is True, res
    assert launched and launched[0] is st and st.state == "recording"
    assert out.read_bytes() == before, "прежняя запись должна остаться нетронутой"
    assert any("прежняя запись" in ln and out.name in ln for ln in st.logs)


def test_recording_path_never_reuses_an_existing_file(recorded_but_failed):
    """Вторая линия защиты: даже если запись всё же началась, писать она обязана
    в НОВЫЙ файл.

    Отказ `run_now` закрывает путь через кнопку, но имя записи складывается из
    даты, времени и названия задачи — оно строго определено. Значит, совпасть
    может и по другим причинам: две встречи с одинаковым названием и временем,
    восстановление после перезапуска. Старая запись — единственный исходник и
    для выгрузки, и для «Повторить», поэтому переписывать её нельзя никогда.
    """
    _st, out = recorded_but_failed

    free = sched_mod._free_path(out)
    assert free != out, (
        f"для занятого пути «{out.name}» выдан он же — ffmpeg открыл бы его на "
        "запись и затёр готовую запись встречи")
    assert not free.exists() and free.parent == out.parent
    assert free.suffix == out.suffix, "расширение обязано сохраниться"

    # …и свободный путь остаётся собой: лишних суффиксов не появляется.
    fresh = out.parent / "нет такого файла.mp4"
    assert sched_mod._free_path(fresh) == fresh
