"""Срок жизни ключа виден в интерфейсе.

У некоторых поставщиков бесплатный ключ выдаётся на срок. Когда он истекает,
протоколы начинают МОЛЧА собираться запасным движком: в шапке появляется
«Выбранный движок не ответил», а причина неочевидна — искать её будут долго.
Поэтому остаток срока показывается заранее, рядом с самим ключом.

Сейчас в KEY_TTL_DAYS пусто: у подключённых поставщиков объявленного срока нет.
Механизм от этого не исчез, поэтому тесты подставляют срок сами — иначе
проверялась бы пустота, а не поведение.
"""
import time

import pytest

from app import config, user_creds
from tests.conftest import register

TTL = 183


@pytest.fixture
def ttl(monkeypatch):
    """Поставщик с объявленным сроком жизни ключа."""
    monkeypatch.setattr(config, "KEY_TTL_DAYS", {"groq": TTL})


def _keys(client, provider="groq"):
    return client.get(f"/api/providers/keys?provider={provider}").json()["keys"]


def test_у_ключа_со_сроком_показан_остаток(client, ttl):
    register(client)
    user_creds.add("alice", "groq", "gsk-test-key-123456")
    k = _keys(client)[0]
    assert k["days_left"] > TTL - 10      # только что добавлен
    assert k["expires_at"] > time.time()


def test_истёкший_ключ_даёт_отрицательный_остаток(client, ttl):
    register(client)
    user_creds.add("alice", "groq", "gsk-old")
    raw = user_creds._read_raw("alice")
    raw["groq"][0]["at"] = time.time() - 200 * 86400   # добавлен 200 дней назад
    user_creds._write("alice", raw)
    assert _keys(client)[0]["days_left"] < 0


def test_у_провайдеров_без_срока_только_дата(client):
    """Срок жизни ключа заявлен не у всех — не выдумываем его."""
    register(client)
    user_creds.add("alice", "groq", "gsk-test")
    k = _keys(client, "groq")[0]
    assert "days_left" not in k and "expires_at" not in k
    assert k["added_at"] > 0


def test_старый_ключ_без_даты_не_ломает_список(client, ttl):
    """Ключи, добавленные до появления даты, просто без срока."""
    register(client)
    user_creds.add("alice", "groq", "gsk-legacy")
    raw = user_creds._read_raw("alice")
    raw["groq"][0].pop("at", None)
    user_creds._write("alice", raw)
    k = _keys(client)[0]
    assert "days_left" not in k and "added_at" not in k
    assert k["masked"]


def test_срок_объявляется_только_известным_провайдерам():
    """Таблица не должна называть провайдера, которого нет в KEY_PROVIDERS."""
    assert set(config.KEY_TTL_DAYS) <= set(config.KEY_PROVIDERS)
