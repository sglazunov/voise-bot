"""V2: горячие пути ходят в базу точечно, а не «прочитать всё → записать всё»."""
from __future__ import annotations

import time

import pytest

from app import db, security


@pytest.fixture
def pg(monkeypatch):
    """Подменяет слой БД словарём и считает вызовы."""
    store: dict = {}
    calls: list = []
    monkeypatch.setattr(db, "enabled", lambda: True)
    monkeypatch.setattr(db, "session_get",
                        lambda k: (calls.append(("get", k)), store.get(k))[1])
    monkeypatch.setattr(db, "session_put", lambda k, u, e: (
        calls.append(("put", k)), store.__setitem__(k, {"user": u, "exp": e}))[1])
    monkeypatch.setattr(db, "session_delete",
                        lambda k: (calls.append(("del", k)), store.pop(k, None))[1])
    monkeypatch.setattr(db, "sessions_delete_user", lambda u: (
        calls.append(("del_user", u)),
        [store.pop(k) for k, v in list(store.items()) if v["user"] == u])[1])
    monkeypatch.setattr(db, "sessions_prune", lambda now: calls.append(("prune",)))
    # Массовых операций на этом пути быть не должно вовсе — их больше нет в db.
    assert not hasattr(db, "sessions_load")
    assert not hasattr(db, "sessions_save")
    return store, calls


def test_session_roundtrip(pg):
    store, calls = pg
    token = security.create_session("Вася")
    assert security.session_user(token) == "вася"
    security.destroy_session(token)
    assert security.session_user(token) is None
    assert all(kind in ("get", "put", "del", "prune") for kind, *_ in calls)


def test_expired_session_deleted_by_key(pg):
    store, calls = pg
    key = security._token_key("t")
    store[key] = {"user": "вася", "exp": time.time() - 1}
    assert security.session_user("t") is None
    assert ("del", key) in calls, "протухшую сессию надо удалять по ключу"


def test_destroy_user_sessions_keeps_others(pg):
    store, _ = pg
    store[security._token_key("a")] = {"user": "вася", "exp": time.time() + 999}
    store[security._token_key("b")] = {"user": "петя", "exp": time.time() + 999}
    security.destroy_user_sessions("Вася")
    assert security.session_user("a") is None
    assert security.session_user("b") == "петя"


def test_user_lookup_is_pointwise(monkeypatch):
    seen: list = []
    monkeypatch.setattr(db, "enabled", lambda: True)
    monkeypatch.setattr(db, "user_get", lambda n: (
        seen.append(n),
        {"pw": "x", "team": "босс"} if n == "вася" else None)[1])
    # Если бы читалась вся таблица, users_load дёрнулся бы — уроним на нём.
    monkeypatch.setattr(db, "users_load", lambda: pytest.fail("читается вся таблица"))

    assert security.user_exists("Вася") is True
    assert security.user_exists("Никто") is False
    assert security.team_of("Вася") == "босс"
    assert security.team_of("Никто") == "никто"
    assert seen == ["вася", "никто", "вася", "никто"]
