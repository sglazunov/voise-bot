"""Исходы встречи переживают ретеншн и перезапуск (docs/ТЗ-МЕТРИКИ.md §14.2).

Разобранные здесь числа код формулировал и выбрасывал:

* `reason` остановки записи — рекордер возвращает его с самого начала
  (`silence`, `max_duration`, `chat_stop`…), а планировщик даже не читал. Из-за
  этого четырёхчасовые записи пустой комнаты разбирались руками по логам.
* `upload_error` — запись не уехала в облако, и после перезапуска узнать об
  этом было негде: в снапшот поле не попадало.
* `missed` / `skipped` — встреча, на которую бот не пришёл или которую отсеял
  фильтр, не оставляла следа вообще: задачи распознавания у неё нет, а
  снапшота у этих состояний не было. Поэтому «явка бота» (И36), знаменатель
  которой — ЗАПЛАНИРОВАННЫЕ встречи, была непосчитаема в принципе.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import stats
from app.automation import scheduler as sched_mod
from app.automation import snapshots
from app.automation.scheduler import MeetingState, Scheduler


@pytest.fixture(autouse=True)
def _file_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(stats.security, "team_of", lambda u: "team")
    monkeypatch.setattr(stats.security, "user_dir", lambda t: tmp_path)
    monkeypatch.setattr(stats.db, "enabled", lambda: False)


def _state(**over) -> MeetingState:
    kw = dict(key="team:42:2026-09-14T10:00:00+00:00", task_id="42",
              title="Планёрка", url="https://telemost.yandex.ru/j/1",
              start=None, owner="team")
    kw.update(over)
    return MeetingState(**kw)


# --------------------------------------------------------------------------- #
# 1. Строка исхода встречи
# --------------------------------------------------------------------------- #
class TestOutcomeRow:
    def test_пропущенная_встреча_оставляет_след(self):
        st = _state(state="missed", detail="Время начала прошло — пропущено.")
        stats.record_meeting(st, "missed")
        rows = stats._file_load("team")
        assert len(rows) == 1
        assert rows[0]["kind"] == "meeting"
        assert rows[0]["status"] == "missed"
        assert rows[0]["ok"] is False
        assert "пропущено" in rows[0]["detail"]

    def test_причина_остановки_записи_сохраняется(self):
        st = _state(stop_reason="max_duration", recorded_sec=4 * 3600)
        stats.record_meeting(st, "recorded")
        row = stats._file_load("team")[0]
        assert row["stop_reason"] == "max_duration"
        assert row["duration_sec"] == pytest.approx(4 * 3600)

    def test_повторная_запись_обновляет_ту_же_строку(self):
        """Поздняя дозагрузка в облако не должна плодить вторую встречу — и не
        должна навсегда оставлять «осталось на сервере»."""
        st = _state(upload_error="облако недоступно")
        stats.record_meeting(st, "recorded")
        st.upload_error = None
        stats.record_meeting(st, "recorded")
        rows = stats._file_load("team")
        assert len(rows) == 1
        assert rows[0]["upload_error"] is None

    def test_неизвестный_исход_не_пишется(self):
        stats.record_meeting(_state(), "непонятно")
        assert stats._file_load("team") == []


# --------------------------------------------------------------------------- #
# 2. Сводка: строки двух видов не складываются
# --------------------------------------------------------------------------- #
class TestSummary:
    def _job_row(self, jid="j1"):
        job = type("J", (), {
            "id": jid, "owner": "u", "filename": "встреча.mp4", "duration": 3600.0,
            "speakers": 3, "status": "done", "finished_at": time.time(),
            "analysis_error": None, "provider": "gemini", "preset": "planerka",
            "transcribe_sec": 2400.0, "analysis": {"tasks": [], "decisions": []},
            "llm_usage": {}, "stop_reason": "call_ended",
        })()
        stats.record(job)

    def test_встреча_не_считается_дважды(self, monkeypatch):
        monkeypatch.setattr(stats.security, "team_of", lambda u: "team")
        self._job_row()
        stats.record_meeting(_state(stop_reason="call_ended"), "recorded")
        s = stats.summary("u", days=30)
        # Одна запись: строка задачи и строка исхода — про ОДНУ встречу.
        assert s["meetings"] == 1
        assert s["planned"] == 1 and s["recorded"] == 1

    def test_старые_строки_без_kind_считаются_задачами(self, monkeypatch):
        monkeypatch.setattr(stats.security, "team_of", lambda u: "team")
        stats._file_save("team", [{"id": "old", "team": "team", "at": time.time(),
                                   "duration_sec": 600.0, "ok": True,
                                   "has_protocol": True}])
        s = stats.summary("u", days=30)
        assert s["meetings"] == 1 and s["planned"] == 0

    def test_явка_считается_без_отсеянных(self, monkeypatch):
        monkeypatch.setattr(stats.security, "team_of", lambda u: "team")
        for i in range(22):
            stats.record_meeting(_state(key=f"k{i}"), "recorded")
        for i in range(2):
            stats.record_meeting(_state(key=f"m{i}"), "missed")
        for i in range(5):
            stats.record_meeting(_state(key=f"s{i}"), "skipped")
        s = stats.summary("u", days=30)
        assert (s["planned"], s["recorded"], s["missed"], s["skipped"]) == (29, 22, 2, 5)
        # Знаменатель — 24 (29 запланированных минус 5 сознательно пропущенных):
        # отказ по решению не провал бота.
        assert s["attendance_base"] == 24
        assert s["attendance"] == pytest.approx(round(22 / 24, 3))

    def test_при_малом_знаменателе_процент_не_показывается(self, monkeypatch):
        """ТЗ §9: при знаменателе меньше 20 процент не считать и не показывать —
        «явка 90 %» на десяти встречах это пересказанная процентами единица."""
        monkeypatch.setattr(stats.security, "team_of", lambda u: "team")
        for i in range(9):
            stats.record_meeting(_state(key=f"k{i}"), "recorded")
        stats.record_meeting(_state(key="m"), "missed")
        s = stats.summary("u", days=30)
        assert s["attendance"] is None
        assert (s["attendance_base"], s["recorded"], s["missed"]) == (10, 9, 1)

    def test_разбивка_по_причинам_остановки(self, monkeypatch):
        monkeypatch.setattr(stats.security, "team_of", lambda u: "team")
        for i in range(3):
            stats.record_meeting(_state(key=f"s{i}", stop_reason="silence"), "recorded")
        stats.record_meeting(_state(key="d", stop_reason="max_duration"), "recorded")
        s = stats.summary("u", days=30)
        assert s["by_stop_reason"][0] == {"reason": "silence",
                                         "label": "тишина после разговора",
                                         "count": 3}
        assert s["by_stop_reason"][1]["label"] == "предел длительности"

    def test_несданные_в_облако_записи_видны(self, monkeypatch):
        monkeypatch.setattr(stats.security, "team_of", lambda u: "team")
        stats.record_meeting(_state(key="a", upload_error="Диск: 507"), "recorded")
        stats.record_meeting(_state(key="b"), "recorded")
        assert stats.summary("u", days=30)["upload_failed"] == 1

    def test_сорванная_запись_не_идёт_в_явку(self, monkeypatch):
        monkeypatch.setattr(stats.security, "team_of", lambda u: "team")
        stats.record_meeting(_state(key="e", detail="Запись не удалась."), "rec_error")
        s = stats.summary("u", days=30)
        assert s["rec_failed"] == 1 and s["recorded"] == 0
        assert s["planned"] == 1 and s["attendance_base"] == 1


# --------------------------------------------------------------------------- #
# 3. Снапшот: исход переживает перезапуск
# --------------------------------------------------------------------------- #
class TestSnapshot:
    def test_исход_записи_попадает_в_снапшот(self):
        snap = snapshots.of_state(_state(state="uploading",
                                         stop_reason="silence",
                                         upload_error="облако недоступно",
                                         rec_bytes=5_000_000))
        assert snap["stop_reason"] == "silence"
        assert snap["upload_error"] == "облако недоступно"
        assert snap["rec_bytes"] == 5_000_000

    def test_пропуски_сохраняются(self):
        # Раньше «missed»/«skipped» в _PERSIST_STATES не входили, и перезапуск
        # стирал единственный след встречи, на которую бот не пришёл.
        assert {"missed", "skipped"} <= Scheduler._PERSIST_STATES

    def test_пропущенная_не_становится_ошибкой_после_рестарта(self, monkeypatch,
                                                              tmp_path):
        monkeypatch.setattr(sched_mod.security, "user_dir", lambda u: tmp_path)
        monkeypatch.setattr(snapshots.security, "user_dir", lambda u: tmp_path)
        monkeypatch.setattr(snapshots.db, "enabled", lambda: False)
        st = _state(state="missed", detail="Время начала прошло — пропущено.")
        snapshots.save(st)
        fresh = _state()
        Scheduler._restore_snapshot(Scheduler.__new__(Scheduler), fresh)
        assert fresh.state == "missed"
        assert "пропущено" in fresh.detail

    def test_причина_восстанавливается(self, monkeypatch, tmp_path):
        monkeypatch.setattr(snapshots.security, "user_dir", lambda u: tmp_path)
        monkeypatch.setattr(snapshots.db, "enabled", lambda: False)
        snapshots.save(_state(state="done", stop_reason="chat_stop",
                              upload_error="облако недоступно"))
        fresh = _state()
        Scheduler._restore_snapshot(Scheduler.__new__(Scheduler), fresh)
        assert fresh.stop_reason == "chat_stop"
        assert fresh.upload_error == "облако недоступно"


# --------------------------------------------------------------------------- #
# 4. Планировщик действительно читает результат записи
# --------------------------------------------------------------------------- #
class _Slot:
    index = 0


class TestSchedulerReadsReason:
    def _prepare(self, monkeypatch, tmp_path, res):
        created = {}
        monkeypatch.setattr(sched_mod.security, "user_dir", lambda u: tmp_path)
        monkeypatch.setattr(snapshots.security, "user_dir", lambda u: tmp_path)
        monkeypatch.setattr(snapshots.db, "enabled", lambda: False)
        monkeypatch.setattr(sched_mod.auto_settings, "load", lambda u: {
            "do_transcribe": True, "do_protocol": False, "live_transcribe": False,
            "weeek_set_video_field": False, "post_back_to_weeek": False,
            "analyze_provider": "auto", "ocr_screen": False,
            "upload_protocol": False, "analyze_preset": "auto"})
        monkeypatch.setattr(sched_mod.recorder, "record_meeting",
                            lambda *a, **kw: res)
        monkeypatch.setattr(sched_mod.recorder, "release_slot", lambda s: None)
        monkeypatch.setattr(sched_mod.delivery, "upload_with_retry",
                            lambda out, cfg, log, **kw: {"ok": True,
                                                         "url": "https://disk/x"})
        monkeypatch.setattr(sched_mod.delivery, "delivered_elsewhere",
                            lambda up, out: False)
        monkeypatch.setattr(sched_mod, "_protocol_wanted",
                            lambda cfg, user, title, log: False)

        def _create(**kw):
            created.update(kw)
            return type("J", (), {"id": "job1", "status": "queued"})()

        monkeypatch.setattr(sched_mod.store, "create", _create)
        return created

    def test_причина_доезжает_до_карточки_задачи_и_метрики(self, monkeypatch,
                                                           tmp_path):
        rec = tmp_path / "rec.mp4"
        created = self._prepare(monkeypatch, tmp_path, {
            "ok": True, "path": str(rec), "reason": "silence",
            "size": 7_000_000, "audio_warning": False})
        rec.parent.mkdir(parents=True, exist_ok=True)
        rec.write_bytes(b"x")

        s = Scheduler()
        st = _state()
        s._states[st.key] = st
        s._run(st, _Slot())

        assert st.stop_reason == "silence"
        assert st.rec_bytes == 7_000_000
        assert created["stop_reason"] == "silence"
        row = [r for r in stats._file_load("team") if r.get("kind") == "meeting"][0]
        assert row["status"] == "recorded" and row["stop_reason"] == "silence"
        # И в карточке встречи это видно словами, а не кодом.
        assert any("тишина после разговора" in m for m in st.logs)

    def test_предел_длительности_поднимает_тревогу(self, monkeypatch, tmp_path):
        rec = tmp_path / "rec.mp4"
        self._prepare(monkeypatch, tmp_path, {
            "ok": True, "path": str(rec), "reason": "max_duration",
            "size": 900_000_000, "audio_warning": True})
        rec.write_bytes(b"x")

        s = Scheduler()
        st = _state()
        s._states[st.key] = st
        s._run(st, _Slot())
        assert any("предел длительности" in m for m in st.logs)
        assert any("никто не завершил" in m for m in st.logs)

    def test_сорванная_запись_пишет_исход(self, monkeypatch, tmp_path):
        self._prepare(monkeypatch, tmp_path, {
            "ok": False, "reason": "nobody_joined",
            "error": "Файл записи пуст — проверьте аудио-устройство и ffmpeg."})
        s = Scheduler()
        st = _state()
        s._states[st.key] = st
        s._run(st, _Slot())
        assert st.state == "error"
        row = [r for r in stats._file_load("team") if r.get("kind") == "meeting"][0]
        assert row["status"] == "rec_error"
        assert row["stop_reason"] == "nobody_joined"


class TestSkippedAndMissed:
    """`_maybe_trigger` — единственное место, где встреча становится
    «пропущена» или «отсеяна». Раньше это состояние жило только в памяти."""

    def _sched(self, monkeypatch, tmp_path):
        monkeypatch.setenv("VTX_RECORDER_ENABLED", "1")
        monkeypatch.setattr(sched_mod.security, "user_dir", lambda u: tmp_path)
        monkeypatch.setattr(snapshots.security, "user_dir", lambda u: tmp_path)
        monkeypatch.setattr(snapshots.db, "enabled", lambda: False)
        monkeypatch.setattr(sched_mod.recorder, "acquire_slot", lambda: None)
        return Scheduler()

    def _meetings(self, s, st):
        s._states[st.key] = st

    def test_отсеянная_фильтром_попадает_в_метрику(self, monkeypatch, tmp_path):
        s = self._sched(monkeypatch, tmp_path)
        now = datetime.now(timezone.utc)
        st = _state(start=now, state="scheduled", title="Обед")
        self._meetings(s, st)
        s._maybe_trigger("team", {"lookahead_min": 5, "rec_exclude": "обед"})
        assert st.state == "skipped"
        row = [r for r in stats._file_load("team") if r.get("kind") == "meeting"][0]
        assert row["status"] == "skipped" and "исключено по слову" in row["detail"]

    def test_пропущенная_по_времени_попадает_в_метрику(self, monkeypatch, tmp_path):
        s = self._sched(monkeypatch, tmp_path)
        s._boot_time = 0.0
        started = datetime.now(timezone.utc) - timedelta(hours=3)
        st = _state(start=started, state="scheduled")
        self._meetings(s, st)
        s._maybe_trigger("team", {"lookahead_min": 5})
        assert st.state == "missed"
        row = [r for r in stats._file_load("team") if r.get("kind") == "meeting"][0]
        assert row["status"] == "missed"
        # И снапшот тоже есть: перезапуск больше не стирает след.
        assert snapshots.load("team")[st.key]["state"] == "missed"
