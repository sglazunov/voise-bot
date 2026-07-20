"""Д8+Д9: устойчивость записи — снапшот пути файла, подхват осиротевшей записи
после рестарта (kill -9), флаги fMP4 в команде ffmpeg, сторож звука в результате.
"""
from pathlib import Path

from app.automation import scheduler as sched_mod
from app.automation.recorder import capture
from app.automation.scheduler import MeetingState, Scheduler
from app.jobs import store


class TestFragmentedMp4:
    def test_mp4_gets_fragmented_movflags(self):
        cmd = capture.build_ffmpeg_cmd("/tmp/x.mp4", {"capture_video": True})
        i = cmd.index("-movflags")
        assert "frag_keyframe" in cmd[i + 1] and "empty_moov" in cmd[i + 1]
        assert cmd[-1] == "/tmp/x.mp4"

    def test_non_mp4_untouched(self):
        cmd = capture.build_ffmpeg_cmd("/tmp/x.webm", {"capture_video": True})
        assert "-movflags" not in cmd


class TestSnapshotRoundTrip:
    def test_out_path_survives_save_load(self):
        s = Scheduler()
        st = MeetingState(key="alice:42:2026-07-18T10:00:00+00:00", task_id="42",
                          title="t", url="", start=None, owner="alice",
                          state="recording", out_path="/data/rec/a.mp4")
        s._save_state(st)
        snap = s._load_snaps("alice")[st.key]
        assert snap["out_path"] == "/data/rec/a.mp4"
        assert snap["state"] == "recording"


class TestOrphanResume:
    def _cfg(self):
        return {"weeek_token": "tok", "do_transcribe": True, "do_protocol": True,
                "upload_protocol": True, "weeek_set_video_field": False,
                "analyze_provider": "auto", "ocr_screen": False,
                "identify_speakers": True}

    def test_orphaned_recording_is_queued(self, tmp_path, monkeypatch):
        rec = tmp_path / "18.07.2026, 10:00. - Планёрка.mp4"
        rec.write_bytes(b"x" * 2048)

        s = Scheduler()
        st = MeetingState(key="alice:77:2026-07-18T10:00:00+00:00", task_id="77",
                          title="Планёрка", url="", start=None, owner="alice",
                          state="recording", out_path=str(rec))
        s._save_state(st)

        monkeypatch.setattr(sched_mod.security, "list_teams", lambda: ["alice"])
        monkeypatch.setattr(sched_mod.auto_settings, "load", lambda u: self._cfg())
        # cloud upload succeeds instantly; waiter thread is irrelevant here
        monkeypatch.setattr(Scheduler, "_upload_with_retry",
                            lambda self, out, cfg, log, attempts=1:
                            {"ok": True, "url": "https://disk/x"})
        monkeypatch.setattr(Scheduler, "_await_and_upload_protocol",
                            lambda self, *a, **k: None)

        s._resume_pending()

        jobs = [j for j in store.list(owner="alice")
                if j.audio_path == str(rec)]
        assert len(jobs) == 1, "осиротевшая запись должна попасть в очередь"
        j = jobs[0]
        assert j.analyze and j.deliver_weeek_task == "77"
        assert j.deliver_protocol_cloud
        assert j.identify_speakers          # Д7: спикеры безусловно
        snap = s._load_snaps("alice")[st.key]
        assert snap["state"] == "transcribing"
        assert snap["job_id"] == j.id

    def test_orphan_with_missing_file_skipped(self, monkeypatch):
        s = Scheduler()
        st = MeetingState(key="alice:88:2026-07-18T11:00:00+00:00", task_id="88",
                          title="x", url="", start=None, owner="alice",
                          state="recording", out_path="/nonexistent/a.mp4")
        s._save_state(st)
        monkeypatch.setattr(sched_mod.security, "list_teams", lambda: ["alice"])
        monkeypatch.setattr(sched_mod.auto_settings, "load", lambda u: self._cfg())
        s._resume_pending()
        assert not [j for j in store.list(owner="alice")
                    if j.deliver_weeek_task == "88"]

    def test_done_meetings_not_requeued(self, tmp_path, monkeypatch):
        rec = tmp_path / "done.mp4"
        rec.write_bytes(b"x")
        s = Scheduler()
        st = MeetingState(key="alice:99:2026-07-18T12:00:00+00:00", task_id="99",
                          title="x", url="", start=None, owner="alice",
                          state="done", out_path=str(rec))
        s._save_state(st)
        monkeypatch.setattr(sched_mod.security, "list_teams", lambda: ["alice"])
        monkeypatch.setattr(sched_mod.auto_settings, "load", lambda u: self._cfg())
        s._resume_pending()
        assert not [j for j in store.list(owner="alice")
                    if j.deliver_weeek_task == "99"]


class TestMissedVisibility:
    def _status(self, s, monkeypatch):
        from app.automation import scheduler as sched_mod
        monkeypatch.setattr(sched_mod.security, "team_of", lambda u: "alice")
        monkeypatch.setattr(sched_mod.auto_settings, "load", lambda u: {})
        monkeypatch.setattr(sched_mod.recorder, "active_recordings", lambda: 0)
        return s.status("alice")

    def test_all_fresh_missed_visible(self, monkeypatch):
        from datetime import datetime, timedelta, timezone
        s = Scheduler()
        now = datetime.now(timezone.utc)
        for i, delta_h in enumerate((1, 5, 30)):   # два свежих, один старый
            st = MeetingState(key=f"alice:{i}:x", task_id=str(i), title=f"m{i}",
                              url="", start=now - timedelta(hours=delta_h),
                              owner="alice", state="missed")
            with s._lock:
                s._states[st.key] = st
        meetings = self._status(s, monkeypatch)["meetings"]
        missed_ids = {m["task_id"] for m in meetings if m["state"] == "missed"}
        assert missed_ids == {"0", "1"}   # оба свежих видны, суточной давности - скрыт

    def test_single_old_missed_still_shown(self, monkeypatch):
        from datetime import datetime, timedelta, timezone
        s = Scheduler()
        now = datetime.now(timezone.utc)
        for i, delta_h in enumerate((30, 50)):
            st = MeetingState(key=f"alice:{i}:y", task_id=str(i), title=f"m{i}",
                              url="", start=now - timedelta(hours=delta_h),
                              owner="alice", state="missed")
            with s._lock:
                s._states[st.key] = st
        meetings = self._status(s, monkeypatch)["meetings"]
        missed = [m for m in meetings if m["state"] == "missed"]
        assert len(missed) == 1 and missed[0]["task_id"] == "0"  # свежайший из старых


class TestInterruptedRecognitionRetry:
    def test_interrupted_job_is_requeued_on_resume(self, tmp_path, monkeypatch):
        from app.automation import scheduler as sched_mod
        rec = tmp_path / "int.mp4"
        rec.write_bytes(b"x" * 1024)
        job = store.create(filename="int.mp4", audio_path=str(rec),
                           language="ru", diarize=False, analyze=True,
                           owner="alice")
        job.status = "error"
        job.error = "Прервано (сервис был перезапущен)."

        s = Scheduler()
        st = MeetingState(key="alice:55:2026-07-20T10:00:00+00:00", task_id="55",
                          title="x", url="", start=None, owner="alice",
                          state="transcribing", job_id=job.id,
                          out_path=str(rec), do_protocol=True)
        s._save_state(st)
        monkeypatch.setattr(sched_mod.security, "list_teams", lambda: ["alice"])
        monkeypatch.setattr(sched_mod.auto_settings, "load", lambda u: {
            "weeek_token": "tok", "do_transcribe": True, "do_protocol": True,
            "upload_protocol": True})
        monkeypatch.setattr(Scheduler, "_await_and_upload_protocol",
                            lambda self, *a, **k: None)
        retried = []
        monkeypatch.setattr(store, "retry", lambda jid: retried.append(jid))
        s._resume_pending()
        assert retried == [job.id]

    def test_interrupted_job_with_lost_file_not_retried(self, monkeypatch):
        from app.automation import scheduler as sched_mod
        job = store.create(filename="gone.mp4", audio_path="/nonexistent/gone.mp4",
                           language="ru", diarize=False, owner="alice")
        job.status = "error"
        job.error = "Прервано (сервис был перезапущен)."
        s = Scheduler()
        st = MeetingState(key="alice:56:2026-07-20T11:00:00+00:00", task_id="56",
                          title="x", url="", start=None, owner="alice",
                          state="transcribing", job_id=job.id, do_protocol=True)
        s._save_state(st)
        monkeypatch.setattr(sched_mod.security, "list_teams", lambda: ["alice"])
        monkeypatch.setattr(sched_mod.auto_settings, "load", lambda u: {
            "weeek_token": "tok", "upload_protocol": True})
        monkeypatch.setattr(Scheduler, "_await_and_upload_protocol",
                            lambda self, *a, **k: None)
        retried = []
        monkeypatch.setattr(store, "retry", lambda jid: retried.append(jid))
        s._resume_pending()
        assert retried == []


class TestRescheduledSlots:
    def _poll_with(self, s, monkeypatch, meetings):
        from app.automation import scheduler as sched_mod
        monkeypatch.setattr(sched_mod.weeek, "upcoming_meetings",
                            lambda tok, pid, tz: meetings)
        monkeypatch.setattr(sched_mod.weeek, "custom_field_bool",
                            lambda raw, field: None)
        s._poll("alice", {"weeek_token": "t"})

    def _meeting(self, task_id, start):
        from app.automation import scheduler as sched_mod
        return sched_mod.weeek.Meeting(task_id=task_id, title="Онбординг",
                                       url="https://telemost.yandex.ru/j/1",
                                       start=start)

    def test_time_change_removes_empty_old_slot(self, monkeypatch):
        from datetime import datetime, timezone
        s = Scheduler()
        old = MeetingState(key="alice:9209:2026-07-20T00:00:00+00:00",
                           task_id="9209", title="x", url="", owner="alice",
                           start=datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc),
                           state="scheduled")
        with s._lock:
            s._states[old.key] = old
        self._poll_with(s, monkeypatch, [self._meeting(
            "9209", datetime(2026, 7, 20, 11, 0, tzinfo=timezone.utc))])
        keys = [k for k in s._states if ":9209:" in k]
        assert keys == ["alice:9209:2026-07-20T11:00:00+00:00"]

    def test_missed_dup_removed_when_same_day_recorded(self, monkeypatch):
        from datetime import datetime, timezone
        s = Scheduler()
        rec = MeetingState(key="alice:9209:2026-07-20T00:00:00+00:00",
                           task_id="9209", title="x", url="", owner="alice",
                           start=datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc),
                           state="uploading")   # запись уже идёт по старому слоту
        new_start = datetime(2026, 7, 20, 11, 0, tzinfo=timezone.utc)
        missed = MeetingState(key="alice:9209:2026-07-20T11:00:00+00:00",
                              task_id="9209", title="x", url="", owner="alice",
                              start=new_start, state="missed")
        with s._lock:
            s._states[rec.key] = rec
            s._states[missed.key] = missed
        self._poll_with(s, monkeypatch, [self._meeting("9209", new_start)])
        states = {k: v.state for k, v in s._states.items() if ":9209:" in k}
        assert states == {rec.key: "uploading"}   # дубль-«пропущена» убран

    def test_recurring_missed_other_day_kept(self, monkeypatch):
        from datetime import datetime, timezone
        s = Scheduler()
        done = MeetingState(key="alice:9209:2026-07-13T11:00:00+00:00",
                            task_id="9209", title="x", url="", owner="alice",
                            start=datetime(2026, 7, 13, 11, 0, tzinfo=timezone.utc),
                            state="done")       # прошлая неделя записана
        this_start = datetime(2026, 7, 20, 11, 0, tzinfo=timezone.utc)
        missed = MeetingState(key="alice:9209:2026-07-20T11:00:00+00:00",
                              task_id="9209", title="x", url="", owner="alice",
                              start=this_start, state="missed")
        with s._lock:
            s._states[done.key] = done
            s._states[missed.key] = missed
        self._poll_with(s, monkeypatch, [self._meeting("9209", this_start)])
        assert s._states[missed.key].state == "missed"   # легитимная — осталась
