"""Сводное ревью 30.10.2026: P0, которые теряют запись или чужой контент.

P0-2 — удаление аккаунта не удаляло задачи, расшифровки и протоколы: логин
освобождался, и новый пользователь с тем же логином видел чужие встречи.
P0-3 — «Тест записи» открывал в серверном Chromium ЛЮБОЙ адрес (SSRF во
внутреннюю сеть) и занимал общесерверные слоты записи кем угодно.
P0-4 — видео, не принятое облаком, после перезапуска не дозаливалось, а
через двое суток уборка удаляла его «БЕЗ ссылки в облаке».
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from app import config
from app.automation import scheduler as sched_mod
from app.automation import snapshots
from app.automation.scheduler import MeetingState, Scheduler
from app.jobs import store
from conftest import login, register


# --------------------------------------------------------------------------- #
# P0-2
# --------------------------------------------------------------------------- #
class TestУдалениеАккаунта:
    def _job(self, owner="alice"):
        job = store.create("Планёрка.mp4", "/nonexistent/x.mp4", "ru", False, owner=owner)
        txt = store.result_path(job.id, "txt")
        txt.parent.mkdir(parents=True, exist_ok=True)
        txt.write_text("секретная расшифровка", encoding="utf-8")
        return job, txt

    def test_задачи_и_файлы_удаляются_вместе_с_аккаунтом(self, client):
        register(client); login(client)
        job, txt = self._job()
        r = client.post("/api/profile/delete", json={"password": "password123"})
        assert r.status_code == 200, r.text
        assert store.get(job.id) is None
        assert not txt.exists()

    def test_новый_аккаунт_с_тем_же_логином_не_видит_чужого(self, client):
        register(client); login(client)
        job, _ = self._job()
        client.post("/api/profile/delete", json={"password": "password123"})
        register(client); login(client)
        assert client.get("/api/jobs").json() == []
        assert client.get(f"/api/jobs/{job.id}").status_code == 404

    def test_задачи_другой_команды_не_трогаются(self, client):
        register(client); login(client)
        other, other_txt = self._job(owner="bob")
        client.post("/api/profile/delete", json={"password": "password123"})
        assert store.get(other.id) is not None and other_txt.exists()

    def test_карточки_встреч_команды_забываются(self, client):
        from app.automation.scheduler import scheduler
        register(client); login(client)
        st = MeetingState(key="alice:1:2026-10-30T10:00:00+00:00", task_id="1",
                          title="Встреча с ОО", url="", start=None, owner="alice",
                          state="done")
        scheduler._states[st.key] = st
        try:
            client.post("/api/profile/delete", json={"password": "password123"})
            assert st.key not in scheduler._states
        finally:
            scheduler._states.pop(st.key, None)


# --------------------------------------------------------------------------- #
# P0-3
# --------------------------------------------------------------------------- #
class TestТестЗаписи:
    def _never(self, monkeypatch):
        from app.automation import recorder
        monkeypatch.setattr(recorder, "record_meeting",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("не должен звать")))
        monkeypatch.setattr(recorder, "acquire_slot",
                            lambda: (_ for _ in ()).throw(AssertionError("слот не нужен")))

    def test_чужой_адрес_отвергается(self, client, monkeypatch):
        register(client); login(client)
        self._never(monkeypatch)
        for url in ("http://169.254.169.254/latest/meta-data/",
                    "http://db:5432/", "file:///etc/passwd",
                    "https://evil.example/telemost.yandex.ru/j/1"):
            r = client.post("/api/automation/recorder/test", json={"url": url})
            assert r.status_code == 400, (url, r.text)

    def test_одна_тестовая_запись_на_сервер(self, client, monkeypatch):
        from app import api_automation
        register(client); login(client)
        self._never(monkeypatch)
        assert api_automation._RECORDER_TEST_LOCK.acquire(blocking=False)
        try:
            r = client.post("/api/automation/recorder/test",
                            json={"url": "https://telemost.yandex.ru/j/123"})
            assert r.status_code == 409 and "тестовая" in r.json()["detail"]
        finally:
            api_automation._RECORDER_TEST_LOCK.release()

    def test_путь_скриншота_наружу_не_уходит(self, client, monkeypatch):
        from app.automation import recorder
        register(client); login(client)
        monkeypatch.setattr(recorder, "acquire_slot", lambda: object())
        monkeypatch.setattr(recorder, "release_slot", lambda s: None)
        monkeypatch.setattr(recorder, "record_meeting",
                            lambda url, *a, **k: {"ok": False, "reason": "join_failed",
                                                  "screenshot": "/data/users/alice/x.png"})
        r = client.post("/api/automation/recorder/test",
                        json={"url": "telemost.yandex.ru/j/123"})
        assert r.status_code == 200
        assert "screenshot" not in r.json()


# --------------------------------------------------------------------------- #
# P0-4
# --------------------------------------------------------------------------- #
class TestДозаливкаПослеРестарта:
    def _arm(self, monkeypatch, cfg=None):
        calls = []
        done = threading.Event()

        def fake_late(self, user, st, cfg_, out, first_delay=300):
            calls.append((st.key, out, first_delay))
            done.set()

        monkeypatch.setattr(Scheduler, "_late_upload", fake_late)
        monkeypatch.setattr(Scheduler, "_await_and_upload_protocol",
                            lambda self, *a, **k: None)
        monkeypatch.setattr(sched_mod.security, "list_teams", lambda: ["alice"])
        monkeypatch.setattr(sched_mod.auto_settings, "load",
                            lambda u: cfg if cfg is not None else
                            {"weeek_token": "tok", "do_protocol": True,
                             "upload_protocol": True})
        monkeypatch.setattr(sched_mod.store, "redeliver", lambda *a, **k: None)
        return calls, done

    def _snap(self, tmp_path, state, task="77", upload_error="облако недоступно",
              cloud_url=None):
        rec = tmp_path / f"30.10.2026, 10-00. - Планёрка-{state}.mp4"
        rec.write_bytes(b"x" * 2048)
        job = store.create(rec.name, str(rec), "ru", False, owner="alice")
        st = MeetingState(key=f"alice:{task}:2026-10-30T10:00:00+00:00", task_id=task,
                          title="Планёрка", url="", start=None, owner="alice",
                          state=state, out_path=str(rec), job_id=job.id,
                          upload_error=upload_error, cloud_url=cloud_url)
        snapshots.save(st)
        return st, rec

    def test_готовая_встреча_без_видео_в_облаке_дозаливается(self, tmp_path, monkeypatch):
        calls, done = self._arm(monkeypatch)
        st, rec = self._snap(tmp_path, "done")
        Scheduler()._resume_pending()
        assert done.wait(2), "дозаливка видео не запущена"
        assert calls[0][0] == st.key and calls[0][1] == str(rec)

    def test_встреча_в_работе_без_видео_дозаливается(self, tmp_path, monkeypatch):
        calls, done = self._arm(monkeypatch)
        st, rec = self._snap(tmp_path, "transcribing")
        Scheduler()._resume_pending()
        assert done.wait(2)

    def test_видео_уже_в_облаке_не_трогается(self, tmp_path, monkeypatch):
        calls, done = self._arm(monkeypatch)
        self._snap(tmp_path, "done", upload_error=None, cloud_url="https://disk/x")
        Scheduler()._resume_pending()
        assert not done.wait(0.3)

    def test_встреча_по_ссылке_без_токена_weeek_восстанавливается(self, tmp_path, monkeypatch):
        """Команда без Weeek, бот по ссылке: раньше _resume_pending пропускал
        такую команду целиком."""
        calls, done = self._arm(monkeypatch, cfg={"do_protocol": True,
                                                  "upload_protocol": True})
        st, rec = self._snap(tmp_path, "transcribing", task="link-20261030-ab12")
        s = Scheduler()
        s._resume_pending()
        assert st.key in s._states
        assert done.wait(2)

    def test_дозаливка_встречи_по_ссылке_не_пишет_в_weeek(self, tmp_path, monkeypatch):
        rec = tmp_path / "x.mp4"
        rec.write_bytes(b"x" * 10)
        st = MeetingState(key="alice:link-1:2026-10-30T10:00:00+00:00",
                          task_id="link-20261030-ab12", title="t", url="", start=None,
                          owner="alice", state="done", out_path=str(rec))
        wrote = []
        monkeypatch.setattr(sched_mod.delivery, "publish_or_upload",
                            lambda *a, **k: {"ok": True, "url": "https://disk/x"})
        monkeypatch.setattr(sched_mod.delivery, "write_weeek_field",
                            lambda *a, **k: wrote.append("field"))
        monkeypatch.setattr(sched_mod.weeek, "add_comment",
                            lambda *a, **k: wrote.append("comment"))
        monkeypatch.setattr(sched_mod.delivery, "delivered_elsewhere", lambda *a: False)
        monkeypatch.setattr(sched_mod, "_meeting_place",
                            lambda *a, **k: {"sub": None})
        monkeypatch.setattr(sched_mod.cloud_folders, "placed", lambda *a, **k: None)
        Scheduler()._late_upload("alice", st, {"weeek_token": "tok",
                                               "weeek_video_field": "Видео",
                                               "post_back_to_weeek": True},
                                 str(rec), first_delay=0)
        assert st.cloud_url == "https://disk/x"
        assert wrote == []
