"""Срок жизни ключа виден в интерфейсе.

Бесплатный ключ NVIDIA действует полгода. Когда он истекает, протоколы
начинают МОЛЧА собираться запасным движком: в шапке появляется «Выбранный
движок не ответил», а причина неочевидна — искать её будут долго. Поэтому
остаток срока показывается заранее, рядом с самим ключом.
"""
import time

from app import config, user_creds
from tests.conftest import register


def _keys(client, provider="nvidia"):
    return client.get(f"/api/providers/keys?provider={provider}").json()["keys"]


def test_у_ключа_nvidia_показан_остаток(client):
    register(client)
    user_creds.add("alice", "nvidia", "nvapi-test-key-123456")
    k = _keys(client)[0]
    assert k["days_left"] > 170          # только что добавлен, TTL 183 дня
    assert k["expires_at"] > time.time()


def test_истёкший_ключ_даёт_отрицательный_остаток(client):
    register(client)
    user_creds.add("alice", "nvidia", "nvapi-old")
    raw = user_creds._read_raw("alice")
    raw["nvidia"][0]["at"] = time.time() - 200 * 86400   # добавлен 200 дней назад
    user_creds._write("alice", raw)
    assert _keys(client)[0]["days_left"] < 0


def test_у_провайдеров_без_срока_только_дата(client):
    """У Groq и прочих срок жизни ключа не заявлен — не выдумываем его."""
    register(client)
    user_creds.add("alice", "groq", "gsk-test")
    k = _keys(client, "groq")[0]
    assert "days_left" not in k and "expires_at" not in k
    assert k["added_at"] > 0


def test_старый_ключ_без_даты_не_ломает_список(client):
    """Ключи, добавленные до появления даты, просто без срока."""
    register(client)
    user_creds.add("alice", "nvidia", "nvapi-legacy")
    raw = user_creds._read_raw("alice")
    raw["nvidia"][0].pop("at", None)
    user_creds._write("alice", raw)
    k = _keys(client)[0]
    assert "days_left" not in k and "added_at" not in k
    assert k["masked"]


def test_срок_объявлен_только_для_nvidia():
    assert config.KEY_TTL_DAYS.get("nvidia") == 183
    assert set(config.KEY_TTL_DAYS) <= set(config.KEY_PROVIDERS)
