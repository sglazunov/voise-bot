"""Несколько клиентов на одном инстансе (2026-10-30).

Каждый тест падал до правки:
1. фильтр по доске Weeek: `weeek_board_id` не сохранялся (ключа не было в
   `_DEFAULTS`), `upcoming_meetings` не умел доску вовсе, а после смены
   доски карточки встреч СТАРОЙ доски оставались в плане и записывались;
2. мастер-профиль браузера (вход бота в Яндекс) был общим на все команды,
   а путь к нему мог задать админ любой команды;
3. «замок настроек»: клиентская команда, управляемая сервером, не может
   снять фильтр доски, сменить токен, облако и место задач — это делает
   только основатель сервера; ручки Weeek не отдают чужие доски и задачи.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app import config, security
from app import weeek_tasks as wt
from app.automation import settings as auto_settings
from app.automation import weeek
from app.automation import scheduler as sched_mod
from app.automation.recorder import browser
from app.automation.scheduler import Scheduler
from conftest import login, register

URL1 = "https://telemost.yandex.ru/j/11111111111111"
URL2 = "https://telemost.yandex.ru/j/22222222222222"
URL3 = "https://telemost.yandex.ru/j/33333333333333"


def _task(tid, url, **extra):
    start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
    return {"id": tid, "title": f"Встреча {tid}", "description": url,
            "dueDateTime": start.isoformat().replace("+00:00", "Z"), **extra}


class _FakeWeeek:
    """Подмена `weeek._request`: весь настоящий код клиента отрабатывает."""

    def __init__(self, tasks, reject_board_param=False, honour_board=False):
        self.tasks = tasks
        self.reject_board_param = reject_board_param
        self.honour_board = honour_board
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method, path, token, params=None, body=None, **kw):
        params = dict(params or {})
        self.calls.append((path, params))
        if path == "/tm/tasks":
            if "boardId" in params and self.reject_board_param:
                raise weeek.WeeekError("400 unknown parameter boardId")
            if params.get("offset"):
                return {"tasks": []}
            tasks = self.tasks
            if self.honour_board and "boardId" in params:
                tasks = [t for t in tasks
                         if str(params["boardId"]) in weeek.task_board_ids(t)]
            return {"tasks": tasks}
        if path.startswith("/tm/tasks/"):
            tid = path.rsplit("/", 1)[-1]
            for t in self.tasks:
                if str(t["id"]) == tid:
                    return {"task": t}
            raise weeek.WeeekError("404")
        if path == "/tm/projects":
            return {"projects": [{"id": 10, "name": "Общий"},
                                 {"id": 20, "name": "Чужой"}]}
        if path == "/tm/boards":
            return {"boards": [{"id": 1, "name": "Клиент А"},
                               {"id": 2, "name": "Клиент Б"}]}
        if path == "/tm/board-columns":
            return {"boardColumns": [{"id": 7, "name": "Новые"}]}
        return {}


TWO_BOARDS = [
    _task(101, URL1, projectId=10, boardId=1),
    _task(102, URL2, projectId=10, boardId=2),
]


# --------------------------------------------------------------------------- #
# 1. Фильтр по доске
# --------------------------------------------------------------------------- #
class TestФильтрПоДоске:
    def test_две_доски_в_одном_проекте(self, monkeypatch):
        monkeypatch.setattr(weeek, "_request", _FakeWeeek(TWO_BOARDS))
        got = weeek.upcoming_meetings("tok", 10, board_id=2)
        assert [m.task_id for m in got] == [102]
        assert got[0].board_id == "2"

    def test_доска_уходит_в_запрос_к_api(self, monkeypatch):
        fake = _FakeWeeek(TWO_BOARDS)
        monkeypatch.setattr(weeek, "_request", fake)
        weeek.upcoming_meetings("tok", 10, board_id=2)
        params = [p for path, p in fake.calls if path == "/tm/tasks"]
        assert params and params[0].get("boardId") == 2
        assert params[0].get("projectId") == 10

    def test_api_не_принял_boardId_фильтр_на_клиенте_остаётся(self, monkeypatch):
        monkeypatch.setattr(weeek, "_request",
                            _FakeWeeek(TWO_BOARDS, reject_board_param=True))
        got = weeek.upcoming_meetings("tok", 10, board_id=1)
        assert [m.task_id for m in got] == [101]

    def test_задача_без_доски_при_фильтре_отсекается(self, monkeypatch):
        tasks = TWO_BOARDS + [_task(103, URL3, projectId=10)]
        monkeypatch.setattr(weeek, "_request", _FakeWeeek(tasks))
        got = weeek.upcoming_meetings("tok", 10, board_id=1)
        assert [m.task_id for m in got] == [101]
        # Без фильтра доски видны все, в том числе задача без доски.
        got = weeek.upcoming_meetings("tok", 10, board_id=None)
        assert sorted(m.task_id for m in got) == [101, 102, 103]

    def test_доска_из_locations_и_board(self, monkeypatch):
        tasks = [
            _task(201, URL1, locations=[{"projectId": 10, "boardId": 5,
                                         "boardColumnId": 50}]),
            _task(202, URL2, board={"id": 5, "name": "Клиент"}),
            _task(203, URL3, locations=[{"projectId": 10, "boardId": 6}]),
        ]
        monkeypatch.setattr(weeek, "_request", _FakeWeeek(tasks))
        got = weeek.upcoming_meetings("tok", None, board_id="5")
        assert sorted(m.task_id for m in got) == [201, 202]

    def test_смена_доски_убирает_карточки_старой(self, monkeypatch):
        """Карточки «scheduled» старой доски не вычищались: уборка трогала
        только задачи, пришедшие в выдаче. После смены доски бот записал бы
        встречу чужой доски, а клиент видел бы её в плане."""
        fake = _FakeWeeek(TWO_BOARDS, honour_board=True)
        monkeypatch.setattr(weeek, "_request", fake)
        s = Scheduler()
        s._poll("alice", {"weeek_token": "t", "weeek_project_id": 10,
                          "weeek_board_id": 1})
        assert {str(st.task_id) for st in s._states.values()} == {"101"}
        s._poll("alice", {"weeek_token": "t", "weeek_project_id": 10,
                          "weeek_board_id": 2})
        assert {str(st.task_id) for st in s._states.values()} == {"102"}

    def test_настройка_доски_сохраняется(self, client):
        register(client); login(client)
        r = client.post("/api/automation/settings", json={"weeek_board_id": "2"})
        assert r.status_code == 200
        assert client.get("/api/automation/settings").json()["weeek_board_id"] == "2"

    def test_предпросмотр_встреч_фильтрует_доску(self, client, monkeypatch):
        monkeypatch.setattr(weeek, "_request", _FakeWeeek(TWO_BOARDS))
        register(client); login(client)
        client.post("/api/automation/settings",
                    json={"weeek_token": "tok", "weeek_project_id": "10",
                          "weeek_board_id": "2"})
        got = client.get("/api/automation/meetings").json()["meetings"]
        assert [m["task_id"] for m in got] == [102]
        assert got[0]["board_id"] == "2"


# --------------------------------------------------------------------------- #
# 2. Своя команда на клиента: профиль браузера
# --------------------------------------------------------------------------- #
class TestПрофильБраузераКоманды:
    def test_у_клиентской_команды_свой_профиль(self, client):
        register(client)                                     # основатель
        register(client, "bob", phone="+7 999 000-00-01")    # клиент
        founder = browser._profile_dir(auto_settings.load("alice"))
        bob = browser._profile_dir(auto_settings.load("bob"))
        assert founder == config.DATA_DIR / "browser-profile"   # вход на бою цел
        assert bob != founder
        assert bob == security.user_dir("bob") / "browser-profile"
        # Участник команды bob работает в профиле своей команды.
        code = security.invite_code_of("bob")
        register(client, "carol", code=code, phone="+7 999 000-00-02")
        assert browser._profile_dir(auto_settings.load("carol")) == bob

    def test_путь_профиля_задаёт_только_основатель(self, client):
        register(client)
        register(client, "bob", phone="+7 999 000-00-01")
        login(client, "bob")
        client.post("/api/automation/settings",
                    json={"browser_profile_dir": str(config.DATA_DIR / "browser-profile")})
        assert auto_settings.load("bob")["browser_profile_dir"] == ""
        client.post("/api/auth/logout")
        login(client)
        client.post("/api/automation/settings", json={"browser_profile_dir": "/srv/p"})
        assert auto_settings.load("alice")["browser_profile_dir"] == "/srv/p"


# --------------------------------------------------------------------------- #
# 3. Замок настроек
# --------------------------------------------------------------------------- #
def _client_team(client, monkeypatch, tasks=TWO_BOARDS):
    """Основатель alice заводит клиента bob: управляемые настройки, доска 1."""
    monkeypatch.setattr(weeek, "_request", _FakeWeeek(tasks))
    register(client)
    register(client, "bob", phone="+7 999 000-00-01")
    login(client)
    r = client.post("/api/automation/settings?team=bob", json={
        "managed_by_server": True, "weeek_token": "tok-bob",
        "weeek_project_id": "10", "weeek_board_id": "1",
        "weeek_tasks_project_id": "10", "weeek_tasks_board_id": "1",
        "weeek_tasks_column_id": "7", "cloud": "yandex_disk",
        "yandex_disk": {"token": "yd", "folder": "disk:/Клиенты/bob"},
    })
    assert r.status_code == 200, r.text
    client.post("/api/auth/logout")
    login(client, "bob")


class TestЗамокНастроек:
    def test_основатель_настраивает_чужую_команду(self, client, monkeypatch):
        _client_team(client, monkeypatch)
        cfg = auto_settings.load("bob")
        assert cfg["managed_by_server"] is True
        assert cfg["weeek_board_id"] == "1"
        assert cfg["weeek_token"] == "tok-bob"
        assert auto_settings.load("alice")["weeek_board_id"] is None

    def test_клиент_не_снимает_фильтр_доски(self, client, monkeypatch):
        _client_team(client, monkeypatch)
        r = client.post("/api/automation/settings", json={
            "weeek_board_id": "", "weeek_project_id": "", "weeek_token": "свой",
            "managed_by_server": False, "cloud": "local",
            "yandex_disk": {"folder": "disk:/Клиенты/чужой"},
            "weeek_tasks_board_id": "2", "rec_include": "планёрка"})
        assert r.status_code == 200
        cfg = auto_settings.load("bob")
        assert cfg["weeek_board_id"] == "1"
        assert cfg["weeek_project_id"] == "10"
        assert cfg["weeek_token"] == "tok-bob"
        assert cfg["managed_by_server"] is True
        assert cfg["cloud"] == "yandex_disk"
        assert cfg["yandex_disk"]["folder"] == "disk:/Клиенты/bob"
        assert cfg["weeek_tasks_board_id"] == "1"
        assert cfg["rec_include"] == "планёрка"          # незаблокированное — меняется

    def test_участник_клиентской_команды_тоже_не_снимает(self, client, monkeypatch):
        _client_team(client, monkeypatch)
        code = security.invite_code_of("bob")
        client.post("/api/auth/logout")
        register(client, "carol", code=code, phone="+7 999 000-00-02")
        login(client, "carol")
        client.post("/api/automation/settings", json={"weeek_board_id": ""})
        assert auto_settings.load("bob")["weeek_board_id"] == "1"

    def test_интерфейс_получает_список_заблокированного(self, client, monkeypatch):
        _client_team(client, monkeypatch)
        d = client.get("/api/automation/settings").json()
        assert d["managed_by_server"] is True
        assert {"weeek_board_id", "weeek_token", "cloud",
                "managed_by_server"} <= set(d["locked_keys"])
        assert d["can_manage_teams"] is False

    def test_клиент_не_открывает_чужие_настройки(self, client, monkeypatch):
        _client_team(client, monkeypatch)
        assert client.get("/api/automation/settings?team=alice").status_code == 403
        assert client.post("/api/automation/settings?team=alice",
                           json={"rec_include": "x"}).status_code == 403

    def test_неуправляемая_команда_меняет_доску_сама(self, client):
        register(client)
        register(client, "bob", phone="+7 999 000-00-01")
        login(client, "bob")
        client.post("/api/automation/settings", json={"weeek_board_id": "3"})
        assert auto_settings.load("bob")["weeek_board_id"] == "3"

    def test_список_команд_только_основателю(self, client, monkeypatch):
        _client_team(client, monkeypatch)
        assert client.get("/api/automation/teams").status_code == 403
        client.post("/api/auth/logout")
        login(client)
        teams = {t["team"]: t for t in client.get("/api/automation/teams").json()["teams"]}
        assert teams["bob"]["managed_by_server"] is True
        assert teams["bob"]["weeek_board_id"] == "1"
        assert teams["alice"]["managed_by_server"] is False

    def test_проекты_и_доски_только_свои(self, client, monkeypatch):
        _client_team(client, monkeypatch)
        projects = client.get("/api/automation/weeek/projects").json()["projects"]
        assert [str(p["id"]) for p in projects] == ["10"]
        boards = client.get("/api/automation/weeek/boards?project_id=10").json()["boards"]
        assert [str(b["id"]) for b in boards] == ["1"]
        assert client.get("/api/automation/weeek/boards?project_id=20").status_code == 403
        assert client.get("/api/automation/weeek/board-columns?board_id=2").status_code == 403
        assert client.get("/api/automation/weeek/board-columns?board_id=1").status_code == 200

    def test_диагностика_чужой_задачи_закрыта(self, client, monkeypatch):
        _client_team(client, monkeypatch)
        assert client.get("/api/automation/weeek/probe?task_id=102").status_code == 403
        assert client.get("/api/automation/weeek/probe?task_id=101").status_code == 200

    def test_ссылки_в_чужую_задачу_не_пишутся(self, client, monkeypatch):
        _client_team(client, monkeypatch)
        r = client.post("/api/automation/meetings/links",
                        json={"task_id": "102", "video_url": "https://x/v.mp4"})
        assert r.status_code == 403

    def test_задачи_из_протокола_идут_на_доску_команды(self):
        cfg = {"managed_by_server": True, "weeek_tasks_project_id": "10",
               "weeek_tasks_board_id": "1", "weeek_tasks_column_id": "7"}
        drafts = [{"key": "j:tasks:0", "project_id": "20", "board_id": "2",
                   "column_id": "9"}]
        items = [{"key": "j:tasks:0", "title": "Сделать", "project_id": "20",
                  "board_id": "2", "column_id": "9"}]
        items = wt.pin_location(drafts, items, cfg)
        assert drafts[0]["project_id"] == "10" and drafts[0]["board_id"] == "1"
        assert drafts[0]["column_id"] == "7"
        assert "board_id" not in items[0] and items[0]["title"] == "Сделать"

    def test_черновики_управляемой_команды_не_берут_проект_серии(self):
        cfg = {**auto_settings._DEFAULTS, "managed_by_server": True,
               "weeek_tasks_project_id": "10"}
        drafts = wt.prepare(
            "j", {"tasks": [{"task": "Сделать отчёт", "owner": ""}]}, cfg,
            date(2026, 10, 30), series={"weeek_project_id": "20"})
        assert drafts and drafts[0]["project_id"] == "10"
