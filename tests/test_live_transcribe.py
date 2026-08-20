"""Д10: live-расшифровка — замок модели (nonblocking), заметки на встрече до
появления job, live_view отдаёт live-текст → partial → финал.
"""
import threading

from app import security
from app.automation import snapshots
from app.automation.scheduler import MeetingState, Scheduler
from app.jobs import store
from app.transcribe import _transcribe_lock, transcribe_file


class TestNonblockingLock:
    def test_busy_lock_returns_none(self):
        assert _transcribe_lock.acquire()
        try:
            assert transcribe_file("/nonexistent.wav", nonblocking=True) is None
        finally:
            _transcribe_lock.release()


def _mk_state(s: Scheduler, **kw) -> MeetingState:
    d = dict(key="alice:5:2026-07-18T10:00:00+00:00", task_id="5", title="Т",
             url="", start=None, owner="alice", state="recording")
    d.update(kw)
    st = MeetingState(**d)
    with s._lock:
        s._states[st.key] = st
    return st


class TestMeetingNotes:
    def test_notes_before_job_and_forwarded_after(self):
        s = Scheduler()
        st = _mk_state(s)
        assert s.set_meeting_notes("alice", "5", "решили: релиз в пт")["ok"]
        assert s.get_meeting_notes("alice", "5")["notes"] == "решили: релиз в пт"
        # job appears later → notes forward onto it
        job = store.create(filename="a.mp4", audio_path="/tmp/a.mp4",
                           language="ru", diarize=False, owner="alice")
        st.job_id = job.id
        s.set_meeting_notes("alice", "5", "решили: релиз в пт; +бюджет")
        assert store.get(job.id).user_notes == "решили: релиз в пт; +бюджет"

    def test_team_member_sees_admins_meeting(self, monkeypatch):
        s = Scheduler()
        _mk_state(s, live_notes="x")
        monkeypatch.setattr(security, "team_of", lambda u: "alice")
        assert s.get_meeting_notes("bob", "5")["ok"]

    def test_unknown_meeting(self):
        s = Scheduler()
        assert not s.set_meeting_notes("alice", "404", "x")["ok"]


class TestLiveView:
    def test_live_text_while_recording(self):
        s = Scheduler()
        _mk_state(s, live_text="[00:10] привет всем", live_updated_at=123.0)
        r = s.live_view("alice", "5")
        assert r["ok"] and r["recording"] and not r["final"]
        assert "привет всем" in r["text"]

    def test_final_transcript_replaces_live(self):
        s = Scheduler()
        job = store.create(filename="a.mp4", audio_path="/tmp/a.mp4",
                           language="ru", diarize=False, owner="alice")
        store.result_path(job.id, "txt").write_text("[00:01] финальный текст",
                                                    encoding="utf-8")
        _mk_state(s, state="done", job_id=job.id, live_text="старый live")
        r = s.live_view("alice", "5")
        assert r["final"] and "финальный текст" in r["text"]

    def test_partial_between_live_and_final(self):
        s = Scheduler()
        job = store.create(filename="a.mp4", audio_path="/tmp/a.mp4",
                           language="ru", diarize=False, owner="alice")
        store._partial[job.id] = [{"start": 0, "end": 1, "text": "кусочек"}]
        _mk_state(s, state="transcribing", job_id=job.id, live_text="live")
        r = s.live_view("alice", "5")
        assert not r["final"] and "кусочек" in r["text"]


class TestSnapshotNotes:
    def test_live_notes_survive_save_load(self):
        s = Scheduler()
        st = _mk_state(s, live_notes="важные заметки")
        snapshots.save(st)
        assert snapshots.load("alice")[st.key]["live_notes"] == "важные заметки"


class TestСтадияПослеРасшифровки:
    """Полоса прогресса показывает только распознавание фрагментов. За ней
    идут разметка говорящих и чтение текста с экрана — каждая на десятки
    минут, и всё это время интерфейс показывал «распознаётся, 100%».
    Человек считал, что задача зависла."""

    def test_стадия_ставится_и_снимается(self):
        from app.jobs import store
        store._set_stage("j1", "Читаю текст с экрана…")
        assert store.stage("j1") == "Читаю текст с экрана…"
        store._set_stage("j1", "")
        assert store.stage("j1") == ""

    def test_у_чужой_задачи_стадии_нет(self):
        from app.jobs import store
        assert store.stage("нет-такой") == ""
