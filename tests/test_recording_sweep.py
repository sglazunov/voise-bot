"""Уборка записей встреч с диска (config.RECORDING_RETENTION_DAYS).

Записи уходят в облако, а локальная копия оставалась навсегда при любой
осечке (облако не приняло, протокол не собрался, повторный заход) — диск
60 ГБ забился за два месяца. Здесь: старое удаляется вместе с побочными
файлами, свежее и занятое — нет, запись без ссылки в облаке удаляется с
предупреждением в лог.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest

from app import config, security
from app.automation import scheduler as sched_mod
from app.automation.scheduler import MeetingState, scheduler

OLD = 3 * 86400          # трое суток назад
NOW = time.time()


def _touch(path: Path, age_sec: float, size: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (NOW - age_sec, NOW - age_sec))
    return path


@pytest.fixture
def team(monkeypatch):
    monkeypatch.setattr(security, "list_teams", lambda: ["admin"])
    rec = security.user_dir("admin") / "recordings"
    rec.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "RECORDING_RETENTION_DAYS", 2)
    with scheduler._lock:
        scheduler._states.clear()
    yield rec
    with scheduler._lock:
        scheduler._states.clear()


def test_старые_записи_и_побочные_файлы_удаляются(team):
    old = _touch(team / "2026-09-20 10-00 Планёрка.mp4", OLD, size=2_000_000)
    for suf in (".ffmpeg.log", ".live.json", ".live.wav", ".tail.wav",
                ".join-failed.png", ".join-failed.html"):
        _touch(Path(str(old) + suf), OLD)
    fresh = _touch(team / "2026-10-02 09-00 Свежая.mp4", 3600)
    res = scheduler.sweep_recordings(now=NOW)
    assert not old.exists()
    assert not list(team.glob("2026-09-20*"))
    assert fresh.exists()
    assert res["deleted"] == 7 and res["kept"] == 1
    assert res["freed_mb"] > 0


def test_идущая_запись_и_исходник_незавершённой_задачи_не_трогаются(team, monkeypatch):
    recording = _touch(team / "2026-09-20 10-00 Идёт.mp4", OLD)
    _touch(Path(str(recording) + ".live.json"), OLD)
    st = MeetingState(key="k", task_id="t", title="Идёт", url="u", start=None, owner="admin",
                      state="recording", out_path=str(recording))
    with scheduler._lock:
        scheduler._states["k"] = st
    queued = _touch(team / "2026-09-20 11-00 Очередь.mp4", OLD)
    job = type("J", (), {})()
    job.status, job.audio_path = "queued", str(queued)
    monkeypatch.setattr(sched_mod.store, "list", lambda owner=None: [job])
    res = scheduler.sweep_recordings(now=NOW)
    assert recording.exists() and Path(str(recording) + ".live.json").exists()
    assert queued.exists()
    assert res["deleted"] == 0 and res["kept"] == 3


def test_запись_без_ссылки_в_облаке_удаляется_с_предупреждением(team, caplog):
    unclouded = _touch(team / "2026-09-20 10-00 Без облака.mp4", OLD)
    clouded = _touch(team / "2026-09-20 12-00 В облаке.mp4", OLD)
    with scheduler._lock:
        scheduler._states["c"] = MeetingState(
            key="c", task_id="t2", title="В облаке", url="u", start=None, owner="admin", state="done",
            out_path=str(clouded), cloud_url="https://disk.yandex.ru/d/x")
    with caplog.at_level(logging.WARNING, logger="vtx.scheduler"):
        scheduler.sweep_recordings(now=NOW)
    assert not unclouded.exists() and not clouded.exists()
    warned = [r.getMessage() for r in caplog.records if "БЕЗ ссылки" in r.getMessage()]
    assert len(warned) == 1 and "Без облака" in warned[0]


def test_ноль_дней_выключает_уборку(team, monkeypatch):
    old = _touch(team / "2026-09-20 10-00 Планёрка.mp4", OLD)
    monkeypatch.setattr(config, "RECORDING_RETENTION_DAYS", 0)
    assert scheduler.sweep_recordings(now=NOW) == {"deleted": 0, "freed_mb": 0.0, "kept": 0}
    assert old.exists()


def test_папки_и_чужие_каталоги_не_трогаются(team):
    sub = team / "подпапка"
    sub.mkdir()
    _touch(sub / "старое.mp4", OLD)
    other = _touch(security.user_dir("admin") / "uploads" / "старое.mp4", OLD)
    scheduler.sweep_recordings(now=NOW)
    assert (sub / "старое.mp4").exists() and other.exists()


def test_цикл_зовёт_уборку_раз_в_час(monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler, "sweep_recordings", lambda: calls.append(1))
    monkeypatch.setattr(security, "list_teams", lambda: [])
    scheduler._last_sweep = 0.0
    monkeypatch.setattr(scheduler._stop, "wait", lambda *_: scheduler._stop.set())
    scheduler._stop.clear()
    try:
        scheduler._loop()
        scheduler._stop.clear()
        scheduler._loop()
    finally:
        scheduler._stop.clear()
    assert calls == [1]
