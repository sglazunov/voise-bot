"""Регрессия по ревью 05.10.2026 правок «живая расшифровка + два воркера»,
«уборка записей» и «передача комнаты». По тесту на каждую находку."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import jobs, stats
from app.automation import scheduler as sched_mod
from app.automation.scheduler import MeetingState, Scheduler
from app.transcribe import Segment

URL = "https://telemost.yandex.ru/j/78283935125180"


# --------------------------------------------------------------------------- #
# Передача комнаты не режет ту же встречу
# --------------------------------------------------------------------------- #
@pytest.fixture
def sched(monkeypatch):
    monkeypatch.setenv("VTX_RECORDER_ENABLED", "1")
    outcomes = []
    monkeypatch.setattr(stats, "record_meeting", lambda st, o: outcomes.append((st.task_id, o)))
    monkeypatch.setattr(sched_mod.snapshots, "save", lambda st: None)
    monkeypatch.setattr(sched_mod.auto_settings, "load", lambda u: {"lookahead_min": 2})
    monkeypatch.setattr(sched_mod.recorder, "acquire_slot", lambda: object())
    s = Scheduler()
    s._boot_time = 0
    s.outcomes = outcomes
    return s


def _busy_and_next(s, busy_started_min_ago: float, next_in_min: float,
                   busy_task="link-20261005-ab12"):
    now = datetime.now(timezone.utc)
    busy = MeetingState(key="t:b", task_id=busy_task, title="Встреча по ссылке",
                        url=URL, start=now - timedelta(minutes=busy_started_min_ago),
                        owner="t", state="recording")
    nxt = MeetingState(key="t:n", task_id="200", title="ЭМО Встреча", url=URL,
                       start=now + timedelta(minutes=next_in_min), owner="t",
                       state="scheduled")
    with s._lock:
        s._states[busy.key] = busy
        s._states[nxt.key] = nxt
    return busy, nxt


class TestПередачаТолькоПредыдущейВстрече:
    def test_бот_по_ссылке_за_две_минуты_до_начала_не_режется(self, sched):
        # Владелец отправил бота по ссылке в 10:58 на встречу 11:00.
        busy, nxt = _busy_and_next(sched, busy_started_min_ago=2.1, next_in_min=-0.1)
        sched._maybe_trigger("t", {"lookahead_min": 2})
        assert busy.stop_flag is False and busy.handoff_to is None
        assert nxt.state == "skipped" and "уже в этом звонке" in nxt.detail

    def test_две_задачи_на_один_звонок_с_разницей_в_минуту(self, sched):
        busy, nxt = _busy_and_next(sched, busy_started_min_ago=1.0, next_in_min=-0.05,
                                   busy_task="100")
        sched._maybe_trigger("t", {"lookahead_min": 2})
        assert busy.stop_flag is False
        assert nxt.state == "skipped"

    def test_встреча_часом_раньше_по_прежнему_передаёт_комнату(self, sched):
        busy, nxt = _busy_and_next(sched, busy_started_min_ago=60, next_in_min=-0.1)
        sched._maybe_trigger("t", {"lookahead_min": 2})
        assert busy.stop_flag is True and busy.handoff_to == "ЭМО Встреча"
        assert nxt.state == "scheduled"


# --------------------------------------------------------------------------- #
# Побочные файлы записи: один список, привязка к записи
# --------------------------------------------------------------------------- #
class TestПобочныеФайлы:
    def _make(self, tmp_path):
        mp4 = tmp_path / "05.10.2026, 11 00. - ЭМО.mp4"
        names = [f"{mp4.name}.ffmpeg.log", f"{mp4.name}.live.json",
                 f"{mp4.name}.live.json.tmp", f"{mp4.name}.live.wav",
                 f"{mp4.name}.tail.wav", f"{mp4.stem}.16k.wav",
                 f"{mp4.stem}.join-failed.png", f"{mp4.stem}.join-failed.html",
                 f"{mp4.stem}.join-failed.frame1.html"]
        for n in [mp4.name] + names:
            (tmp_path / n).write_bytes(b"x")
        return mp4, names

    def test_drop_sidecars_убирает_всё_в_том_числе_with_suffix(self, tmp_path):
        mp4, names = self._make(tmp_path)
        jobs.drop_sidecars(str(mp4), with_media=True)
        assert list(tmp_path.iterdir()) == []

    def test_без_флага_видео_остаётся(self, tmp_path):
        mp4, _ = self._make(tmp_path)
        jobs.drop_sidecars(str(mp4))
        assert [p.name for p in tmp_path.iterdir()] == [mp4.name]

    def test_уборка_привязывает_оба_вида_имён_к_записи(self, tmp_path):
        mp4, names = self._make(tmp_path)
        for n in names:
            assert Scheduler._recording_base(tmp_path / n) == str(mp4), n
        assert Scheduler._recording_base(mp4) == str(mp4)

    def test_новая_запись_по_старому_пути_не_берёт_чужую_живую_расшифровку(
            self, tmp_path, monkeypatch):
        # Видео прошлой части удалили, файл живой расшифровки остался.
        rec_dir = tmp_path / "users" / "t" / "recordings"
        rec_dir.mkdir(parents=True)
        monkeypatch.setattr(sched_mod.security, "user_dir", lambda u: tmp_path / "users" / u)
        monkeypatch.setattr(sched_mod.auto_settings, "load",
                            lambda u: {"live_transcribe": False, "do_transcribe": False})
        monkeypatch.setattr(sched_mod.snapshots, "save", lambda st: None)
        seen = {}

        def fake_record(url, out, cfg, **kw):
            seen["live_left"] = Path(f"{out}.live.json").exists()
            return {"ok": False, "error": "стоп теста", "reason": "error"}

        monkeypatch.setattr(sched_mod.recorder, "record_meeting", fake_record)
        monkeypatch.setattr(sched_mod.recorder, "release_slot", lambda s: None)
        monkeypatch.setattr(stats, "record_meeting", lambda st, o: None)
        st = MeetingState(key="t:1", task_id="1", title="ЭМО", url=URL,
                          start=datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc), owner="t")
        s = Scheduler()
        s._tz = lambda cfg: timezone.utc
        stale = rec_dir / "05.10.2026, 08:00. - ЭМО.mp4.live.json"
        stale.write_text('{"until": 300, "segments": [{"start":1,"end":2,"text":"чужое"}]}')
        s._run(st, slot=None)
        assert seen["live_left"] is False


# --------------------------------------------------------------------------- #
# Файл живой расшифровки и учёт времени
# --------------------------------------------------------------------------- #
class TestЖиваяРасшифровка:
    @pytest.fixture
    def rec(self, tmp_path, monkeypatch):
        media = tmp_path / "встреча.mp4"
        media.write_bytes(b"\x00" * 1024)
        live = tmp_path / "встреча.mp4.live.json"
        live.write_text(json.dumps({"until": 120.0, "sec": 95.0, "segments": [
            {"start": 1.0, "end": 4.0, "text": "начало встречи"}]}, ensure_ascii=False),
            encoding="utf-8")
        progress = []

        def fake_transcribe(path, language=None, on_segment=None, on_start=None,
                            initial_prompt=None, model_name=None, nonblocking=False,
                            offset_sec=0.0):
            if on_start:
                on_start()
            seg = Segment(start=offset_sec + 5.0, end=offset_sec + 10.0, text="хвост")
            if on_segment:
                on_segment(seg, 180.0)
            return [seg], {"language": "ru", "duration": 180.0, "model": "medium"}

        monkeypatch.setattr(jobs, "transcribe_file", fake_transcribe)
        monkeypatch.setattr(jobs, "_media_duration", lambda p, timeout=60: 180.0)
        tail = tmp_path / "встреча.mp4.tail.wav"
        tail.write_bytes(b"RIFF")
        monkeypatch.setattr(jobs, "_cut_wav", lambda p, start, *a, **k: str(tail))
        real_set = jobs.store._set

        def spy(job, persist=True, **kw):
            if "progress" in kw:
                progress.append(kw["progress"])
            return real_set(job, persist=persist, **kw)

        monkeypatch.setattr(jobs.store, "_set", spy)
        return media, live, progress

    def _run(self, media, live):
        job = jobs.store.create(filename=media.name, audio_path=str(media),
                                language="ru", diarize=False, owner="alice",
                                live_path=str(live))
        jobs.store._queue.get(timeout=5)
        jobs.store._process(job)
        return job

    def test_время_живых_кусков_входит_в_transcribe_sec(self, rec):
        media, live, _ = rec
        job = self._run(media, live)
        assert job.status == jobs.STATUS_DONE, job.error
        assert job.transcribe_sec >= 95.0

    def test_прогресс_не_откатывается_к_одному_проценту(self, rec):
        media, live, progress = rec
        self._run(media, live)
        start = progress.index(next(p for p in progress if p and p > 0.5))
        assert all(p >= progress[start] for p in progress[start:] if p is not None)

    def test_запись_файла_атомарна(self, tmp_path, monkeypatch):
        # persist пишет во временный файл и подменяет — читатель никогда не
        # видит полузаписанный JSON.
        import inspect
        src = inspect.getsource(Scheduler._live_transcribe_loop)
        assert "os.replace" in src and ".tmp" in src


# --------------------------------------------------------------------------- #
# Протоколы — по одной задаче, даже при двух воркерах
# --------------------------------------------------------------------------- #
class TestОчередьПротоколов:
    def test_вторая_задача_ждёт_и_отменяется(self):
        store = jobs.store
        assert store._post_lock.acquire(blocking=False)
        try:
            job = jobs.Job(id="x1", filename="a", audio_path="a", language="ru",
                           diarize=False, created_at=time.time(), owner="alice")
            store._jobs[job.id] = job
            ctrl = {"cancel": False}
            got = {}

            def run():
                try:
                    got["ok"] = store._acquire_post(job, ctrl)
                except jobs.JobCancelled:
                    got["cancelled"] = True

            t = threading.Thread(target=run)
            t.start()
            time.sleep(0.3)
            assert t.is_alive(), "вторая задача должна ждать очереди"
            ctrl["cancel"] = True
            t.join(3)
            assert got == {"cancelled": True}
        finally:
            store._post_lock.release()
            store._jobs.pop("x1", None)

    def test_свободная_очередь_берётся_сразу(self):
        store = jobs.store
        job = jobs.Job(id="x2", filename="a", audio_path="a", language="ru",
                       diarize=False, created_at=time.time(), owner="alice")
        assert store._acquire_post(job, {}) is True
        store._post_lock.release()


# --------------------------------------------------------------------------- #
# Уборка: не сразу после старта, ссылки в облаке — из снапшотов
# --------------------------------------------------------------------------- #
class TestУборкаПослеСтарта:
    def test_первая_уборка_через_час(self, monkeypatch):
        s = Scheduler()
        monkeypatch.setattr(sched_mod.threading, "Thread",
                            lambda *a, **k: type("T", (), {"start": lambda self: None,
                                                           "is_alive": lambda self: False})())
        s.start()
        assert time.time() - s._last_sweep < 5

    def test_ссылка_из_снапшота_гасит_ложное_предупреждение(self, tmp_path, monkeypatch, caplog):
        import logging
        rec = tmp_path / "recordings"
        rec.mkdir()
        old = rec / "старая.mp4"
        old.write_bytes(b"x")
        t = time.time() - 3 * 86400
        os.utime(old, (t, t))
        monkeypatch.setattr(sched_mod.security, "list_teams", lambda: ["team"])
        monkeypatch.setattr(sched_mod.security, "user_dir", lambda u: tmp_path)
        monkeypatch.setattr(sched_mod.snapshots, "load",
                            lambda u: {"k": {"out_path": str(old),
                                             "cloud_url": "https://disk/x"}})
        s = Scheduler()
        with caplog.at_level(logging.WARNING, logger="vtx.scheduler"):
            res = s.sweep_recordings(days=2)
        assert res["deleted"] == 1 and not old.exists()
        assert not [r for r in caplog.records if "БЕЗ ссылки" in r.getMessage()]
