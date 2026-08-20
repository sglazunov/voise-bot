"""Атака на затенённый логгер: обработчик ошибки сам падает.

Тот же дефект, что и в рекордере, живёт ещё в двух местах. Имя `log` в этих
модулях — модульный логгер, но внутри функций оно перекрыто ЛОКАЛЬНЫМ `log`
(параметром или вложенной функцией), а в `except` вызывается `log.info` /
`log.warning`:

  * app/automation/delivery.py:36   — `wipe_stale_links(task_id, cfg, log)`;
  * app/automation/scheduler.py:674 — `report()` внутри
    `_await_and_upload_protocol`.

Оба вызова стоят в «косметических» ветках, поэтому AttributeError выглядит
безобидно — но он вылетает из фонового потока и обрывает всё, что стояло за ним.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.automation import delivery, scheduler as sched_mod
from app.automation.scheduler import MeetingState, scheduler


class TestWipeStaleLinks:
    def test_weeek_failure_does_not_kill_the_wiper(self, monkeypatch):
        """Weeek отдал 500 при очистке поля. Функция помечена как
        «cosmetic step — never blocks the recording», значит обязана проглотить
        сбой и вернуть управление."""
        def boom(*a, **kw):
            raise RuntimeError("Weeek 500")

        monkeypatch.setattr(delivery.weeek, "set_custom_field", boom)
        cfg = {"weeek_token": "wk", "weeek_video_field": "Видео встречи",
               "weeek_protocol_field": "Протокол встречи"}
        lines: list[str] = []

        delivery.wipe_stale_links(12345, cfg, lines.append)   # не должно бросать


class TestAwaitProtocolReport:
    def test_report_survives_a_weeek_outage(self, monkeypatch):
        """Задача распознавания исчезла (ретеншн/перезапуск) — ожидатель должен
        объяснить это комментарием и ЗАКРЫТЬ карточку встречи. Если Weeek в
        этот момент недоступен, поток обязан доработать, а не умереть."""
        def boom(*a, **kw):
            raise RuntimeError("Weeek недоступен")

        monkeypatch.setattr(sched_mod.weeek, "add_comment", boom)
        st = MeetingState(key="alice:777:2026-09-17T10:00:00+00:00",
                          task_id=777, title="Планёрка",
                          url="https://telemost.yandex.ru/j/abc",
                          start=datetime.now(timezone.utc), owner="alice",
                          state="transcribing")
        cfg = {"weeek_token": "wk", "post_back_to_weeek": True}

        scheduler._await_and_upload_protocol(st, "job-that-vanished", cfg)

        assert st.logs, "в лог карточки ничего не попало"
