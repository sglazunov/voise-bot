"""Записи планировщика в Weeek — только в задачу на доске команды.

Второй раунд ревью 30.10: проверку доски получила доставка протокола
(`jobs._deliver_protocol`), а поле «Видео встречи» и комментарии, которые пишет
сам планировщик (после записи, при дозаливке, у восстановленной сироты), шли в
`st.task_id` без неё. Задачу могли перенести на доску другого клиента, пока шла
запись или висела дозаливка, — ссылка на чужую встречу уезжала к нему.
"""
from __future__ import annotations

from pathlib import Path

from app.automation import scheduler as sched_mod
from app.automation.scheduler import MeetingState, Scheduler

_CFG = {"weeek_token": "tok", "weeek_video_field": "Видео встречи",
        "weeek_set_video_field": True, "post_back_to_weeek": True,
        "managed_by_server": True, "weeek_board_id": "1"}


def _arm(monkeypatch, on_board: bool):
    writes: list[tuple] = []
    monkeypatch.setattr(sched_mod.weeek, "task_on_team_board",
                        lambda cfg, task_id: on_board)
    monkeypatch.setattr(sched_mod.delivery, "write_weeek_field",
                        lambda *a, **k: writes.append(("field", a[1])))
    monkeypatch.setattr(sched_mod.weeek, "add_comment",
                        lambda tok, task_id, text: writes.append(("comment", task_id)) or True)
    return writes


class TestДозаливка:
    def _late(self, monkeypatch, tmp_path, on_board):
        writes = _arm(monkeypatch, on_board)
        rec = tmp_path / "x.mp4"
        rec.write_bytes(b"x" * 10)
        st = MeetingState(key="bob:101:2026-10-30T10:00:00+00:00", task_id="101",
                          title="t", url="", start=None, owner="bob", state="done",
                          out_path=str(rec), upload_error="облако недоступно")
        monkeypatch.setattr(sched_mod.delivery, "publish_or_upload",
                            lambda *a, **k: {"ok": True, "url": "https://disk/x"})
        monkeypatch.setattr(sched_mod.delivery, "delivered_elsewhere", lambda *a: False)
        monkeypatch.setattr(sched_mod, "_meeting_place", lambda *a, **k: {"sub": None})
        monkeypatch.setattr(sched_mod.cloud_folders, "placed", lambda *a, **k: None)
        Scheduler()._late_upload("bob", st, dict(_CFG), str(rec), first_delay=0)
        return st, writes

    def test_задача_ушла_с_доски_ссылка_не_пишется(self, monkeypatch, tmp_path):
        st, writes = self._late(monkeypatch, tmp_path, on_board=False)
        assert st.cloud_url == "https://disk/x", "видео уехало, ссылка у карточки есть"
        assert writes == [], f"запись в задачу чужой доски: {writes}"

    def test_задача_на_доске_ссылка_пишется(self, monkeypatch, tmp_path):
        _, writes = self._late(monkeypatch, tmp_path, on_board=True)
        assert ("field", "101") in writes and ("comment", "101") in writes


class TestПослеЗаписи:
    def _run(self, monkeypatch, tmp_path, on_board):
        from tests.test_meeting_outcomes import TestSchedulerReadsReason, _Slot, _state
        TestSchedulerReadsReason()._prepare(monkeypatch, tmp_path, {})
        cfg = dict(sched_mod.auto_settings.load("team"), **_CFG)
        monkeypatch.setattr(sched_mod.auto_settings, "load", lambda u: cfg)

        def recording(url, out, c, **kw):
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"x")
            return {"ok": True, "path": out, "reason": "stopped", "size": 1}

        monkeypatch.setattr(sched_mod.recorder, "record_meeting", recording)
        writes = _arm(monkeypatch, on_board)
        s = Scheduler()
        st = _state()
        s._states[st.key] = st
        s._run(st, _Slot())
        return writes

    def test_задача_ушла_с_доски_во_время_записи(self, monkeypatch, tmp_path):
        writes = self._run(monkeypatch, tmp_path, on_board=False)
        assert writes == [], f"запись в задачу чужой доски: {writes}"

    def test_задача_на_доске(self, monkeypatch, tmp_path):
        writes = self._run(monkeypatch, tmp_path, on_board=True)
        assert ("field", "42") in writes
