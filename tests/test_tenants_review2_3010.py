"""Адверсариальное ревью 30.10, второй раунд, раздел 3: разделение клиентов.

Каждый тест падал на `4fc23c0` (или убивает выжившего мутанта — помечено
«мутант»). Фикстуры — `_client_team` из tests/test_tenants_3010.py (основатель
alice, клиент bob под замком, доска 1, Яндекс.Диск) и `_founder_with_bob` из
tests/test_tenants_review_3010.py (bob без замка).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app import config, security
from app import main as main_mod
from app.automation import clouds
from app.automation import scheduler as sched_mod
from app.automation import settings as auto_settings
from app.automation import weeek
from app.automation.clouds import local as local_cloud
from app.automation.recorder import browser
from app.automation.scheduler import Scheduler
from app.jobs import Job, store
from conftest import login, register

from test_tenants_3010 import TWO_BOARDS, URL1, _client_team, _FakeWeeek, _task
from test_tenants_review_3010 import _founder_with_bob, _logout


def _cfg(board=1, **extra):
    return {"weeek_token": "t", "weeek_project_id": 10,
            "weeek_board_id": board, **extra}


# --------------------------------------------------------------------------- #
# В: «Подключиться снова» на карточке задачи, ушедшей на чужую доску
# --------------------------------------------------------------------------- #
class TestПодключитьсяСнова:
    def _done_card_moved_away(self, monkeypatch):
        tasks = [dict(t) for t in TWO_BOARDS]
        monkeypatch.setattr(weeek, "_request", _FakeWeeek(tasks, honour_board=True))
        s = Scheduler()
        s._poll("bob", _cfg())
        st = next(iter(s._states.values()))
        st.state = "done"                       # бот уже записал эту встречу
        tasks[0]["boardId"] = 2                 # задачу перенесли к другому клиенту
        s._poll("bob", _cfg())
        launched = []
        monkeypatch.setattr(s, "_launch_manual",
                            lambda team, st, **k: launched.append(st.task_id)
                            or {"ok": True})
        return s, st, launched

    @pytest.mark.parametrize("state", ["done", "error"])
    def test_задача_ушла_с_доски_бот_не_идёт(self, monkeypatch, state):
        s, st, launched = self._done_card_moved_away(monkeypatch)
        st.state = state
        res = s.run_now("bob", "101")
        assert res["ok"] is False
        assert launched == []

    def test_задача_на_доске_заход_разрешён(self, monkeypatch):
        monkeypatch.setattr(weeek, "_request", _FakeWeeek(TWO_BOARDS, honour_board=True))
        s = Scheduler()
        s._poll("bob", _cfg())
        next(iter(s._states.values())).state = "done"
        launched = []
        monkeypatch.setattr(s, "_launch_manual",
                            lambda team, st, **k: launched.append(st.task_id) or {"ok": True})
        assert s.run_now("bob", "101")["ok"] is True
        assert launched == [101]

    def test_встреча_по_ссылке_не_проверяется(self, monkeypatch):
        s = Scheduler()
        st = sched_mod.MeetingState(key="bob:link-1:x", task_id="link-20261030-ab",
                                    title="t", url=URL1, start=None, owner="bob")
        st.state = "done"
        s._states[st.key] = st
        monkeypatch.setattr(s, "_launch_manual", lambda team, st, **k: {"ok": True})
        assert s.run_now("bob", "link-20261030-ab")["ok"] is True


# --------------------------------------------------------------------------- #
# С: «Прикрепить заново» и пересборка без задачи — в сохранённую чужую задачу
# --------------------------------------------------------------------------- #
def _bob_job(job_id="j-bob", task="102"):
    job = Job(id=job_id, filename="x.wav", audio_path="x", language="ru",
              diarize=False, owner="bob", status="done", created_at=time.time())
    job.deliver_weeek_task = task
    store._jobs[job.id] = job
    return job


class TestСохранённаяЗадачаДоставки:
    def test_прикрепить_заново_в_чужую_задачу_403(self, client, monkeypatch):
        _client_team(client, monkeypatch)
        _bob_job()
        called = []
        monkeypatch.setattr(main_mod.store, "redeliver",
                            lambda *a, **k: called.append(a) or "delivered")
        r = client.post("/api/jobs/j-bob/redeliver")
        assert r.status_code == 403
        assert called == []

    def test_прикрепить_заново_в_свою_задачу(self, client, monkeypatch):
        _client_team(client, monkeypatch)
        _bob_job(task="101")
        monkeypatch.setattr(main_mod.store, "redeliver", lambda *a, **k: "delivered")
        assert client.post("/api/jobs/j-bob/redeliver").status_code == 200

    def test_пересборка_без_задачи_проверяет_сохранённую(self, client, monkeypatch):
        _client_team(client, monkeypatch)
        job = _bob_job()
        called = []
        monkeypatch.setattr(main_mod.store, "reanalyze",
                            lambda *a, **k: called.append(k) or job)
        r = client.post("/api/jobs/j-bob/reanalyze", json={})
        assert r.status_code == 403
        assert called == []
        # Очистить задачу доставки — можно.
        r = client.post("/api/jobs/j-bob/reanalyze", json={"deliver_weeek_task": ""})
        assert r.status_code == 200, r.text
        assert called[0]["deliver_weeek_task"] == ""

    def test_фоновая_доставка_не_пишет_в_чужую_задачу(self, client, monkeypatch, tmp_path):
        """`_resume_pending` и `_late_upload` зовут `store.redeliver` без
        человека — проверка стоит в самой доставке."""
        _client_team(client, monkeypatch)
        job = _bob_job()
        job.protocol_cloud_url = "https://disk.yandex.ru/d/x"
        writes = []
        monkeypatch.setattr(weeek, "set_custom_field",
                            lambda tok, tid, *a, **k: writes.append(tid) or {"ok": True})
        monkeypatch.setattr(weeek, "add_comment",
                            lambda tok, tid, *a, **k: writes.append(tid) or True)
        store._deliver_protocol(job, tmp_path / "p.docx")
        assert writes == []
        assert "доск" in (job.delivery_error or "")
        job.deliver_weeek_task = "101"
        store._deliver_protocol(job, tmp_path / "p.docx")
        assert writes and set(map(str, writes)) == {"101"}

    def test_без_замка_доставка_как_раньше(self, client, monkeypatch, tmp_path):
        _founder_with_bob(client, monkeypatch, weeek_token="tok-bob")
        job = _bob_job()
        job.protocol_cloud_url = "https://disk.yandex.ru/d/x"
        writes = []
        monkeypatch.setattr(weeek, "set_custom_field",
                            lambda tok, tid, *a, **k: writes.append(tid) or {"ok": True})
        store._deliver_protocol(job, tmp_path / "p.docx")
        assert writes == ["102"]


# --------------------------------------------------------------------------- #
# С: старые `local_dir` и абсолютная `protocol_folder`
# --------------------------------------------------------------------------- #
class TestЛокальноеОблакоМиграция:
    def _bob(self, client):
        register(client)
        register(client, "bob", phone="+7 999 000-00-01")

    def test_старая_папка_другой_команды_не_используется(self, client):
        self._bob(client)
        alice_dir = config.DATA_DIR / "recordings" / "alice"
        auto_settings.save("bob", {"local_dir": str(alice_dir)})
        cfg = clouds._backend_cfg(auto_settings.load("bob"), "local", "bob")
        assert local_cloud.base_dir(cfg) == config.DATA_DIR / "recordings" / "bob"

    @pytest.mark.parametrize("rel", ["recordings", "users/alice/x", "recordings/alice/sub"])
    def test_общая_папка_и_подпапки_чужих_тоже(self, client, rel):
        self._bob(client)
        auto_settings.save("bob", {"local_dir": str(config.DATA_DIR / rel)})
        cfg = clouds._backend_cfg(auto_settings.load("bob"), "local", "bob")
        assert local_cloud.base_dir(cfg) == config.DATA_DIR / "recordings" / "bob"

    def test_загрузка_ложится_в_свою_папку(self, client, tmp_path):
        self._bob(client)
        auto_settings.save("bob", {"local_dir": str(config.DATA_DIR / "recordings" / "alice"),
                                   "cloud": "local"})
        src = tmp_path / "rec.mp4"
        src.write_bytes(b"1")
        up = clouds.upload(str(src), "rec.mp4", auto_settings.load("bob"), owner="bob")
        assert up["ok"]
        assert Path(up["path"]).parent == config.DATA_DIR / "recordings" / "bob"

    def test_абсолютная_папка_протоколов_в_чужую_команду(self, client, tmp_path):
        self._bob(client)
        auto_settings.save("bob", {"cloud": "local", "protocol_folder":
                                   str(config.DATA_DIR / "recordings" / "alice")})
        src = tmp_path / "p.docx"
        src.write_bytes(b"1")
        cfg = auto_settings.load("bob")
        up = clouds.upload(str(src), "p.docx", cfg, folder=cfg["protocol_folder"], owner="bob")
        assert up["ok"]
        assert Path(up["path"]).parent == config.DATA_DIR / "recordings" / "bob" / "protocols"

    def test_папка_основателя_для_клиента_работает(self, client, tmp_path):
        self._bob(client)
        login(client)
        r = client.post("/api/automation/settings?team=bob", json={
            "cloud": "local", "local_dir": str(tmp_path / "bob"),
            "protocol_folder": str(tmp_path / "bob" / "p")})
        assert r.status_code == 200
        cfg = auto_settings.load("bob")
        assert local_cloud.base_dir(clouds._backend_cfg(cfg, "local", "bob")) == tmp_path / "bob"
        src = tmp_path / "x.docx"
        src.write_bytes(b"1")
        up = clouds.upload(str(src), "x.docx", cfg, folder=cfg["protocol_folder"], owner="bob")
        assert Path(up["path"]).parent == tmp_path / "bob" / "p"

    def test_основатель_очищает_старые_значения(self, client):
        self._bob(client)
        auto_settings.save("bob", {"local_dir": "/srv/alice",
                                   "protocol_folder": "/srv/alice/p"})
        login(client)
        r = client.post("/api/automation/settings?team=bob",
                        json={"local_dir": "", "protocol_folder": ""})
        assert r.status_code == 200
        cfg = auto_settings.load("bob")
        assert cfg["local_dir"] == "" and cfg["protocol_folder"] == ""

    def test_своей_команде_основателя_путь_как_задан(self, client):
        self._bob(client)
        shared = config.DATA_DIR / "recordings"
        auto_settings.save("alice", {"local_dir": str(shared)})
        cfg = clouds._backend_cfg(auto_settings.load("alice"), "local", "alice")
        assert local_cloud.base_dir(cfg) == shared

    def test_папка_протоколов_основателя_как_задана(self, client, tmp_path):
        """Своей команде основатель задаёт любую папку протоколов — и вне
        папки записей."""
        self._bob(client)
        auto_settings.save("alice", {"cloud": "local",
                                     "protocol_folder": str(tmp_path / "alice-p")})
        src = tmp_path / "x.docx"
        src.write_bytes(b"1")
        cfg = auto_settings.load("alice")
        up = clouds.upload(str(src), "x.docx", cfg, folder=cfg["protocol_folder"], owner="alice")
        assert Path(up["path"]).parent == tmp_path / "alice-p"

    def test_статус_облака_показывает_папку_команды(self, client):
        self._bob(client)
        r = clouds.readiness(auto_settings.load("bob"))
        assert str(Path("recordings") / "bob") in r["backends"]["local"]["detail"]


# --------------------------------------------------------------------------- #
# Н: законная папка Яндекс.Диска «/Протоколы»
# --------------------------------------------------------------------------- #
class TestПапкаДиска:
    def test_путь_диска_со_слэша_принимается(self, client, monkeypatch):
        _founder_with_bob(client, monkeypatch, cloud="yandex_disk")
        login(client, "bob")
        r = client.post("/api/automation/settings", json={"protocol_folder": "/Протоколы"})
        assert r.status_code == 200
        assert auto_settings.load("bob")["protocol_folder"] == "/Протоколы"

    @pytest.mark.parametrize("folder", ["C:\\data\\recordings\\alice", "\\\\host\\share"])
    def test_путь_сервера_отказ_с_ошибкой(self, client, monkeypatch, folder):
        _founder_with_bob(client, monkeypatch, cloud="yandex_disk")
        login(client, "bob")
        r = client.post("/api/automation/settings", json={"protocol_folder": folder})
        assert r.status_code == 400
        assert "сервер" in r.json()["detail"]
        assert auto_settings.load("bob")["protocol_folder"] == "disk:/Телемост-протоколы"

    def test_у_локального_облака_слэш_это_сервер(self, client, monkeypatch):
        _founder_with_bob(client, monkeypatch)            # облако по умолчанию — local
        login(client, "bob")
        r = client.post("/api/automation/settings", json={"protocol_folder": "/srv/x"})
        assert r.status_code == 400
        r = client.post("/api/automation/settings",
                        json={"cloud": "yandex_disk", "protocol_folder": "/Протоколы"})
        assert r.status_code == 200
        assert auto_settings.load("bob")["protocol_folder"] == "/Протоколы"

    def test_старое_значение_формы_не_мешает_сохранению(self, client, monkeypatch):
        """Форма шлёт всё целиком: сохранённое раньше значение — не новая
        попытка, иначе команда не сохранила бы облако вовсе."""
        _founder_with_bob(client, monkeypatch)
        auto_settings.save("bob", {"protocol_folder": "/srv/old"})
        login(client, "bob")
        r = client.post("/api/automation/settings",
                        json={"protocol_folder": "/srv/old", "oneoff_folder": "Разовые"})
        assert r.status_code == 200
        assert auto_settings.load("bob")["oneoff_folder"] == "Разовые"


# --------------------------------------------------------------------------- #
# Н: пустой/кривой ответ Weeek не стирает план
# --------------------------------------------------------------------------- #
class _BrokenWeeek(_FakeWeeek):
    def __init__(self, tasks, broken):
        super().__init__(tasks)
        self.broken = broken
        self.on = False

    def __call__(self, method, path, token, params=None, body=None, **kw):
        if self.on and path == "/tm/tasks":
            return self.broken
        return super().__call__(method, path, token, params, body, **kw)


class TestКривойОтветWeeek:
    @pytest.mark.parametrize("broken", [{}, {"success": True}, {"tasks": None},
                                        {"tasks": "x"}, "строка", None])
    def test_план_остаётся(self, monkeypatch, broken):
        fake = _BrokenWeeek(TWO_BOARDS, broken)
        monkeypatch.setattr(weeek, "_request", fake)
        s = Scheduler()
        s._poll("bob", _cfg())
        before = set(s._states)
        assert before
        fake.on = True
        with pytest.raises(weeek.WeeekError):
            s._poll("bob", _cfg())
        assert set(s._states) == before

    def test_пустой_список_задач_законен(self, monkeypatch):
        monkeypatch.setattr(weeek, "_request", lambda *a, **k: {"tasks": []})
        assert weeek.list_tasks("t") == []

    def test_кнопка_обновить_говорит_об_ошибке(self, client, monkeypatch):
        register(client)
        auto_settings.save("alice", {"weeek_token": "t"})
        monkeypatch.setattr(weeek, "_request", lambda *a, **k: {})
        s = Scheduler()
        monkeypatch.setattr(s, "start", lambda: None)
        res = s.poll_now("alice")
        assert res["ok"] is False


# --------------------------------------------------------------------------- #
# С: окно входа, застрявшее в длинной команде
# --------------------------------------------------------------------------- #
class _Stuck:
    """Поток окна входа, который не отвечает (застрял в длинном `type`)."""

    def __init__(self):
        self.release = threading.Event()
        self.t = threading.Thread(target=self.release.wait, daemon=True)
        self.t.start()


@pytest.fixture
def stuck():
    made = []

    def make(team: str, cfg: dict, expired=True):
        st = _Stuck()
        ses = browser._LoginSession(cfg)
        ses._thread = st.t
        if expired:
            ses._opened = time.time() - browser.LOGIN_MAX_SEC - 5
        ses.close = lambda wait=10.0: None          # не отвечает на «закрыть»
        browser.login_sessions[team] = ses
        made.append(st)
        return ses
    browser.login_sessions.clear()
    yield make
    for st in made:
        st.release.set()
    browser.login_sessions.clear()


class TestЗастрявшееОкноВхода:
    def test_просроченное_не_отдаётся_своей_команде(self, stuck):
        """мутант: `_live` без `not expired()`."""
        stuck("bob", {"_team": "bob"})
        assert browser.login_get("bob") is None

    def test_застрявшее_окно_считается_в_потолке(self, stuck, monkeypatch):
        monkeypatch.setattr(browser, "LOGIN_MAX_WINDOWS", 1)
        stuck("bob", {"_team": "bob"})
        started = []
        monkeypatch.setattr(browser._LoginSession, "start", lambda self: started.append(1))
        with pytest.raises(RuntimeError):
            browser.login_open({"_team": "carol"}, "carol")
        assert started == []

    def test_застрявшее_окно_держит_свой_профиль(self, stuck, monkeypatch):
        stuck("bob", {})
        started = []
        monkeypatch.setattr(browser._LoginSession, "start", lambda self: started.append(1))
        with pytest.raises(RuntimeError):
            browser.login_open({}, "carol")
        assert started == []

    def test_своё_застрявшее_окно_не_даёт_второй_chromium(self, stuck, monkeypatch):
        stuck("bob", {"_team": "bob"})
        started = []
        monkeypatch.setattr(browser._LoginSession, "start", lambda self: started.append(1))
        with pytest.raises(RuntimeError):
            browser.login_open({"_team": "bob"}, "bob")
        assert started == []

    def test_потолок_окон(self, stuck, monkeypatch):
        """мутант: убрана проверка `LOGIN_MAX_WINDOWS`."""
        monkeypatch.setattr(browser, "LOGIN_MAX_WINDOWS", 2)
        stuck("bob", {"_team": "bob"}, expired=False)
        stuck("carol", {"_team": "carol"}, expired=False)
        monkeypatch.setattr(browser._LoginSession, "start", lambda self: None)
        with pytest.raises(RuntimeError, match="слишком много"):
            browser.login_open({"_team": "dave"}, "dave")

    def test_занятость_по_профилю(self, stuck):
        """мутант: `_login_busy` без профиля."""
        stuck("bob", {"_team": "bob"}, expired=False)
        assert browser._login_busy({"_team": "bob"}) is True
        assert browser._login_busy({"_team": "carol"}) is False

    def test_длинный_текст_не_принимается(self, stuck):
        ses = stuck("bob", {"_team": "bob"}, expired=False)
        with pytest.raises(ValueError):
            ses.send("type", text="x" * (browser.LOGIN_MAX_TEXT + 1))
        assert ses._cmds.empty()
        ses.send("type", text="x" * browser.LOGIN_MAX_TEXT)
        assert not ses._cmds.empty()

    def test_длинный_текст_через_api_400(self, client, stuck):
        register(client)
        ses = stuck(security.team_of("alice"), {"_team": "alice"}, expired=False)
        r = client.post("/api/automation/recorder/login/action",
                        json={"kind": "type", "text": "x" * 5000})
        assert r.status_code in (400, 422)
        assert ses._cmds.empty()


# --------------------------------------------------------------------------- #
# Выжившие мутанты: `_norm_id`, `_foreign_profile`, `_allowed_boards`
# --------------------------------------------------------------------------- #
class TestМутантыВторогоРаунда:
    def test_доска_из_пробелов_под_замком_закрывает_всё(self, monkeypatch):
        """мутант: `team_meetings` без `_norm_id` — «   » считалась доской,
        а дальше `upcoming_meetings` превращала её в «без доски»: все доски."""
        monkeypatch.setattr(weeek, "_request", _FakeWeeek(TWO_BOARDS))
        got = weeek.team_meetings(_cfg(board="   ", managed_by_server=True,
                                       _team="bob-ws"))
        assert got == []

    def test_подкаталог_общего_профиля_чужой(self, client):
        """мутант: `_foreign_profile` сравнивает только `==`."""
        register(client)
        register(client, "bob", phone="+7 999 000-00-01")
        sub = config.DATA_DIR / "browser-profile" / "Default"
        auto_settings.save("bob", {"browser_profile_dir": str(sub)})
        got = browser._profile_dir(auto_settings.load("bob"))
        assert got == security.user_dir("bob") / "browser-profile"

    def test_подкаталог_своей_команды_свой(self, client):
        register(client)
        register(client, "bob", phone="+7 999 000-00-01")
        mine = security.user_dir("bob") / "другой-профиль"
        auto_settings.save("bob", {"browser_profile_dir": str(mine)})
        assert browser._profile_dir(auto_settings.load("bob")) == mine

    def test_доска_задач_тоже_своя(self, client, monkeypatch):
        """мутант: `_allowed_boards` без `weeek_tasks_board_id`."""
        _founder_with_bob(client, monkeypatch, managed_by_server=True,
                          weeek_token="tok-bob", weeek_project_id="10",
                          weeek_board_id="1", weeek_tasks_board_id="2")
        login(client, "bob")
        assert client.get("/api/automation/weeek/probe?task_id=102").status_code == 200
        assert client.get("/api/automation/weeek/probe?task_id=101").status_code == 200
        r = client.get("/api/automation/weeek/board-columns?board_id=2")
        assert r.status_code == 200
