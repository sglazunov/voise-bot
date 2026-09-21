"""Бот по ссылке — без задачи Weeek (2026-09-21).

Карточка получает task_id с префиксом `link-`; запись, облако, распознавание
и протокол идут как обычно, а всё, что пишется В Weeek, пропускается."""
from __future__ import annotations

import pytest

from app.automation import scheduler as sched_mod
from app.automation.scheduler import (MeetingState, Scheduler, is_adhoc,
                                      telemost_url)
from conftest import login, register


class TestСсылка:
    @pytest.mark.parametrize("raw,canon", [
        ("https://telemost.yandex.ru/j/78283935125180", "https://telemost.yandex.ru/j/78283935125180"),
        ("telemost.yandex.ru/j/1?x=1", "https://telemost.yandex.ru/j/1?x=1"),
        ("  HTTPS://Telemost.Yandex.RU/j/2  ", "https://telemost.yandex.ru/j/2"),
        ("https://telemost.yandex.com/j/3", "https://telemost.yandex.com/j/3"),
    ])
    def test_ссылка_телемоста_канонизируется(self, raw, canon):
        assert telemost_url(raw) == canon

    @pytest.mark.parametrize("raw", ["", "https://zoom.us/j/1", "https://telemost.yandex.ru/",
                                     "https://evil.example/telemost.yandex.ru/j/1", "не ссылка"])
    def test_чужие_и_пустые_отвергаются(self, raw):
        assert telemost_url(raw) is None

    def test_признак_встречи_по_ссылке(self):
        assert is_adhoc("link-20260921-1530-ab12") and not is_adhoc("11052") and not is_adhoc(None)


class TestЗапуск:
    @pytest.fixture
    def sched(self, monkeypatch):
        s = Scheduler()
        monkeypatch.setattr(sched_mod.security, "team_of", lambda u: "alice")
        monkeypatch.setattr(sched_mod.auto_settings, "load",
                            lambda u: {"do_protocol": True, "timezone": "Europe/Moscow"})
        monkeypatch.setattr(sched_mod.recorder, "acquire_slot", lambda: "slot0")
        monkeypatch.setattr(sched_mod.recorder, "release_slot", lambda s: None)
        launched = []
        monkeypatch.setattr(Scheduler, "_run", lambda self, st, slot, manual=False:
                            launched.append((st, slot, manual)))
        s.launched = launched
        return s

    def test_карточка_создаётся_и_запись_стартует(self, sched):
        res = sched.run_url("alice", "telemost.yandex.ru/j/555", title="  Демо   клиенту ")
        assert res["ok"] and res["task_id"].startswith("link-")
        st, slot, manual = sched.launched[0]
        assert manual is True and slot == "slot0"
        assert st.url == "https://telemost.yandex.ru/j/555" and st.title == "Демо клиенту"
        assert st.state == "recording" and st.owner == "alice" and st.do_protocol is True
        assert st.key.split(":", 2)[1] == st.task_id, "ключ карточки режется по «:»"
        assert st.key in sched._states and st.public()["adhoc"] is True

    def test_протокол_по_выбору_в_форме_а_не_по_общему_тумблеру(self, sched):
        sched.run_url("alice", "https://telemost.yandex.ru/j/556", do_protocol=False)
        assert sched.launched[0][0].do_protocol is False

    def test_без_названия_подставляется_дата(self, sched):
        sched.run_url("alice", "https://telemost.yandex.ru/j/557")
        assert sched.launched[0][0].title.startswith("Встреча по ссылке ")

    def test_плохая_ссылка_не_занимает_слот(self, sched, monkeypatch):
        taken = []
        monkeypatch.setattr(sched_mod.recorder, "acquire_slot", lambda: taken.append(1) or "s")
        res = sched.run_url("alice", "https://zoom.us/j/1")
        assert not res["ok"] and "Телемост" in res["error"] and not taken and not sched.launched

    def test_бот_уже_в_этой_комнате(self, sched):
        from datetime import datetime, timezone
        busy = MeetingState(key="alice:1:a", task_id="1", title="x",
                            url="https://telemost.yandex.ru/j/777",
                            start=datetime.now(timezone.utc), owner="alice", state="recording")
        with sched._lock:
            sched._states[busy.key] = busy
        res = sched.run_url("alice", "https://telemost.yandex.ru/j/777/?utm=1")
        assert not res["ok"] and "уже в этом звонке" in res["error"] and not sched.launched
        assert all(not is_adhoc(s.task_id) for s in sched._states.values()), "карточка не осталась"


class TestЭндпоинт:
    def test_плохая_ссылка_400_и_хорошая_200(self, client, monkeypatch):
        register(client); login(client)
        calls = []
        monkeypatch.setattr(sched_mod.scheduler, "run_url",
                            lambda user, url, title="", do_protocol=None:
                            (calls.append((url, title, do_protocol)) or
                             ({"ok": True, "detail": "Запись запущена.", "task_id": "link-x"}
                              if "telemost" in url else {"ok": False, "error": "Нужна ссылка на Телемост"})))
        r = client.post("/api/automation/scheduler/run-url", json={"url": "https://zoom.us/1"})
        assert r.status_code == 400 and "Телемост" in r.json()["detail"]
        r = client.post("/api/automation/scheduler/run-url",
                        json={"url": "https://telemost.yandex.ru/j/1", "title": "Демо", "do_protocol": False})
        assert r.status_code == 200 and r.json()["task_id"] == "link-x"
        assert calls[-1] == ("https://telemost.yandex.ru/j/1", "Демо", False)

    def test_без_входа_401(self, client):
        assert client.post("/api/automation/scheduler/run-url",
                           json={"url": "https://telemost.yandex.ru/j/1"}).status_code == 401
