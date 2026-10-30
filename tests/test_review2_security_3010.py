"""Адверсариальное ревью 30.10.2026, второй раунд
(docs/АДВЕРСАРИАЛЬНОЕ-РЕВЮ-2026-10-30-2.md), §2 «Удаление аккаунта и
безопасность»: находки и выжившие мутанты.

Каждый тест находки падал на `4fc23c0`; тесты-убийцы мутантов проходят на нём
и падают на соответствующем мутанте (см. CLAUDE.md, «Второй раунд ревью 30.10,
§2»).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

import app.main as main_mod
from app import config, jobs as jobs_mod, security, telegram
from app.automation import scheduler as sched_mod
from app.automation.recorder import browser
from app.automation.scheduler import MeetingState, Scheduler
from app.automation.scheduler import scheduler as global_sched
from app.jobs import STATUS_DONE, STATUS_RUNNING, Job, store
from app.main import app
from app.transcribe import Segment
from conftest import login, register

PWD = "password123"
P1, P2, P3 = "+79990000001", "+79990000002", "+79990000003"
MAX = security.BRUTE_MAX_ATTEMPTS


@pytest.fixture(autouse=True)
def _clean():
    yield
    with global_sched._lock:
        for k in [k for k, st in global_sched._states.items()
                  if st.owner in ("alice", "bob", "founder", "team")]:
            global_sched._states.pop(k, None)
        global_sched._forgetting.clear()
        global_sched._forgotten.clear()
    getattr(security, "_DELETING", set()).clear()
    telegram._LINK_CODES.clear()
    for t in ("alice", "bob", "founder"):
        browser.login_sessions.pop(t, None)
    for jid in [j for j in store._control if j.startswith("adv2-")]:
        store._control.pop(jid, None)


def _job(owner="alice", status=STATUS_RUNNING, jid="adv2-job-1", **kw) -> Job:
    """Задача прямо в хранилище, мимо очереди: настоящий воркер её не тронет."""
    job = Job(id=jid, filename="Планёрка.mp4", audio_path="/nonexistent/x.mp4",
              language="ru", diarize=False, owner=owner, status=status, **kw)
    store._jobs[job.id] = job
    return job


def _card(owner="alice", key=None, **kw) -> MeetingState:
    st = MeetingState(key=key or f"{owner}:1:x", task_id="1", title="Планёрка",
                      url="", start=None, owner=owner,
                      state=kw.pop("state", "recording"), **kw)
    global_sched._states[st.key] = st
    return st


def _alice_jobs() -> list:
    return [j for j in store._jobs.values() if j.owner == "alice"]


# --------------------------------------------------------------------------- #
# Сборка протокола после удаления (В)
# --------------------------------------------------------------------------- #
class TestПротоколПослеУдаления:
    @pytest.fixture
    def spies(self, monkeypatch):
        seen = {"series": [], "delivered": []}
        monkeypatch.setattr(jobs_mod.meeting_series, "remember",
                            lambda *a, **k: seen["series"].append("remember") or "k")
        monkeypatch.setattr(jobs_mod.meeting_series, "carry_over",
                            lambda *a, **k: seen["series"].append("carry") or [])
        monkeypatch.setattr(store, "_deliver_protocol",
                            lambda job, p: seen["delivered"].append(job.id))

        def transcribe(path, language=None, on_segment=None, on_start=None,
                       initial_prompt=None, model_name=None, **k):
            return ([Segment(start=0.0, end=2.0, text="обсудили бюджет")],
                    {"language": "ru", "duration": 2.0, "model": "medium"})
        monkeypatch.setattr(jobs_mod, "transcribe_file", transcribe)
        return seen

    @staticmethod
    def _result():
        return {"summary": "итоги", "decisions": ["секретное решение"],
                "_provider": "adv2"}

    def _job(self):
        return _job(status="queued", jid="adv2-proto", analyze=True,
                    deliver_protocol_cloud=True, created_at=time.time())

    def test_удаление_во_время_последнего_вызова_модели(self, spies, monkeypatch):
        """Память серии удалённой команды писалась под освободившимся логином:
        новый владелец видел её итоги и решения как «ПРОШЛАЯ ВСТРЕЧА»."""
        job = self._job()

        def last_call(job, txt, context, rerun=False):
            store.delete_owner("alice")      # удаление, пока модель отвечала
            return self._result()
        monkeypatch.setattr(store, "_protocol_pass", last_call)
        store._process(job)
        assert spies["series"] == [], "память серии записана после удаления"
        assert spies["delivered"] == [], "протокол доставлен после удаления"
        assert not store.docx_path(job.id, "adv2").exists()

    def test_удаление_во_время_сборки_word(self, spies, monkeypatch):
        """Проверка перед доставкой: Word собирается секунды, удаление могло
        прийти уже после записи памяти серии."""
        job = self._job()
        monkeypatch.setattr(store, "_protocol_pass",
                            lambda job, txt, context, rerun=False: self._result())
        from app import docx_export

        def report(out_path, **kw):
            Path(out_path).write_bytes(b"docx")
            store.delete_owner("alice")
        monkeypatch.setattr(docx_export, "generate_report", report)
        store._process(job)
        assert spies["delivered"] == []
        assert not store.docx_path(job.id, "adv2").exists()

    def test_пересборка_после_удаления(self, spies, monkeypatch):
        job = _job(status=STATUS_DONE, jid="adv2-re", analyze=True,
                   deliver_protocol_cloud=True)

        def last_call(job, txt, context, rerun=False):
            store.delete_owner("alice")
            return self._result()
        monkeypatch.setattr(store, "_protocol_pass", last_call)
        assert store._reanalyze_lock.acquire(timeout=5)
        store._do_reanalyze(job, "текст встречи")
        assert spies["series"] == [] and spies["delivered"] == []
        assert not store.docx_path(job.id, "adv2").exists()

    def test_пересборка_word_собран_после_удаления(self, spies, monkeypatch):
        """Word пересборки, дописанный уже после удаления, убирается: прежде
        `_finalise_gone` звал только воркер первого прогона."""
        job = _job(status=STATUS_DONE, jid="adv2-re2", analyze=True,
                   deliver_protocol_cloud=True)
        monkeypatch.setattr(store, "_protocol_pass",
                            lambda job, txt, context, rerun=False: self._result())
        from app import docx_export

        def report(out_path, **kw):
            store.delete_owner("alice")         # удалили, пока собирался Word
            Path(out_path).write_bytes(b"docx")
        monkeypatch.setattr(docx_export, "generate_report", report)
        assert store._reanalyze_lock.acquire(timeout=5)
        store._do_reanalyze(job, "текст встречи")
        assert spies["delivered"] == []
        assert not store.docx_path(job.id, "adv2").exists()


# --------------------------------------------------------------------------- #
# Хранилище задач: сбой удаления, индекс, расход
# --------------------------------------------------------------------------- #
class TestХранилищеЗадач:
    def test_сбой_записи_не_оставляет_задачи_отменёнными(self, monkeypatch):
        """Удаление, упавшее на записи хранилища: аккаунт остаётся, а его идущие
        задачи уже получили отмену и останавливались."""
        job = _job(jid="adv2-drop")
        ctrl = store._control.setdefault(job.id, {"pause": True, "cancel": False})

        def fail(*a, **k):
            raise OSError("диск полон")
        monkeypatch.setattr(store, "_save", fail)
        with pytest.raises(OSError):
            store.delete_owner("alice")
        assert store.get(job.id) is job
        assert ctrl == {"pause": True, "cancel": False}

    def test_индекс_поиска_не_пишется_для_удалённой_задачи(self, monkeypatch):
        """Мутант «index_search без проверки, что задача ещё есть»."""
        job = Job(id="adv2-gone", filename="x.mp4", audio_path="", language="ru",
                  diarize=False, owner="alice", status=STATUS_DONE,
                  analysis={"summary": "секрет"})
        saved, indexed = [], []
        monkeypatch.setattr(jobs_mod, "db", SimpleNamespace(
            enabled=lambda: True, search_save=lambda *a: saved.append(a)))
        from app import meeting_index
        monkeypatch.setattr(meeting_index, "index_job",
                            lambda st, j: indexed.append(j.id))
        store.index_search(job)
        assert saved == [] and indexed == []

    def test_расход_не_пишется_для_удалённой_задачи(self, monkeypatch):
        """Мутант «_add_usage без проверки, что задача ещё есть»."""
        job = Job(id="adv2-gone2", filename="x.mp4", audio_path="", language="ru",
                  diarize=False, owner="alice", status=STATUS_DONE)
        seen = []
        from app import events, stats
        monkeypatch.setattr(events, "record_many", lambda *a, **k: seen.append("ev"))
        monkeypatch.setattr(stats, "record", lambda j: seen.append("stats"))
        store._add_usage(job, {"calls": 1, "log": [{"model": "m"}]})
        assert seen == [] and not job.llm_usage


# --------------------------------------------------------------------------- #
# Порядок удаления и проверки «можно ли удалять»
# --------------------------------------------------------------------------- #
class TestПорядокИПроверки:
    def test_forget_team_раньше_delete_owner(self, client, monkeypatch):
        """Мутант «delete_owner до forget_team»: поток записи успевал создать
        задачу между ними."""
        register(client, phone=P1)
        order = []
        real = global_sched.forget_team
        monkeypatch.setattr(global_sched, "forget_team",
                            lambda t: order.append("forget") or real(t))
        monkeypatch.setattr(store, "delete_owner",
                            lambda o: order.append("delete") or 0)
        r = client.post("/api/profile/delete", json={"password": PWD})
        assert r.status_code == 200, r.text
        assert order == ["forget", "delete"]

    def test_админ_с_участниками_отказ_до_очистки(self, client):
        """Мутант «без первой проверки _deletion_blocker»: задачи и карточки
        стирались, а потом удаление всё равно отклонялось."""
        register(client, phone=P1)
        code = security.invite_code_of("alice")
        assert register(TestClient(app), "bob", code=code, phone=P2).status_code == 200
        job = _job(status=STATUS_DONE)
        card = _card()
        r = client.post("/api/profile/delete", json={"password": PWD})
        assert r.status_code == 400 and "участник" in r.json()["detail"]
        assert store.get(job.id) is job, "задачи стёрты, хотя удаление отклонено"
        assert global_sched._states.get(card.key) is card
        assert not card.forgotten and not card.stop_flag

    def test_регистрация_во_время_очистки_основателя(self, client, monkeypatch):
        """Отказ ПОСЛЕ очистки: основатель-единственный аккаунт удалял себя,
        в это время кто-то регистрировался — 400 «Вы — основатель», а задачи
        уже стёрты. Теперь регистрация ждёт конца удаления."""
        register(client, "founder", phone=P1)
        assert security.is_super_admin("founder")
        seen = {}

        def delete_owner(owner):
            seen["reg"] = security.create_user("intruder", PWD, phone=P2)
            return 0
        monkeypatch.setattr(store, "delete_owner", delete_owner)
        r = client.post("/api/profile/delete", json={"password": PWD})
        assert not seen["reg"]["ok"], "регистрация прошла посреди удаления"
        assert r.status_code == 200, r.text
        assert not security.user_exists("founder")
        assert not security.user_exists("intruder")

    def test_участник_не_входит_в_удаляемую_команду(self, client, monkeypatch):
        register(client, phone=P1)
        code = security.invite_code_of("alice")
        seen = {}

        def delete_owner(owner):
            seen["reg"] = security.create_user("bob", PWD, code=code, phone=P2)
            return 0
        monkeypatch.setattr(store, "delete_owner", delete_owner)
        r = client.post("/api/profile/delete", json={"password": PWD})
        assert not seen["reg"]["ok"]
        assert r.status_code == 200, r.text
        assert not security.user_exists("bob")

    def test_логин_не_занять_пока_стираются_данные(self, client, monkeypatch):
        """Логин освобождался до удаления папки и строк: зарегистрировавшийся в
        это окно терял свои данные вместе с данными прежнего владельца."""
        register(client, phone=P1)
        real = security._delete_user_data
        seen = {}

        def wipe(u):
            seen["reg"] = security.create_user("alice", PWD, phone=P2)
            real(u)
        monkeypatch.setattr(security, "_delete_user_data", wipe)
        r = client.post("/api/profile/delete", json={"password": PWD})
        assert r.status_code == 200, r.text
        assert not seen["reg"]["ok"]


# --------------------------------------------------------------------------- #
# Пересекающиеся запросы удаления: forget_done по запросу (В)
# --------------------------------------------------------------------------- #
class TestПересекающиесяУдаления:
    def _overlap(self, client, monkeypatch, password):
        register(client, phone=P1)
        other = TestClient(app)
        assert login(other).status_code == 200
        card = _card()
        real = store.delete_owner
        codes, nested = [], []

        def delete_owner(owner):
            if not nested:                  # вторая вкладка — один раз
                nested.append(1)
                codes.append(other.post("/api/profile/delete",
                                        json={"password": password}).status_code)
            return real(owner)
        monkeypatch.setattr(store, "delete_owner", delete_owner)
        r = client.post("/api/profile/delete", json={"password": PWD})
        return r, codes, card

    def test_неверный_пароль_во_второй_вкладке(self, client, monkeypatch):
        r, codes, card = self._overlap(client, monkeypatch, "wrong-pass")
        assert codes == [400]
        assert r.status_code == 200, r.text
        assert card.key not in global_sched._states, "карточка удалённой команды вернулась"
        assert card.forgotten

    def test_вторая_вкладка_с_верным_паролем(self, client, monkeypatch):
        r, codes, card = self._overlap(client, monkeypatch, PWD)
        assert codes == [400], "второе удаление того же аккаунта не отклонено"
        assert r.status_code == 200, r.text
        assert card.key not in global_sched._states and card.forgotten

    def test_сбой_одного_запроса_не_возвращает_карточки_другого(self):
        s = Scheduler()
        st = MeetingState(key="alice:1:x", task_id="1", title="t", url="",
                          start=None, owner="alice", state="recording")
        s._states[st.key] = st
        t1 = s.forget_team("alice")
        t2 = s.forget_team("alice")
        s.forget_done("alice", t1, restore=True)    # сорвался первый запрос
        assert st.key not in s._states and st.forgotten
        s.forget_done("alice", t2, restore=True)    # и второй — аккаунт остался
        assert s._states.get(st.key) is st and not st.forgotten

    def test_сбой_удаления_возвращает_карточки(self, client, monkeypatch):
        """Мутант «forget_done без восстановления»."""
        register(client, phone=P1)
        card = _card()

        def fail(owner):
            raise RuntimeError("база недоступна")
        monkeypatch.setattr(store, "delete_owner", fail)
        r = client.post("/api/profile/delete", json={"password": PWD})
        assert r.status_code >= 500
        assert global_sched._states.get(card.key) is card and not card.forgotten
        assert security.user_exists("alice")

    def test_откат_перезапускает_дозаливку(self, monkeypatch, tmp_path):
        """Дозаливка видит forgotten и выходит; удаление сорвалось — видео
        должно уехать в облако, а не дождаться уборки через двое суток."""
        rec, part = tmp_path / "r.mp4", tmp_path / "p.mp4"
        rec.write_bytes(b"x")
        part.write_bytes(b"x")
        s = Scheduler()
        st = MeetingState(key="alice:1:x", task_id="1", title="t", url="",
                          start=None, owner="alice", state="done",
                          out_path=str(rec), upload_error="облако недоступно",
                          parts=[{"out_path": str(part)}])
        s._states[st.key] = st
        started = []
        monkeypatch.setattr(s, "_start_late_upload",
                            lambda team, st, cfg, out, first_delay=300:
                            started.append(out) or True)
        monkeypatch.setattr(sched_mod.auto_settings, "load", lambda t: {})
        t = s.forget_team("alice")
        s.forget_done("alice", t, restore=True)
        assert sorted(started) == sorted([str(rec), str(part)])


# --------------------------------------------------------------------------- #
# Планировщик: опрос, восстановление после рестарта, дозаливка, ожидатель
# --------------------------------------------------------------------------- #
def _meeting(task_id="7"):
    from datetime import datetime, timedelta, timezone
    return SimpleNamespace(task_id=task_id, title="Встреча", raw={},
                           url="https://telemost.yandex.ru/j/1",
                           start=datetime.now(timezone.utc) + timedelta(hours=1))


class TestОпросИЭпоха:
    def test_удаление_целиком_прошло_пока_шёл_запрос(self, monkeypatch):
        """Мутанты «без epoch++» и «эпоха читается после запроса к Weeek»:
        удаление началось и закончилось, пока Weeek отвечал, — `_forgetting`
        уже пуст, и отличить это можно только по эпохе."""
        s = Scheduler()

        def upcoming(*a, **k):
            s.forget_done("alice", s.forget_team("alice"))
            return [_meeting()]
        monkeypatch.setattr(sched_mod.weeek, "upcoming_meetings", upcoming)
        monkeypatch.setattr(sched_mod.snapshots, "load", lambda u: {})
        s._poll("alice", {"weeek_token": "t"})
        assert [k for k in s._states if k.startswith("alice:")] == []


class TestВосстановлениеПослеРестарта:
    KEY = "team:5:2026-10-30T10:00:00+00:00"

    def _env(self, monkeypatch, tmp_path, s, on_load=None):
        rec = tmp_path / "r.mp4"
        rec.write_bytes(b"x")
        snap = {"state": "done", "out_path": str(rec), "title": "Планёрка",
                "upload_error": "облако недоступно", "saved_at": time.time()}
        monkeypatch.setattr(sched_mod.security, "list_teams", lambda: ["team"])
        monkeypatch.setattr(sched_mod.auto_settings, "load", lambda t: {})

        def load(team):
            if on_load:
                on_load()
            return {self.KEY: dict(snap)}
        monkeypatch.setattr(sched_mod.snapshots, "load", load)
        started = []
        monkeypatch.setattr(s, "_start_late_upload",
                            lambda team, st, cfg, out, first_delay=300:
                            started.append(out) or True)
        return started, rec

    def test_не_поднимает_карточки_удаляемой_команды(self, monkeypatch, tmp_path):
        s = Scheduler()
        s.forget_team("team")
        started, _ = self._env(monkeypatch, tmp_path, s)
        s._resume_pending()
        assert self.KEY not in s._states
        assert started == [], "дозаливка удаляемой команды запущена"

    def test_удаление_прошло_пока_читались_снапшоты(self, monkeypatch, tmp_path):
        s = Scheduler()
        started, _ = self._env(monkeypatch, tmp_path, s, on_load=lambda: s.forget_done(
            "team", s.forget_team("team")))
        s._resume_pending()
        assert self.KEY not in s._states and started == []

    def test_сорвавшееся_удаление_возвращает_поднятую_карточку(self, monkeypatch, tmp_path):
        s = Scheduler()
        token = s.forget_team("team")
        started, rec = self._env(monkeypatch, tmp_path, s)
        s._resume_pending()
        s.forget_done("team", token, restore=True)
        assert self.KEY in s._states and not s._states[self.KEY].forgotten
        assert started == [str(rec)]


class TestОсиротевшаяЗапись:
    """`_finalize_orphan` (запись, прерванная рестартом) — двойник `_run`: без
    проверок `forgotten` заливал видео, писал раскладку и создавал задачу под
    освободившимся логином."""

    def _run(self, monkeypatch, tmp_path, *, forgotten=False, on_upload=None,
             on_create=None, on_field=None):
        rec = tmp_path / "orphan.mp4"
        rec.write_bytes(b"x")
        s = Scheduler()
        st = MeetingState(key="team:5:x", task_id="5", title="Планёрка", url="",
                          start=None, owner="team", state="transcribing",
                          forgotten=forgotten)
        seen = {"upload": 0, "placed": 0, "field": 0, "created": []}
        monkeypatch.setattr(sched_mod, "_meeting_place", lambda *a, **k: {"sub": None})
        monkeypatch.setattr(sched_mod, "_protocol_wanted", lambda *a, **k: True)

        def upload(*a, **k):
            seen["upload"] += 1
            if on_upload:
                on_upload(st)
            return {"ok": True, "url": "https://disk/x", "path": "/x"}
        monkeypatch.setattr(sched_mod.delivery, "upload_with_retry", upload)
        def field(*a, **k):
            seen["field"] += 1
            if on_field:
                on_field(st)
        monkeypatch.setattr(sched_mod.delivery, "write_weeek_field", field)
        monkeypatch.setattr(sched_mod.cloud_folders, "placed",
                            lambda *a, **k: seen.__setitem__("placed", seen["placed"] + 1))

        def create(**kw):
            job = _job(owner="team", jid="adv2-orphan", status="queued")
            seen["created"].append(job.id)
            if on_create:
                on_create(st)
            return job
        monkeypatch.setattr(sched_mod.store, "create", create)
        monkeypatch.setattr(s, "_set", lambda *a, **k: None)
        cfg = {"weeek_token": "t", "weeek_video_field": "Видео встречи",
               "do_transcribe": True, "upload_protocol": False}
        s._finalize_orphan(st, cfg, str(rec))
        return seen, rec

    def test_поднятая_во_время_удаления(self, monkeypatch, tmp_path):
        seen, rec = self._run(monkeypatch, tmp_path, forgotten=True)
        assert seen["upload"] == 0 and seen["placed"] == 0 and seen["created"] == []
        assert not rec.exists()

    def test_удаление_во_время_выгрузки(self, monkeypatch, tmp_path):
        seen, rec = self._run(monkeypatch, tmp_path,
                              on_upload=lambda st: setattr(st, "forgotten", True))
        assert seen["placed"] == 0 and seen["field"] == 0
        assert seen["created"] == [], "задача создана для удалённой команды"
        assert not rec.exists()

    def test_удаление_во_время_записи_поля_weeek(self, monkeypatch, tmp_path):
        """Проверка перед созданием задачи: удаление между выгрузкой и
        `store.create` — задачи нет вовсе (а не создана и стёрта)."""
        seen, rec = self._run(monkeypatch, tmp_path,
                              on_field=lambda st: setattr(st, "forgotten", True))
        assert seen["field"] == 1
        assert seen["created"] == []
        assert not rec.exists()

    def test_удаление_в_момент_создания_задачи(self, monkeypatch, tmp_path):
        seen, _ = self._run(monkeypatch, tmp_path,
                            on_create=lambda st: setattr(st, "forgotten", True))
        assert seen["created"] == ["adv2-orphan"]
        assert store.get("adv2-orphan") is None, "задача удалённой команды осталась"


class TestДозаливкаИОжидатель:
    def test_раскладка_папок_после_проверки_forgotten(self, monkeypatch, tmp_path):
        """`cloud_folders.placed` писал память серии удалённой команды ДО
        проверки; заодно мутант «без второй проверки forgotten» (Weeek)."""
        rec = tmp_path / "rec.mp4"
        rec.write_bytes(b"x")
        st = MeetingState(key="team:5:x", task_id="5", title="t", url="",
                          start=None, owner="team", state="done", out_path=str(rec))
        seen = []
        monkeypatch.setattr(sched_mod, "_meeting_place", lambda *a, **k: {"sub": None})

        def publish(*a, **k):
            st.forgotten = True                 # удалили, пока шла выгрузка
            return {"ok": True, "url": "https://disk/x", "path": "/x"}
        monkeypatch.setattr(sched_mod.delivery, "publish_or_upload", publish)
        monkeypatch.setattr(sched_mod.cloud_folders, "placed",
                            lambda *a, **k: seen.append("placed"))
        monkeypatch.setattr(sched_mod.delivery, "write_weeek_field",
                            lambda *a, **k: seen.append("field"))
        monkeypatch.setattr(sched_mod.weeek, "add_comment",
                            lambda *a, **k: seen.append("comment"))
        monkeypatch.setattr(sched_mod.snapshots, "save", lambda st: seen.append("snap"))
        Scheduler()._late_upload(
            "team", st, {"weeek_token": "tok", "weeek_video_field": "Видео",
                         "post_back_to_weeek": True}, str(rec), first_delay=0)
        assert seen == []

    def test_ожидатель_проверяет_forgotten_после_цикла(self, monkeypatch):
        """Мутант «без второй проверки forgotten в ожидателе протокола»."""
        s = Scheduler()
        st = MeetingState(key="team:5:x", task_id="5", title="t", url="",
                          start=None, owner="team", state="transcribing")
        job = SimpleNamespace(status="done", docx_providers=["x"], delivery_error="",
                              protocol_cloud_url=None, analysis_error="")
        n = {"get": 0}

        def get(jid):
            n["get"] += 1
            if n["get"] >= 2:
                st.forgotten = True             # удалили между проверками
            return job
        monkeypatch.setattr(sched_mod.store, "get", get)
        seen = []
        from app.automation import notify
        monkeypatch.setattr(notify, "send", lambda *a, **k: seen.append("tg"))
        monkeypatch.setattr(sched_mod.weeek, "add_comment",
                            lambda *a, **k: seen.append("weeek"))
        monkeypatch.setattr(s, "_set", lambda *a, **k: seen.append("set"))
        s._await_and_upload_protocol(st, "j", {"weeek_token": "tok",
                                               "post_back_to_weeek": True})
        assert seen == []


# --------------------------------------------------------------------------- #
# Загрузка, начатая до удаления (С)
# --------------------------------------------------------------------------- #
class TestЗагрузкаПриУдалении:
    @pytest.fixture(autouse=True)
    def _no_worker(self, monkeypatch):
        """Созданную загрузкой задачу настоящий воркер не берёт."""
        import queue
        monkeypatch.setattr(store, "_queue", queue.Queue())

    @staticmethod
    def _upload(c):
        # analyze=true: ключи команды читаются уже ПОСЛЕ приёма файла — там
        # первый тест и удаляет аккаунт.
        return c.post("/api/jobs", data={"language": "ru", "analyze": "true"},
                      files={"file": ("встреча.wav", b"RIFF0000WAVE", "audio/wav")})

    def test_аккаунт_удалили_пока_шла_загрузка(self, client, monkeypatch):
        register(client, phone=P1)

        def creds(user):
            security.delete_account("alice", PWD, before_release=store.delete_owner)
            return {}
        monkeypatch.setattr(main_mod.user_creds, "load", creds)
        r = self._upload(client)
        assert r.status_code != 201, r.text
        assert _alice_jobs() == [], "задача создана под освободившимся логином"

    def test_загрузка_посреди_удаления(self, client, monkeypatch):
        """Задача создана ПОСЛЕ delete_owner, но до освобождения логина."""
        register(client, phone=P1)
        uploader = TestClient(app)
        assert login(uploader).status_code == 200
        real = store.delete_owner
        codes = []

        def delete_owner(owner):
            n = real(owner)
            codes.append(self._upload(uploader).status_code)
            return n
        monkeypatch.setattr(store, "delete_owner", delete_owner)
        r = client.post("/api/profile/delete", json={"password": PWD})
        assert r.status_code == 200, r.text
        assert codes and codes[0] != 201
        assert _alice_jobs() == []


# --------------------------------------------------------------------------- #
# Код привязки Telegram и окно входа в Яндекс (С)
# --------------------------------------------------------------------------- #
@pytest.fixture
def tg(monkeypatch):
    from tests.test_telegram_recovery import _FakeApi
    monkeypatch.setenv("VTX_TELEGRAM_BOT_TOKEN", "123:abc")
    fake = _FakeApi()
    monkeypatch.setattr(telegram, "_api", fake)
    monkeypatch.setattr(telegram, "_ME", None)
    telegram._LINK_CODES.clear()
    yield fake
    telegram._LINK_CODES.clear()


def _start(chat_id, text):
    return {"update_id": 7, "message": {"text": text, "chat": {"id": chat_id},
                                        "from": {"username": "ivan"}}}


class TestКодПривязкиTelegram:
    def test_код_гаснет_при_удалении(self, client, tg):
        register(client, phone=P1)
        r = client.post("/api/profile/telegram/link", json={"password": PWD})
        code = r.json()["code"]
        assert client.post("/api/profile/delete", json={"password": PWD}).status_code == 200
        assert code not in telegram._LINK_CODES, "код удалённого аккаунта жив"
        register(TestClient(app), "alice", phone=P2)
        telegram.handle_update(_start(777, f"/start {code}"))
        assert security.telegram_of("alice") == {}

    def test_код_прежнего_владельца_логина_не_подходит(self, client, tg):
        """Код выдан параллельным запросом, пока шло удаление (после чистки
        кодов): он должен быть привязан к аккаунту, а не к строке логина."""
        register(client, phone=P1)
        code = telegram.new_link_code("alice")
        assert security.delete_account("alice", PWD)["ok"]
        time.sleep(0.01)
        register(TestClient(app), "alice", phone=P2)
        telegram.handle_update(_start(777, f"/start {code}"))
        assert security.telegram_of("alice") == {}, \
            "чат прежнего владельца привязан к новому аккаунту"


class TestОкноВходаЯндекса:
    @staticmethod
    def _ses(closed):
        return SimpleNamespace(close=lambda wait=10.0: closed.append(1),
                               alive=lambda: True, expired=lambda now=None: "",
                               touch=lambda: None)

    def test_окно_удалённой_команды_закрывается_до_освобождения(self, client, monkeypatch):
        register(client, phone=P1)
        closed = []
        browser.login_sessions["alice"] = self._ses(closed)
        seen = []
        real = store.delete_owner
        monkeypatch.setattr(store, "delete_owner",
                            lambda o: seen.append(browser.login_sessions.get(o)) or real(o))
        assert client.post("/api/profile/delete", json={"password": PWD}).status_code == 200
        assert closed and "alice" not in browser.login_sessions
        assert seen == [None], "окно ещё открыто, пока стираются данные"

    def test_окно_открытое_посреди_удаления(self, client, monkeypatch):
        register(client, phone=P1)
        closed = []
        real = store.delete_owner

        def delete_owner(o):
            browser.login_sessions[o] = self._ses(closed)   # параллельный запрос
            return real(o)
        monkeypatch.setattr(store, "delete_owner", delete_owner)
        assert client.post("/api/profile/delete", json={"password": PWD}).status_code == 200
        assert closed and "alice" not in browser.login_sessions


# --------------------------------------------------------------------------- #
# Лимит попыток пароля (С) и его выжившие мутанты
# --------------------------------------------------------------------------- #
class TestЛимитПароля:
    def _parallel(self, monkeypatch, make_client, path, body, n=32):
        gate = threading.Event()
        calls, codes = [], []
        lock = threading.Lock()

        def slow_verify(username, password):
            with lock:
                calls.append(1)
            gate.wait(5)                 # медленный хэш: все запросы внутри
            return False
        monkeypatch.setattr(security, "verify_user", slow_verify)

        def one():
            r = make_client().post(path, json=body)
            with lock:
                codes.append(r.status_code)
        threads = [threading.Thread(target=one) for _ in range(n)]
        for t in threads:
            t.start()
        deadline = time.time() + 5
        while len(codes) < n - MAX and time.time() < deadline:
            time.sleep(0.02)
        gate.set()
        for t in threads:
            t.join(15)
        return calls, codes

    @pytest.mark.parametrize("path,body", [
        ("/api/profile/phone", {"phone": "+79991112233", "password": "wrong"}),
        ("/api/profile/delete", {"password": "wrong"}),
    ])
    def test_параллельные_запросы_профиля(self, client, monkeypatch, path, body):
        register(client, phone=P1)
        cookies = dict(client.cookies)

        def make():
            c = TestClient(app)
            c.cookies.update(cookies)
            return c
        calls, codes = self._parallel(monkeypatch, make, path, body)
        assert len(calls) <= MAX, f"проверок пароля: {len(calls)}"
        assert codes.count(429) >= 32 - MAX, codes

    def test_параллельный_вход(self, client, monkeypatch):
        register(client, phone=P1)
        calls, codes = self._parallel(
            monkeypatch, lambda: TestClient(app), "/api/auth/login",
            {"username": "alice", "password": "wrong"})
        assert len(calls) <= MAX, f"проверок пароля: {len(calls)}"
        assert codes.count(429) >= 32 - MAX, codes

    def test_вход_лимит_по_логину_с_разных_ip(self, client, monkeypatch):
        register(client, phone=P1)
        client.post("/api/auth/logout")
        ips = iter(f"10.1.{i // 250}.{i % 250 + 1}" for i in range(10_000))
        monkeypatch.setattr(main_mod, "_client_ip", lambda request: next(ips))
        codes = [login(client, password=f"wrong-{i}").status_code
                 for i in range(MAX + 2)]
        assert 429 in codes, codes

    def test_лимит_по_ip_для_разных_логинов(self):
        """Мутант «лимит только по логину»: с одного адреса перебирают пароли
        нескольких угнанных сессий."""
        a = TestClient(app)
        register(a, "alice", phone=P1)
        b = TestClient(app)
        register(b, "bob", phone=P2)
        half = MAX // 2
        for i in range(half):
            a.post("/api/profile/phone", json={"phone": "+79991112233", "password": f"w{i}"})
        codes = [b.post("/api/profile/phone",
                        json={"phone": "+79991112234", "password": f"w{i}"}).status_code
                 for i in range(MAX - half + 1)]
        assert 429 in codes, codes

    def test_успех_сбрасывает_счётчик(self, client):
        """Мутант «_pwd_outcome без throttle_clear»."""
        register(client, phone=P1)
        for i in range(MAX - 1):
            client.post("/api/profile/phone", json={"phone": "+79991112233",
                                                    "password": f"w{i}"})
        ok = client.post("/api/profile/phone", json={"phone": "+79991112233",
                                                     "password": PWD})
        assert ok.status_code == 200, ok.text
        codes = [client.post("/api/profile/phone",
                             json={"phone": "+79991112233", "password": f"x{i}"}).status_code
                 for i in range(MAX - 1)]
        assert 429 not in codes, codes

    def test_сбой_удаления_не_считается_попыткой(self, client, monkeypatch):
        """500 при удалении (база недоступна) — не перебор пароля: бронь
        попытки возвращается, иначе после нескольких сбоев владелец получал 429
        с верным паролем."""
        register(client, phone=P1)

        def fail(owner):
            raise RuntimeError("база недоступна")
        monkeypatch.setattr(store, "delete_owner", fail)
        codes = [client.post("/api/profile/delete", json={"password": PWD}).status_code
                 for _ in range(MAX + 1)]
        assert 429 not in codes, codes
        ok = client.post("/api/profile/phone", json={"phone": "+79991112233",
                                                     "password": PWD})
        assert ok.status_code == 200, ok.text

    def test_кривой_номер_не_считается_попыткой(self, client):
        """Мутант «считать любую ошибку»: верный пароль с опечаткой в номере —
        не перебор."""
        register(client, phone=P1)
        codes = [client.post("/api/profile/phone",
                             json={"phone": "123", "password": PWD}).status_code
                 for _ in range(MAX + 2)]
        assert 429 not in codes, codes
        ok = client.post("/api/profile/phone", json={"phone": "+79991112233",
                                                     "password": PWD})
        assert ok.status_code == 200, ok.text


class TestФормыИПоддомены:
    def test_вход_с_поддомена_отклонён_даже_при_доверии(self, client, monkeypatch):
        """Мутант «формы доверяют поддоменам при VTX_TRUST_SUBDOMAINS=1»: соседний
        сайт на kreativdelo.ru логинил бы браузер жертвы в чужой аккаунт."""
        monkeypatch.setattr(main_mod, "_TRUST_SAME_SITE", True)
        register(client, phone=P1)
        client.post("/api/auth/logout")
        r = client.post("/api/auth/login", json={"username": "alice", "password": PWD},
                        headers={"Sec-Fetch-Site": "same-site"})
        assert r.status_code == 403
