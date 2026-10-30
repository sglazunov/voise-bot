"""Адверсариальное ревью 30.10, раздел 3: разделение клиентов и замок настроек.

Каждый тест здесь падал на коде `e728109` (или убивает выжившего мутанта
из раздела 7 отчёта — помечено «мутант»). Фикстура клиента — та же
`_client_team`, что в tests/test_tenants_3010.py: основатель alice заводит
клиента bob с замком, доской 1 и облаком Яндекса.
"""
from __future__ import annotations

import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import config, security
from app import main as main_mod
from app.automation import clouds
from app.automation import scheduler as sched_mod
from app.automation import settings as auto_settings
from app.automation import weeek
from app.automation.recorder import browser
from app.automation.scheduler import Scheduler
from app.jobs import Job, store
from conftest import login, register

from test_tenants_3010 import (TWO_BOARDS, URL1, URL2, _client_team,
                               _FakeWeeek, _task)


def _logout(client):
    client.post("/api/auth/logout")


def _founder_with_bob(client, monkeypatch, **bob):
    """Основатель alice и клиент bob БЕЗ замка (или с тем, что передали)."""
    monkeypatch.setattr(weeek, "_request", _FakeWeeek(TWO_BOARDS))
    register(client)
    register(client, "bob", phone="+7 999 000-00-01")
    login(client)
    if bob:
        assert client.post("/api/automation/settings?team=bob",
                           json=bob).status_code == 200
    _logout(client)


# --------------------------------------------------------------------------- #
# К: доставка протокола в чужую задачу Weeek
# --------------------------------------------------------------------------- #
class TestДоставкаВЧужуюЗадачу:
    """`POST /api/jobs` и пересборка принимали `deliver_weeek_task` без
    `_guard_task`: клиент под замком писал протокол комментарием в задачу
    другого клиента общего воркспейса."""

    def _upload(self, client, task):
        return client.post("/api/jobs", files={"file": ("x.wav", b"RIFF0000", "audio/wav")},
                           data={"deliver_weeek_task": task})

    def test_новая_задача_с_чужой_задачей_weeek_403(self, client, monkeypatch):
        _client_team(client, monkeypatch)
        made = []
        monkeypatch.setattr(main_mod.store, "create", lambda *a, **k: made.append(k))
        assert self._upload(client, "102").status_code == 403
        assert self._upload(client, "https://app.weeek.net/ws/1/task/102").status_code == 403
        assert made == []

    def test_своя_задача_проходит(self, client, monkeypatch):
        _client_team(client, monkeypatch)
        job = Job(id="j-own", filename="x.wav", audio_path="x", language="ru",
                  diarize=False, owner="bob")
        made = []
        monkeypatch.setattr(main_mod.store, "create",
                            lambda *a, **k: made.append(k) or job)
        r = self._upload(client, "101")
        assert r.status_code == 201, r.text
        assert made[0]["deliver_weeek_task"] == "101"

    def test_мусор_вместо_номера_задачи_400(self, client, monkeypatch):
        _client_team(client, monkeypatch)
        monkeypatch.setattr(main_mod.store, "create", lambda *a, **k: None)
        assert self._upload(client, "../../ws/members").status_code == 400

    def test_пересборка_с_чужой_задачей_403(self, client, monkeypatch):
        _client_team(client, monkeypatch)
        job = Job(id="j-bob", filename="x.wav", audio_path="x", language="ru",
                  diarize=False, owner="bob", status="done", created_at=time.time())
        store._jobs[job.id] = job
        called = []
        monkeypatch.setattr(main_mod.store, "reanalyze",
                            lambda *a, **k: called.append(k) or job)
        r = client.post("/api/jobs/j-bob/reanalyze",
                        json={"deliver_weeek_task": "102"})
        assert r.status_code == 403
        assert called == []
        r = client.post("/api/jobs/j-bob/reanalyze",
                        json={"deliver_weeek_task": "101"})
        assert r.status_code == 200, r.text
        assert called[0]["deliver_weeek_task"] == "101"

    def test_без_замка_проверки_нет(self, client, monkeypatch):
        """Своя команда без замка доставляет куда хочет — как раньше."""
        _founder_with_bob(client, monkeypatch, weeek_token="tok-bob")
        login(client, "bob")
        job = Job(id="j-free", filename="x.wav", audio_path="x", language="ru",
                  diarize=False, owner="bob")
        monkeypatch.setattr(main_mod.store, "create", lambda *a, **k: job)
        assert self._upload(client, "102").status_code == 201


# --------------------------------------------------------------------------- #
# К + В: карточки задач, ушедших с доски; решение «Пишем» по чужой встрече
# --------------------------------------------------------------------------- #
def _cfg(board=1, **extra):
    return {"weeek_token": "t", "weeek_project_id": 10,
            "weeek_board_id": board, **extra}


class TestКарточкиЧужойДоски:
    def test_задачу_перенесли_на_чужую_доску(self, monkeypatch):
        """Карточка убиралась, только если задача ещё в выдаче или сменился
        фильтр. Задачу 101 перенесли на доску 2 — бот клиента 1 всё равно
        пошёл бы на чужую встречу."""
        tasks = [dict(t) for t in TWO_BOARDS]
        monkeypatch.setattr(weeek, "_request", _FakeWeeek(tasks, honour_board=True))
        s = Scheduler()
        s._poll("bob", _cfg())
        assert {str(st.task_id) for st in s._states.values()} == {"101"}
        tasks[0]["boardId"] = 2                      # перенесли к другому клиенту
        s._poll("bob", _cfg())
        assert s._states == {}

    def test_задачу_удалили_карточка_уходит(self, monkeypatch):
        tasks = [dict(TWO_BOARDS[0])]
        monkeypatch.setattr(weeek, "_request", _FakeWeeek(tasks))
        s = Scheduler()
        s._poll("bob", _cfg())
        tasks.clear()
        s._poll("bob", _cfg())
        assert s._states == {}

    def test_встреча_по_ссылке_остаётся(self, monkeypatch):
        monkeypatch.setattr(weeek, "_request", _FakeWeeek([]))
        s = Scheduler()
        st = sched_mod.MeetingState(key="bob:link-1:x", task_id="link-20261030-ab",
                                    title="t", url=URL1, start=None, owner="bob")
        s._states[st.key] = st
        s._poll("bob", _cfg())
        assert st.key in s._states

    def test_пропущенная_карточка_широкого_фильтра_уходит(self, monkeypatch):
        """Без доски карточка 102 получила «skipped»; после выбора доски 1 она
        оставалась, и решение «Пишем» возвращало её в план."""
        monkeypatch.setattr(weeek, "_request", _FakeWeeek(TWO_BOARDS, honour_board=True))
        s = Scheduler()
        s._poll("bob", _cfg(board=None))
        for st in s._states.values():
            st.state = "skipped"
        s._poll("bob", _cfg(board=1))
        assert {str(st.task_id) for st in s._states.values()} == {"101"}

    def test_решение_не_возвращает_чужую_встречу(self, client, monkeypatch):
        register(client)
        monkeypatch.setattr(weeek, "_request", _FakeWeeek(TWO_BOARDS, honour_board=True))
        s = Scheduler()
        s._boot_time = 0
        s._poll("alice", _cfg(board=1))
        # Карточка 102 осталась от прежнего широкого фильтра (снапшот, гонка
        # с опросом) — решение по ней не должно ставить её в план.
        alien = sched_mod.MeetingState(
            key="alice:102:x", task_id=102, title="Чужая", url=URL2,
            start=datetime.now(timezone.utc) + timedelta(days=1), owner="alice")
        alien.state = "skipped"
        s._states[alien.key] = alien
        s.set_decision("alice", "102", True)
        assert alien.state == "skipped"
        own = next(st for st in s._states.values() if str(st.task_id) == "101")
        own.state = "skipped"
        s.set_decision("alice", "101", True)
        assert own.state == "scheduled"


# --------------------------------------------------------------------------- #
# В: замок без выбранной доски
# --------------------------------------------------------------------------- #
class TestЗамокБезДоски:
    """Замок включён, доска не выбрана: эндпоинты Weeek всё запрещали, а план
    встреч и бот видели ВСЕ доски общего воркспейса."""

    def test_опрос_не_видит_встреч(self, monkeypatch):
        monkeypatch.setattr(weeek, "_request", _FakeWeeek(TWO_BOARDS))
        s = Scheduler()
        s._poll("bob", _cfg(board=None, managed_by_server=True))
        assert s._states == {}

    def test_без_замка_доска_не_нужна(self, monkeypatch):
        monkeypatch.setattr(weeek, "_request", _FakeWeeek(TWO_BOARDS))
        s = Scheduler()
        s._poll("bob", _cfg(board=None))
        assert {str(st.task_id) for st in s._states.values()} == {"101", "102"}

    def test_предпросмотр_пустой(self, client, monkeypatch):
        _founder_with_bob(client, monkeypatch, managed_by_server=True,
                          weeek_token="tok-bob", weeek_project_id="10")
        login(client, "bob")
        r = client.get("/api/automation/meetings")
        assert r.status_code == 200
        assert r.json()["meetings"] == []

    def test_ночная_очистка_не_идёт_по_чужим_доскам(self, monkeypatch):
        fake = _FakeWeeek(TWO_BOARDS)
        monkeypatch.setattr(weeek, "_request", fake)
        monkeypatch.setattr(sched_mod, "_WIPE_HOUR", 0)
        monkeypatch.setattr(sched_mod, "_WIPE_WINDOW_H", 24)
        monkeypatch.setattr(sched_mod.auto_settings, "save", lambda *a, **k: None)
        seen = []
        s = Scheduler()
        monkeypatch.setattr(s, "_wipe_inherited",
                            lambda user, cfg, meetings, **k: seen.append(
                                [m.task_id for m in meetings]) or (0, 0))
        s._nightly_wipe("bob", _cfg(board=None, managed_by_server=True))
        assert seen in ([], [[]])


# --------------------------------------------------------------------------- #
# В: окно входа в Яндекс
# --------------------------------------------------------------------------- #
class _FakePage:
    def goto(self, *a, **k): pass
    def screenshot(self, **k): return b"png"


class _FakeCtx:
    def __init__(self):
        self.pages = [_FakePage()]
        self.closed = False
    def close(self): self.closed = True


class _FakePW:
    def __init__(self):
        self.ctx = _FakeCtx()
        self.chromium = types.SimpleNamespace(
            launch_persistent_context=lambda *a, **k: self.ctx)
    def __enter__(self): return self
    def __exit__(self, *a): return False


@pytest.fixture
def fake_playwright(monkeypatch):
    pw = _FakePW()
    mod = types.ModuleType("playwright.sync_api")
    mod.sync_playwright = lambda: pw
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)
    browser.login_sessions.clear()
    yield pw
    for ses in list(browser.login_sessions.values()):
        ses.close(wait=2)
    browser.login_sessions.clear()


class TestОкноВхода:
    def test_брошенное_окно_закрывается_само(self, fake_playwright, monkeypatch):
        """У сессии не было тайм-аута: окно, которое никто не трогает, жило
        вечно и держало вход."""
        monkeypatch.setattr(browser, "LOGIN_IDLE_SEC", 0.5)
        ses = browser.login_open({"_team": "bob"}, "bob")
        time.sleep(0.3)
        assert ses.alive()
        deadline = time.time() + 5
        while ses.alive() and time.time() < deadline:
            time.sleep(0.1)
        assert not ses.alive()
        assert fake_playwright.ctx.closed
        assert browser.login_get("bob") is None

    def test_пока_окно_смотрят_оно_живёт(self, fake_playwright, monkeypatch):
        monkeypatch.setattr(browser, "LOGIN_IDLE_SEC", 0.6)
        browser.login_open({"_team": "bob"}, "bob")
        for _ in range(10):                      # интерфейс опрашивает экран
            ses = browser.login_get("bob")
            assert ses is not None
            ses.screenshot()
            time.sleep(0.15)
        assert browser.login_get("bob") is not None

    def test_окно_не_держит_дольше_потолка(self, fake_playwright, monkeypatch):
        """Опрос экрана — тоже активность; без потолка открытая вкладка
        держала бы окно вечно."""
        monkeypatch.setattr(browser, "LOGIN_MAX_SEC", 0.6)
        ses = browser.login_open({"_team": "bob"}, "bob")
        deadline = time.time() + 5
        while ses.alive() and time.time() < deadline:
            browser.login_get("bob")
            time.sleep(0.1)
        assert not ses.alive()

    def test_одна_команда_не_держит_вход_всех(self, fake_playwright, client):
        register(client)
        register(client, "bob", phone="+7 999 000-00-01")
        browser.login_open(auto_settings.load("bob"), "bob")
        # Профили у команд разные — окно основателя открывается.
        ses = browser.login_open(auto_settings.load("alice"), "alice")
        assert ses.alive()

    def test_общий_профиль_по_прежнему_одно_окно(self, fake_playwright):
        browser.login_open({}, "bob")
        with pytest.raises(RuntimeError):
            browser.login_open({}, "carol")


# --------------------------------------------------------------------------- #
# В: старый общий `browser_profile_dir`
# --------------------------------------------------------------------------- #
class TestПрофильМиграция:
    def test_старый_общий_профиль_клиента_игнорируется(self, client):
        """Значение, сохранённое до обновления, вело в общий профиль; ключ
        теперь серверный — клиент не может его даже очистить."""
        register(client)
        register(client, "bob", phone="+7 999 000-00-01")
        shared = str(config.DATA_DIR / "browser-profile")
        auto_settings.save("bob", {"browser_profile_dir": shared})
        got = browser._profile_dir(auto_settings.load("bob"))
        assert got == security.user_dir("bob") / "browser-profile"

    def test_профиль_другой_команды_игнорируется(self, client):
        register(client)
        register(client, "bob", phone="+7 999 000-00-01")
        register(client, "carol", phone="+7 999 000-00-02")
        auto_settings.save("bob", {"browser_profile_dir":
                                   str(security.user_dir("carol") / "browser-profile")})
        got = browser._profile_dir(auto_settings.load("bob"))
        assert got == security.user_dir("bob") / "browser-profile"

    def test_основателю_путь_по_прежнему_свой(self, client, tmp_path):
        register(client)
        register(client, "bob", phone="+7 999 000-00-01")
        auto_settings.save("alice", {"browser_profile_dir": str(tmp_path / "p")})
        assert browser._profile_dir(auto_settings.load("alice")) == tmp_path / "p"
        # Особый путь, заданный основателем клиенту, тоже работает.
        auto_settings.save("bob", {"browser_profile_dir": str(tmp_path / "bob")})
        assert browser._profile_dir(auto_settings.load("bob")) == tmp_path / "bob"


# --------------------------------------------------------------------------- #
# С + Н: серверные ключи
# --------------------------------------------------------------------------- #
class TestСерверныеКлючи:
    def test_local_dir_клиент_без_замка_не_меняет(self, client, monkeypatch):
        _founder_with_bob(client, monkeypatch)
        login(client, "bob")
        client.post("/api/automation/settings",
                    json={"local_dir": str(security.user_dir("alice"))})
        assert auto_settings.load("bob")["local_dir"] == ""
        assert "local_dir" in client.get("/api/automation/settings").json()["locked_keys"]

    @pytest.mark.parametrize("folder", ["/srv/data/recordings/alice",
                                        "C:\\data\\recordings\\alice",
                                        "\\\\host\\share"])
    def test_абсолютная_папка_протоколов_не_меняется(self, client, monkeypatch, folder):
        _founder_with_bob(client, monkeypatch)
        login(client, "bob")
        client.post("/api/automation/settings", json={"protocol_folder": folder})
        assert auto_settings.load("bob")["protocol_folder"] == "disk:/Телемост-протоколы"

    @pytest.mark.parametrize("folder", ["disk:/Мои протоколы", "1AbCdEfG", ""])
    def test_папка_облака_меняется(self, client, monkeypatch, folder):
        _founder_with_bob(client, monkeypatch)
        login(client, "bob")
        client.post("/api/automation/settings", json={"protocol_folder": folder})
        assert auto_settings.load("bob")["protocol_folder"] == folder

    def test_экономика_команды_только_основателю(self, client, monkeypatch):
        _founder_with_bob(client, monkeypatch)
        login(client, "bob")
        client.post("/api/automation/settings",
                    json={"subscription_rub": 1, "support_hours_month": 999})
        cfg = auto_settings.load("bob")
        assert cfg["subscription_rub"] is None and cfg["support_hours_month"] == 0.0
        _logout(client)
        login(client)
        client.post("/api/automation/settings?team=bob",
                    json={"subscription_rub": 6000, "support_hours_month": 2,
                          "local_dir": "/srv/bob", "protocol_folder": "/srv/bob/p"})
        cfg = auto_settings.load("bob")
        assert cfg["subscription_rub"] == 6000 and cfg["support_hours_month"] == 2
        assert cfg["local_dir"] == "/srv/bob" and cfg["protocol_folder"] == "/srv/bob/p"


# --------------------------------------------------------------------------- #
# С: `?team=` на остальных ручках
# --------------------------------------------------------------------------- #
class TestTeamНаВсехРучках:
    def _as_founder(self, client, monkeypatch):
        _client_team(client, monkeypatch)
        _logout(client)
        login(client)
        auto_settings.save("alice", {"weeek_token": "tok-alice"})

    def test_диагностика_задачи_токеном_клиента(self, client, monkeypatch):
        self._as_founder(client, monkeypatch)
        tokens = []
        orig = weeek.get_task
        monkeypatch.setattr(weeek, "get_task",
                            lambda tok, tid: tokens.append(tok) or orig(tok, tid))
        r = client.get("/api/automation/weeek/probe?task_id=101&team=bob")
        assert r.status_code == 200
        assert tokens and set(tokens) == {"tok-bob"}

    def test_участники_и_кэш_команды(self, client, monkeypatch):
        self._as_founder(client, monkeypatch)
        tokens = []
        monkeypatch.setattr(weeek, "list_members",
                            lambda tok: tokens.append(tok) or [{"id": "u1", "name": "Боб"}])
        r = client.get("/api/automation/weeek/members?refresh=1&team=bob")
        assert r.status_code == 200 and tokens == ["tok-bob"]
        assert auto_settings.load("bob")["weeek_members_cache"]["members"]
        assert not auto_settings.load("alice")["weeek_members_cache"]["members"]

    def test_соответствие_имён_в_команду(self, client, monkeypatch):
        self._as_founder(client, monkeypatch)
        r = client.post("/api/automation/weeek/user-map?team=bob",
                        json={"name": "Боб", "user_id": "u1"})
        assert r.status_code == 200
        assert auto_settings.load("bob")["weeek_user_map"] == {"Боб": "u1"}
        assert auto_settings.load("alice")["weeek_user_map"] == {}

    def test_облако_команды(self, client, monkeypatch):
        self._as_founder(client, monkeypatch)
        assert client.get("/api/automation/clouds/status?team=bob").json()["selected"] == "yandex_disk"
        assert client.get("/api/automation/clouds/status").json()["selected"] == "local"
        seen = []
        monkeypatch.setattr(clouds, "upload", lambda path, name, cfg, **k: seen.append(
            (cfg.get("cloud"), k.get("owner"))) or {"ok": True, "url": "https://x"})
        assert client.post("/api/automation/clouds/test?team=bob").status_code == 200
        assert seen == [("yandex_disk", "bob")]

    def test_клиент_не_ходит_в_чужую_команду(self, client, monkeypatch):
        _client_team(client, monkeypatch)
        for method, url in (("get", "/api/automation/weeek/probe?task_id=101&team=alice"),
                            ("get", "/api/automation/weeek/members?team=alice"),
                            ("get", "/api/automation/clouds/status?team=alice"),
                            ("post", "/api/automation/clouds/test?team=alice")):
            assert getattr(client, method)(url).status_code == 403, url
        r = client.post("/api/automation/weeek/user-map?team=alice",
                        json={"name": "x", "user_id": "y"})
        assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Мутанты раздела 7: «Разделение клиентов — 21»
# --------------------------------------------------------------------------- #
# Ключи — списком в тесте, а не через settings.MANAGED_KEYS: иначе убранный из
# кортежа ключ убрал бы и свой тест (так мутанты и выживали).
_MANAGED_SAMPLES = {
    "weeek_token": "свой-токен",
    "weeek_project_id": "20",
    "weeek_board_id": "2",
    "weeek_tasks_project_id": "20",
    "weeek_tasks_board_id": "2",
    "weeek_tasks_column_id": "9",
    "cloud": "local",
    "yandex_disk": {"folder": "disk:/Чужое", "token": "свой"},
    "gdrive": {"folder_id": "чужая", "refresh_token": "r"},
    "local_dir": "/srv/чужое",
    "protocol_folder": "disk:/Чужие протоколы",
    "folders_by_series": False,
    "oneoff_folder": "Чужое",
    "subscription_rub": 1,
    "support_hours_month": 99,
}


class TestМутанты:
    def test_список_ключей_сверен(self):
        assert set(_MANAGED_SAMPLES) == set(auto_settings.MANAGED_KEYS)

    @pytest.mark.parametrize("key", sorted(_MANAGED_SAMPLES))
    def test_ключ_под_замком_не_меняется(self, client, monkeypatch, key):
        _client_team(client, monkeypatch)
        before = auto_settings.load("bob")[key]
        client.post("/api/automation/settings", json={key: _MANAGED_SAMPLES[key]})
        assert auto_settings.load("bob")[key] == before
        assert key in client.get("/api/automation/settings").json()["locked_keys"]

    def test_участник_команды_основателя_не_основатель(self, client, monkeypatch):
        """`is_super_admin(team_of(user))` вместо `is_super_admin(user)` —
        приглашённый в команду основателя получил бы ?team= над всеми."""
        _client_team(client, monkeypatch)
        _logout(client)
        register(client, "dave", code=security.invite_code_of("alice"),
                 phone="+7 999 000-00-03")
        login(client, "dave")
        assert client.get("/api/automation/settings?team=bob").status_code == 403
        assert client.post("/api/automation/settings?team=bob",
                           json={"managed_by_server": False}).status_code == 403
        assert client.get("/api/automation/teams").status_code == 403
        assert auto_settings.load("bob")["managed_by_server"] is True
        d = client.get("/api/automation/settings").json()
        assert d["can_manage_teams"] is False
        assert "managed_by_server" in d["locked_keys"]
        client.post("/api/automation/settings", json={"browser_profile_dir": "/x"})
        assert auto_settings.load("alice")["browser_profile_dir"] == ""

    def test_неизвестная_команда_404(self, client, monkeypatch):
        _client_team(client, monkeypatch)
        _logout(client)
        login(client)
        assert client.get("/api/automation/settings?team=nobody").status_code == 404
        assert client.post("/api/automation/settings?team=nobody",
                           json={"rec_include": "x"}).status_code == 404
        assert not (config.DATA_DIR / "users" / "nobody" / "automation.json").exists()

    def test_guard_task_без_досок_закрыт(self, client, monkeypatch):
        _founder_with_bob(client, monkeypatch, managed_by_server=True,
                          weeek_token="tok-bob", weeek_project_id="10")
        login(client, "bob")
        assert client.get("/api/automation/weeek/probe?task_id=101").status_code == 403
        r = client.post("/api/automation/meetings/links",
                        json={"task_id": "101", "video_url": "https://x/v.mp4"})
        assert r.status_code == 403

    def test_задачи_из_протокола_через_эндпоинт(self, client, monkeypatch):
        _client_team(client, monkeypatch)
        from app import weeek_tasks
        job = Job(id="j-tasks", filename="30.10.2026, 10:00. - Встреча.mp4",
                  audio_path="x", language="ru", diarize=False, owner="bob",
                  status="done", created_at=time.time())
        job.weeek_tasks = [{"key": "k1", "title": "Сделать", "status": "draft",
                            "project_id": "20", "board_id": "2", "column_id": "9"}]
        store._jobs[job.id] = job
        seen = {}
        monkeypatch.setattr(weeek_tasks, "create_selected",
                            lambda drafts, items, *a, **k: seen.update(
                                drafts=[dict(d) for d in drafts], items=items) or [])
        r = client.post("/api/jobs/j-tasks/weeek-tasks/create", json={"items": [
            {"key": "k1", "title": "Сделать", "project_id": "20",
             "board_id": "2", "column_id": "9"}]})
        assert r.status_code == 200, r.text
        d = seen["drafts"][0]
        assert (d["project_id"], d["board_id"], d["column_id"]) == ("10", "1", "7")
        assert "board_id" not in seen["items"][0]

    def test_ночная_очистка_по_доске(self, monkeypatch):
        """Без фильтра доски ночная очистка снимала поля в задачах других
        клиентов общего воркспейса."""
        fake = _FakeWeeek(TWO_BOARDS)
        monkeypatch.setattr(weeek, "_request", fake)
        monkeypatch.setattr(sched_mod, "_WIPE_HOUR", 0)
        monkeypatch.setattr(sched_mod, "_WIPE_WINDOW_H", 24)
        monkeypatch.setattr(sched_mod.auto_settings, "save", lambda *a, **k: None)
        seen = []
        s = Scheduler()
        monkeypatch.setattr(s, "_wipe_inherited",
                            lambda user, cfg, meetings, **k: seen.append(
                                sorted(m.task_id for m in meetings)) or (0, 0))
        s._nightly_wipe("bob", _cfg(board=1))
        assert seen == [[101]]
