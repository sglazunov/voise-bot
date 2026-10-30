"""Адверсариальное ревью 30.10.2026 (docs/АДВЕРСАРИАЛЬНОЕ-РЕВЮ-2026-10-30.md):
раздел 2 «Удаление аккаунта», раздел 5 «Безопасность» и выжившие мутанты
этих областей из раздела 7.

Удаление аккаунта: задача удалённой команды не отменялась (`_control` —
словарь, `ctl.cancel()` падал и глушился), приостановленная крутилась вечно и
занимала воркер; логин освобождался ДО удаления задач; всё, что писалось после
удаления, доставалось следующему владельцу логина; роль основателя переходила
к случайному клиенту; идущий опрос Weeek и выгрузка видео воскрешали карточки
и задачи удалённой команды; ожидатель протокола писал в Weeek ложное «задача
пропала».

Безопасность: перебор пароля через телефон, Telegram и удаление аккаунта без
лимита; запросы с соседнего поддомена (`Sec-Fetch-Site: same-site`) с сессией;
восстановление пароля без проверки CSRF.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from app import config, jobs as jobs_mod, llm_custom, security, sms
from app.automation import scheduler as sched_mod
from app.automation import snapshots
from app.automation.scheduler import MeetingState, Scheduler, telemost_url
from app.jobs import STATUS_DONE, STATUS_PAUSED, STATUS_RUNNING, Job, store
from app.main import app
from conftest import login, register

PHONE = "+79990000000"
PWD = "password123"


def _job(owner="alice", status=STATUS_RUNNING, jid="adv-job-1") -> Job:
    """Задача прямо в хранилище, мимо очереди: настоящий воркер её не тронет."""
    job = Job(id=jid, filename="Планёрка.mp4", audio_path="/nonexistent/x.mp4",
              language="ru", diarize=False, owner=owner, status=status)
    store._jobs[job.id] = job
    return job


def _last_code() -> str:
    m = re.search(r"\b(\d{6})\b", sms.sent_messages[-1]["text"])
    assert m
    return m.group(1)


# --------------------------------------------------------------------------- #
# 2. Удаление аккаунта
# --------------------------------------------------------------------------- #
class TestОтменаЗадачПриУдалении:
    def test_идущая_задача_получает_отмену(self):
        job = _job()
        ctrl = store._control.setdefault(job.id, {"pause": False, "cancel": False})
        store.delete_owner("alice")
        assert ctrl["cancel"] is True
        # Сборка протокола спрашивает флаг через словарь хранилища, а не через
        # свою ссылку: отмена должна быть видна и там.
        assert store._control.get(job.id, {}).get("cancel") is True

    def test_приостановленная_задача_не_вешает_воркер(self):
        job = _job(status=STATUS_PAUSED)
        ctrl = store._control.setdefault(job.id, {"pause": True, "cancel": False})
        released = threading.Event()

        def worker():          # тот же цикл ожидания, что в JobStore._process
            while ctrl.get("pause") and not ctrl.get("cancel"):
                time.sleep(0.01)
            released.set()

        threading.Thread(target=worker, daemon=True).start()
        store.delete_owner("alice")
        assert released.wait(2), "приостановленная задача удалённой команды держит воркер"
        assert ctrl["pause"] is False

    def test_после_удаления_задача_не_пишет_метрику_и_индекс(self, monkeypatch):
        job = _job()
        store.delete_owner("alice")
        recorded, indexed = [], []
        from app import stats
        monkeypatch.setattr(stats, "record", lambda j: recorded.append(j.id))
        monkeypatch.setattr(store, "index_search", lambda j: indexed.append(j.id))
        store._set(job, status=STATUS_DONE, finished_at=time.time())
        assert recorded == [] and indexed == []

    def test_файлы_задачи_дописанные_после_удаления_убираются(self):
        """Воркер дописывает результаты, пока не увидит отмену: после его
        выхода от задачи удалённой команды на диске не остаётся ничего."""
        job = _job()
        txt = store.result_path(job.id, "txt")
        store.delete_owner("alice")
        txt.parent.mkdir(parents=True, exist_ok=True)
        txt.write_text("секретная расшифровка", encoding="utf-8")   # воркер дописал
        store._process(job)                 # воркер доходит до конца задачи
        assert not txt.exists()
        assert job.id not in store._control and job.id not in store._partial

    def test_сбой_записи_не_теряет_задачи(self, monkeypatch):
        """Хранилище не приняло удаление — задачи остаются, повтор удаления
        находит их снова (иначе строки в базе пережили бы аккаунт навсегда)."""
        job = _job(status=STATUS_DONE)

        def fail(*a, **k):
            raise OSError("диск полон")
        monkeypatch.setattr(store, "_save", fail)
        with pytest.raises(OSError):
            store.delete_owner("alice")
        assert store.get(job.id) is not None


class TestПорядокУдаления:
    def test_логин_жив_пока_удаляются_задачи(self, client, monkeypatch):
        register(client); login(client)
        seen = []
        monkeypatch.setattr(store, "delete_owner",
                            lambda owner: seen.append(security.user_exists(owner)) or 0)
        r = client.post("/api/profile/delete", json={"password": PWD})
        assert r.status_code == 200, r.text
        assert seen == [True], "логин освободился до удаления задач"
        assert not security.user_exists("alice")

    def test_сбой_удаления_задач_оставляет_аккаунт(self, client, monkeypatch):
        register(client); login(client)
        job = _job()

        def fail(owner):
            raise RuntimeError("база недоступна")
        monkeypatch.setattr(store, "delete_owner", fail)
        r = client.post("/api/profile/delete", json={"password": PWD})
        assert r.status_code >= 500
        assert security.user_exists("alice")
        assert store.get(job.id) is not None
        client.post("/api/auth/logout")
        assert login(client).status_code == 200

    def test_удаление_задач_переживает_перезапуск_в_файловом_режиме(self):
        """Мутант «delete_owner без _save()»: в памяти задачи нет, а в jobs.json
        осталась — после перезапуска она воскресает под освободившимся логином."""
        job = _job(status=STATUS_DONE)
        store._save()
        assert job.id in config.JOBS_FILE.read_text(encoding="utf-8")
        store.delete_owner("alice")
        ids = [d["id"] for d in json.loads(config.JOBS_FILE.read_text(encoding="utf-8"))]
        assert job.id not in ids

    def test_задача_убирается_из_поискового_индекса(self, monkeypatch):
        """Мутант «без search_delete»: расшифровка оставалась в search_docs и
        находилась поиском под освободившимся логином."""
        job = _job(status=STATUS_DONE)
        removed = []
        fake_db = SimpleNamespace(enabled=lambda: True,
                                  search_delete=lambda jid: removed.append(jid),
                                  jobs_save=lambda rows: None,
                                  job_upsert=lambda row: None)
        monkeypatch.setattr(jobs_mod, "db", fake_db)
        store.delete_owner("alice")
        assert removed == [job.id]


class TestОснователь:
    def test_основатель_не_удаляет_себя_пока_есть_другие(self, client):
        register(client, "founder")
        other = TestClient(app)
        register(other, "client1", phone="+79995556677")
        assert security.is_super_admin("founder")
        r = client.post("/api/profile/delete", json={"password": PWD})
        assert r.status_code == 400
        assert "основател" in r.json()["detail"].lower()
        assert security.user_exists("founder")
        assert not security.is_super_admin("client1")

    def test_основатель_без_флага_всё_равно_защищён(self, client):
        """Старые данные без флага `is_super`: основатель определяется при
        проверке, и запрет на удаление действует на него так же."""
        register(client, "founder")
        register(TestClient(app), "client1", phone="+79995556677")
        with security._LOCK:
            users = security._load_users()
            for rec in users.values():
                rec.pop("is_super", None)
            security._save_users(users)
        r = client.post("/api/profile/delete", json={"password": PWD})
        assert r.status_code == 400
        assert security.user_exists("founder")

    def test_неверный_пароль_основателю_не_подсказывает_роль(self, client):
        register(client, "founder")
        register(TestClient(app), "client1", phone="+79995556677")
        r = client.post("/api/profile/delete", json={"password": "wrong-pass"})
        assert r.status_code == 400
        assert "основател" not in r.json()["detail"].lower()

    def test_единственный_аккаунт_удаляется(self, client):
        register(client, "founder")
        assert security.is_super_admin("founder")
        r = client.post("/api/profile/delete", json={"password": PWD})
        assert r.status_code == 200, r.text
        # Сервер пуст — следующий зарегистрированный честно становится
        # основателем, как на свежей установке.
        register(client, "next1", phone="+79995556677")
        assert security.is_super_admin("next1")


def _meeting(task_id="7"):
    from datetime import datetime, timedelta, timezone
    return SimpleNamespace(task_id=task_id, title="Встреча", raw={},
                           url="https://telemost.yandex.ru/j/1",
                           start=datetime.now(timezone.utc) + timedelta(hours=1))


class TestПланировщикПослеУдаления:
    def test_forget_team_останавливает_запись(self):
        """Мутант «forget_team без stop_flag»: идущая запись удалённой команды
        продолжалась до конца встречи."""
        s = Scheduler()
        st = MeetingState(key="alice:1:x", task_id="1", title="t", url="",
                          start=None, owner="alice", state="recording")
        s._states[st.key] = st
        s.forget_team("alice")
        assert st.stop_flag is True and st.forgotten is True

    def test_идущий_опрос_не_воскрешает_карточки(self, monkeypatch):
        s = Scheduler()

        def upcoming(*a, **k):
            s.forget_team("alice")          # аккаунт удалили, пока шёл запрос
            return [_meeting()]
        monkeypatch.setattr(sched_mod.weeek, "upcoming_meetings", upcoming)
        monkeypatch.setattr(sched_mod.snapshots, "load", lambda u: {})
        s._poll("alice", {"weeek_token": "t"})
        assert [k for k in s._states if k.startswith("alice:")] == []

    def test_опрос_во_время_удаления_карточек_не_заводит(self, monkeypatch):
        s = Scheduler()
        monkeypatch.setattr(sched_mod.weeek, "upcoming_meetings",
                            lambda *a, **k: [_meeting()])
        monkeypatch.setattr(sched_mod.snapshots, "load", lambda u: {})
        s.forget_team("alice")              # удаление идёт: задачи ещё стираются
        s._poll("alice", {"weeek_token": "t"})
        assert [k for k in s._states if k.startswith("alice:")] == []
        s.forget_done("alice")              # логин свободен — новый владелец живёт
        s._poll("alice", {"weeek_token": "t"})
        assert [k for k in s._states if k.startswith("alice:")]

    def _run_with(self, monkeypatch, tmp_path, *, during_upload=False,
                  during_field=False):
        from tests.test_meeting_outcomes import TestSchedulerReadsReason, _Slot, _state
        s = Scheduler()
        st = _state()
        s._states[st.key] = st
        rec = tmp_path / "rec.mp4"
        created = TestSchedulerReadsReason()._prepare(monkeypatch, tmp_path, {
            "ok": True, "path": str(rec), "reason": "stopped", "size": 1})
        rec.write_bytes(b"x")
        outcomes = []
        monkeypatch.setattr(sched_mod.stats, "record_meeting",
                            lambda st, o: outcomes.append(o))
        self.placed = []
        monkeypatch.setattr(sched_mod.cloud_folders, "placed",
                            lambda *a, **k: self.placed.append(a))

        def upload(*a, **k):
            if during_upload:
                s.forget_team("team")       # удаление посреди выгрузки (минуты)
            return {"ok": True, "url": "https://disk/x"}
        monkeypatch.setattr(sched_mod.delivery, "upload_with_retry", upload)
        if during_field:
            monkeypatch.setattr(sched_mod, "_protocol_wanted",
                                lambda cfg, user, title, log: s.forget_team("team") and False)
        s._run(st, _Slot())
        return created, outcomes, rec

    def test_удаление_во_время_выгрузки_не_создаёт_задачу(self, monkeypatch, tmp_path):
        created, outcomes, rec = self._run_with(monkeypatch, tmp_path, during_upload=True)
        assert created == {}, "задача распознавания создана для удалённой команды"
        assert outcomes == [], "исход встречи удалённой команды ушёл в метрику"
        # Память серии (раскладка по папкам) удалённой команды не пишется:
        # её строка в meeting_series досталась бы следующему владельцу логина.
        assert self.placed == []
        assert not rec.exists()

    def test_исход_встречи_удалённой_команды_не_пишется(self, monkeypatch):
        outcomes = []
        monkeypatch.setattr(sched_mod.stats, "record_meeting",
                            lambda st, o: outcomes.append(o))
        st = MeetingState(key="team:5:x", task_id="5", title="t", url="",
                          start=None, owner="team", state="missed", forgotten=True)
        Scheduler()._record_outcome(st, "missed")
        assert outcomes == []

    def test_удаление_перед_созданием_задачи(self, monkeypatch, tmp_path):
        created, _, rec = self._run_with(monkeypatch, tmp_path, during_field=True)
        assert created == {}
        assert not rec.exists()

    def test_задача_созданная_в_момент_удаления_убирается(self, monkeypatch, tmp_path):
        """Удаление между проверкой и store.create: задача уже создана —
        её надо убрать, иначе она достаётся следующему владельцу логина."""
        from tests.test_meeting_outcomes import TestSchedulerReadsReason, _Slot, _state
        s = Scheduler()
        st = _state()
        s._states[st.key] = st
        rec = tmp_path / "rec.mp4"
        TestSchedulerReadsReason()._prepare(monkeypatch, tmp_path, {
            "ok": True, "path": str(rec), "reason": "stopped", "size": 1})
        rec.write_bytes(b"x")
        made = []

        def create(**kw):
            job = _job(owner="team", jid="late-job", status="queued")
            made.append(job.id)
            s.forget_team("team")           # удаление пришло прямо сейчас
            return job
        monkeypatch.setattr(sched_mod.store, "create", create)
        s._run(st, _Slot())
        assert made == ["late-job"]
        assert store.get("late-job") is None, "задача удалённой команды осталась"

    def test_дозаливка_удалённой_команды_не_идёт(self, monkeypatch, tmp_path):
        """Мутант «_late_upload без forgotten»."""
        rec = tmp_path / "rec.mp4"
        rec.write_bytes(b"x")
        st = MeetingState(key="team:5:x", task_id="5", title="t", url="",
                          start=None, owner="team", state="done", forgotten=True)
        calls = []
        monkeypatch.setattr(sched_mod.delivery, "publish_or_upload",
                            lambda *a, **k: calls.append(1) or {"ok": True, "url": "u"})
        monkeypatch.setattr(sched_mod, "_meeting_place", lambda *a, **k: {"sub": None})
        Scheduler()._late_upload("team", st, {"weeek_token": "tok"}, str(rec),
                                 first_delay=0)
        assert calls == []

    def test_ожидатель_протокола_молчит_после_удаления(self, monkeypatch):
        comments = []
        monkeypatch.setattr(sched_mod.weeek, "add_comment",
                            lambda *a, **k: comments.append(a) or True)
        st = MeetingState(key="team:5:x", task_id="5", title="t", url="",
                          start=None, owner="team", state="transcribing",
                          forgotten=True)
        Scheduler()._await_and_upload_protocol(
            st, "no-such-job", {"weeek_token": "tok", "post_back_to_weeek": True})
        assert comments == [], "в Weeek ушёл ложный комментарий «задача пропала»"


# --------------------------------------------------------------------------- #
# 5. Безопасность
# --------------------------------------------------------------------------- #
class TestЛимитПопытокПароля:
    @pytest.mark.parametrize("path,body", [
        ("/api/profile/phone", {"phone": "+79991112233"}),
        ("/api/profile/telegram/link", {}),
        ("/api/profile/telegram/unlink", {}),
        ("/api/profile/delete", {}),
    ])
    def test_перебор_упирается_в_лимит(self, client, monkeypatch, path, body):
        from app import telegram
        monkeypatch.setattr(telegram, "configured", lambda: True)
        register(client)
        codes = [client.post(path, json={**body, "password": f"wrong-{i}"}).status_code
                 for i in range(security.BRUTE_MAX_ATTEMPTS + 2)]
        assert 429 in codes, codes
        # Лимит держит и верный пароль: иначе перебор просто продолжается.
        assert client.post(path, json={**body, "password": PWD}).status_code == 429

    def test_лимит_общий_для_всех_проверок_пароля(self, client):
        register(client)
        half = security.BRUTE_MAX_ATTEMPTS // 2
        for i in range(half):
            client.post("/api/profile/phone", json={"phone": "+79991112233",
                                                    "password": f"w{i}"})
        for i in range(security.BRUTE_MAX_ATTEMPTS - half):
            client.post("/api/profile/password", json={"new_password": "newpass123",
                                                       "old_password": f"w{i}"})
        r = client.post("/api/profile/delete", json={"password": "wrong"})
        assert r.status_code == 429

    def test_лимит_по_логину_а_не_только_по_ip(self, client, monkeypatch):
        """Мутант «лимит только по IP»: угнанную сессию гоняют с разных адресов."""
        import app.main as main_mod
        register(client)
        ips = iter(f"10.0.0.{i}" for i in range(1, 100))
        monkeypatch.setattr(main_mod, "_client_ip", lambda request: next(ips))
        codes = [client.post("/api/profile/password",
                             json={"new_password": "newpass123",
                                   "old_password": f"wrong-{i}"}).status_code
                 for i in range(security.BRUTE_MAX_ATTEMPTS + 2)]
        assert 429 in codes, codes

    def test_верный_пароль_без_перебора_проходит(self, client):
        register(client)
        r = client.post("/api/profile/phone", json={"phone": "+79991112233",
                                                    "password": PWD})
        assert r.status_code == 200, r.text


class TestСоседнийПоддомен:
    def test_запрос_с_сессией_с_поддомена_отклонён(self, client):
        register(client)
        r = client.post("/api/context", json={"text": "x"},
                        headers={"Sec-Fetch-Site": "same-site"})
        assert r.status_code == 403

    def test_поддомены_можно_разрешить_явно(self, client, monkeypatch):
        import app.main as main_mod
        monkeypatch.setattr(main_mod, "_TRUST_SAME_SITE", True)
        register(client)
        r = client.post("/api/context", json={"text": "x"},
                        headers={"Sec-Fetch-Site": "same-site"})
        assert r.status_code != 403

    def test_свой_сайт_проходит(self, client):
        register(client)
        r = client.post("/api/context", json={"text": "x"},
                        headers={"Sec-Fetch-Site": "same-origin"})
        assert r.status_code != 403


class TestВосстановлениеCSRF:
    def test_запрос_кода_с_чужого_сайта_отклонён(self, client):
        register(client); client.post("/api/auth/logout")
        sms.sent_messages.clear()
        r = client.post("/api/auth/recover/request",
                        json={"username": "alice", "phone": PHONE},
                        headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403
        assert sms.sent_messages == []

    def test_ввод_кода_с_чужого_сайта_отклонён(self, client):
        register(client); client.post("/api/auth/logout")
        r = client.post("/api/auth/recover/verify",
                        json={"username": "alice", "phone": PHONE, "code": "000000",
                              "new_password": "newpass123"},
                        headers={"Origin": "https://evil.example"})
        assert r.status_code == 403

    def test_запрос_кода_со_своего_сайта_проходит(self, client):
        register(client); client.post("/api/auth/logout")
        r = client.post("/api/auth/recover/request",
                        json={"username": "alice", "phone": PHONE},
                        headers={"Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 200


# --------------------------------------------------------------------------- #
# 7. Выжившие мутанты «Безопасность»
# --------------------------------------------------------------------------- #
class TestКодВосстановления:
    def _request(self, client):
        register(client); client.post("/api/auth/logout")
        sms.sent_messages.clear()
        client.post("/api/auth/recover/request",
                    json={"username": "alice", "phone": PHONE})
        return _last_code()

    def test_код_не_принимается_с_чужим_телефоном(self, client):
        code = self._request(client)
        r = client.post("/api/auth/recover/verify",
                        json={"username": "alice", "phone": "+79995556677",
                              "code": code, "new_password": "stolen-pass1"})
        assert r.status_code == 400
        assert login(client).status_code == 200

    def test_код_одноразовый(self, client):
        code = self._request(client)
        ok = client.post("/api/auth/recover/verify",
                         json={"username": "alice", "phone": PHONE, "code": code,
                               "new_password": "first-pass1"})
        assert ok.status_code == 200, ok.text
        again = client.post("/api/auth/recover/verify",
                            json={"username": "alice", "phone": PHONE, "code": code,
                                  "new_password": "second-pass1"})
        assert again.status_code == 400
        assert login(client, password="first-pass1").status_code == 200


class TestUsersJsonNull:
    def test_null_вместо_словаря_это_ошибка_а_не_пустой_сервер(self):
        p = config.DATA_DIR / "users.json"
        p.write_text("null", encoding="utf-8")
        with pytest.raises(security.UsersStoreError):
            security._load_users()
        assert p.read_text(encoding="utf-8") == "null"

    def test_регистрация_поверх_null_не_затирает_файл(self, client):
        p = config.DATA_DIR / "users.json"
        p.write_text("null", encoding="utf-8")
        r = register(client)
        assert r.status_code == 503
        assert p.read_text(encoding="utf-8") == "null"


class TestСсылкаТелемоста:
    @pytest.mark.parametrize("raw", [
        "https://telemost.evil.example/j/1",
        "https://evil-telemost.example/j/1",
        "https://telemost.yandex.ru.evil.example/j/1",
    ])
    def test_чужой_хост_со_словом_telemost(self, raw):
        assert telemost_url(raw) is None

    def test_обратный_слеш_не_уводит_на_чужой_хост(self):
        """Браузер читает «\\» как «/»: «https://evil\\@telemost…» для urlsplit —
        хост Телемоста, а для Chromium — evil. Наружу идёт только ссылка,
        собранная заново из разобранных частей."""
        out = telemost_url("https://evil.example\\@telemost.yandex.ru/j/1")
        assert out in (None, "https://telemost.yandex.ru/j/1")


class TestSSRFLoopback:
    def test_loopback_рядом_со_служебным_адресом_не_прячет_его(self, monkeypatch):
        """Мутант «loopback → return ''»: имя, резолвящееся в 127.0.0.1 И в
        169.254.169.254, проходило проверку по первому адресу."""
        monkeypatch.setattr(llm_custom.socket, "getaddrinfo", lambda host, port: [
            (2, 1, 6, "", ("127.0.0.1", 0)),
            (2, 1, 6, "", ("169.254.169.254", 0)),
        ])
        assert llm_custom.check_url("https://metadata.example/v1")
