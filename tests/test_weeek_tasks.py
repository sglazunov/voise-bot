"""Черновики задач для Weeek из протокола (app/weeek_tasks.py + клиент)."""
from datetime import date

import pytest

from app import weeek_tasks as wt
from app.automation import weeek


MEMBERS = [
    {"id": "u-zoya", "firstName": "Зоя", "lastName": "Рябова", "email": "z@x"},
    {"id": "u-kirill", "firstName": "Кирилл", "lastName": "Бубнов", "email": "k@x"},
    {"id": "u-sergey1", "firstName": "Сергей", "lastName": "Глазунов", "email": "s1@x"},
    {"id": "u-sergey2", "firstName": "Сергей", "lastName": "Бескопыльный", "email": "s2@x"},
]


class TestParseDue:
    BASE = date(2026, 8, 27)   # четверг

    def test_weekday_relative_to_meeting(self):
        assert wt.parse_due("сделать до пятницы", self.BASE)[0] == "2026-08-28"
        assert wt.parse_due("к понедельнику", self.BASE)[0] == "2026-08-31"
        # тот же день недели — следующая неделя, а не сегодня
        assert wt.parse_due("до четверга", self.BASE)[0] == "2026-09-03"
        assert wt.parse_due("к следующей пятнице", self.BASE)[0] == "2026-09-04"

    def test_weekday_without_cue_is_not_a_deadline(self):
        assert wt.parse_due("в понедельник обсуждали макет", self.BASE) == (None, None)

    def test_explicit_dates(self):
        assert wt.parse_due("до 5 сентября", self.BASE)[0] == "2026-09-05"
        assert wt.parse_due("к 05.09", self.BASE)[0] == "2026-09-05"
        assert wt.parse_due("до 20 сентября и 15 октября", self.BASE)[0] == "2026-09-20"
        assert wt.parse_due("до 10.01", self.BASE)[0] == "2027-01-10"   # уже прошло → следующий год

    def test_relative_phrases(self):
        assert wt.parse_due("через неделю", self.BASE)[0] == "2026-09-03"
        assert wt.parse_due("завтра", self.BASE)[0] == "2026-08-28"
        assert wt.parse_due("до конца месяца", self.BASE)[0] == "2026-08-31"

    def test_nothing(self):
        assert wt.parse_due("подготовить макет главной", self.BASE) == (None, None)


class TestMatchMember:
    def test_full_name_and_initial(self):
        assert wt.match_member("Кирилл Бубнов", MEMBERS) == ("u-kirill", "exact")
        assert wt.match_member("Зоя Р", MEMBERS) == ("u-zoya", "exact")
        assert wt.match_member("Бубнов Кирилл", MEMBERS) == ("u-kirill", "exact")

    def test_unique_first_name_is_fuzzy_and_ambiguous_is_none(self):
        assert wt.match_member("Кирилл", MEMBERS) == ("u-kirill", "fuzzy")
        assert wt.match_member("Сергей", MEMBERS) == (None, "none")

    def test_team_map_wins(self):
        assert wt.match_member("Сергей", MEMBERS, {"Сергей": "u-sergey1"}) == ("u-sergey1", "map")
        assert wt.match_member("—", MEMBERS) == (None, "none")


def _analysis():
    return {
        "tasks": [
            {"task": "Подготовить макет главной до пятницы", "owner": "Кирилл Бубнов"},
            {"task": "Выдуманная задача", "owner": "Сергей"},
            {"task": "Написать ТЗ по задачам 2.3 и 2.4", "owner": "—", "due": "к 5 сентября"},
        ],
        "minor_tasks": [{"task": "Скинуть ссылку на Figma", "owner": "Зоя Р"}],
        "verification": {"tasks": [{"ok": True, "quote": "сделай макет до пятницы", "t": "12:40"},
                                   {"ok": False}, {"ok": True, "quote": "напишем ТЗ", "t": "06:12"}],
                         "minor_tasks": [{"ok": True, "quote": "скину ссылку"}]},
    }


CFG = {"weeek_tasks_include_minor": False, "weeek_tasks_only_grounded": True,
       "weeek_user_map": {}, "weeek_tasks_project_id": "5"}


class TestPrepare:
    def test_drafts_carry_owner_due_and_grounding(self):
        drafts = wt.prepare("j1", _analysis(), CFG, date(2026, 8, 27), MEMBERS)
        assert [d["key"] for d in drafts] == ["j1:tasks:0", "j1:tasks:1", "j1:tasks:2"]
        d0, d1, d2 = drafts
        assert d0["owner_user_id"] == "u-kirill" and d0["owner_match"] == "exact"
        assert d0["due"] == "2026-08-28" and d0["grounded"] and d0["t"] == "12:40"
        assert d1["grounded"] is False and d1["owner_match"] == "none"
        assert d2["due"] == "2026-09-05" and d2["owner_name"] == ""
        assert d0["project_id"] == "5"
        assert wt.default_selected(d0, CFG) is True
        assert wt.default_selected(d1, CFG) is False     # нет основания
        assert wt.default_selected(d2, CFG) is False     # нет исполнителя

    def test_minor_tasks_optional(self):
        drafts = wt.prepare("j1", _analysis(), {**CFG, "weeek_tasks_include_minor": True},
                            date(2026, 8, 27), MEMBERS)
        assert drafts[-1]["section"] == "minor_tasks" and drafts[-1]["owner_user_id"] == "u-zoya"

    def test_rebuild_keeps_created_status_by_fingerprint(self):
        drafts = wt.prepare("j1", _analysis(), CFG, date(2026, 8, 27), MEMBERS)
        drafts[0].update(status="created", weeek_task_id=777, weeek_url="u")
        # После пересборки задача съехала на другой индекс и чуть изменилась пунктуация
        a = _analysis()
        a["tasks"] = [a["tasks"][1], {"task": "Подготовить макет главной, до пятницы", "owner": "Кирилл Бубнов"}]
        again = wt.prepare("j1", a, CFG, date(2026, 8, 27), MEMBERS, previous=drafts)
        moved = [d for d in again if d["weeek_task_id"] == 777]
        assert len(moved) == 1 and moved[0]["status"] == "created"


class FakeClient:
    def __init__(self, fail_keys=()):
        self.calls = []
        self.fail = set(fail_keys)
        self.n = 100

    def create_task(self, token, **kw):
        self.calls.append(kw)
        if any(f in kw["title"] for f in self.fail):
            raise weeek.WeeekError("429 too many")
        self.n += 1
        return {"id": self.n, "url": f"https://app.weeek.net/ws/1/task/{self.n}", "warnings": []}


class TestCreate:
    def test_partial_failure_and_idempotency(self, monkeypatch):
        monkeypatch.setattr(wt.time, "sleep", lambda *_: None)
        drafts = wt.prepare("j1", _analysis(), CFG, date(2026, 8, 27), MEMBERS)
        client = FakeClient(fail_keys=("Выдуманная",))
        res = wt.create_selected(drafts, [{"key": "j1:tasks:0", "due": "2026-09-01"},
                                          {"key": "j1:tasks:1"}, {"key": "nope"}],
                                 "tok", "Операционная встреча", "27.08.2026", "alice",
                                 protocol_url="https://disk/p.docx", client=client)
        assert res[0]["ok"] and res[0]["weeek_task_id"] == 101
        assert res[1]["ok"] is False and "429" in res[1]["error"]
        assert res[2]["ok"] is False
        assert drafts[0]["status"] == "created" and drafts[0]["due"] == "2026-09-01"
        assert drafts[1]["status"] == "failed"
        assert "Основание" in client.calls[0]["description"] and "12:40" in client.calls[0]["description"]
        assert client.calls[0]["user_id"] == "u-kirill"
        # Повторный клик по созданной — не дубль
        res2 = wt.create_selected(drafts, [{"key": "j1:tasks:0"}], "tok", "x", "d", "alice", client=client)
        assert res2[0]["already"] is True and len(client.calls) == 2


class TestClient:
    def test_create_task_shape_and_due_update(self, monkeypatch):
        sent = []

        def fake_request(method, path, token, params=None, body=None, timeout=30):
            sent.append((method, path, body))
            if path == "/tm/tasks" and method == "POST":
                return {"success": True, "task": {"id": 555, "userId": body.get("userId")}}
            if path == "/ws":
                return {"workspace": {"id": "W1"}}
            return {"success": True}

        monkeypatch.setattr(weeek, "_request", fake_request)
        res = weeek.create_task("tok", "Заголовок", "<p>описание</p>", project_id="5",
                                board_id=19, column_id="58", user_id="u-1", due="2026-09-05")
        assert res == {"id": 555, "url": "https://app.weeek.net/ws/W1/task/555", "warnings": []}
        m, p, body = sent[0]
        assert (m, p) == ("POST", "/tm/tasks")
        assert body["locations"] == [{"projectId": 5, "boardId": 19, "boardColumnId": 58}]
        assert body["userId"] == "u-1" and body["type"] == "action"
        assert ("PUT", "/tm/tasks/555", {"dueDate": "2026-09-05"}) in sent

    def test_members_tolerant_parsing(self, monkeypatch):
        monkeypatch.setattr(weeek, "_request", lambda *a, **k: {
            "members": [{"id": "u1", "firstName": "Зоя", "lastName": "Р"},
                        {"id": "u2", "name": "Кирилл"}]})
        got = weeek.list_members("tok")
        assert got[0]["name"] == "Зоя Р" and got[1]["name"] == "Кирилл"


def test_retry_after_is_honoured(monkeypatch):
    import urllib.error
    waits = []
    monkeypatch.setattr(weeek.time, "sleep", lambda s: waits.append(s))
    calls = {"n": 0}

    class Resp:
        def __init__(self):
            self.payload = b'{"success": true, "tasks": []}'

        def read(self):
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(req, timeout=30):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {"Retry-After": "7"}, None)
        return Resp()

    monkeypatch.setattr(weeek.urllib.request, "urlopen", fake_open)
    weeek._request("GET", "/tm/tasks", "tok")
    assert waits and waits[0] == 7.0
