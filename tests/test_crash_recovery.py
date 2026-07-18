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
