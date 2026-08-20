"""Каждая настройка автоматизации должна СОХРАНЯТЬСЯ.

Модель запроса раньше была ручным списком полей и отстала от умолчаний на
одиннадцать ключей. Pydantic молча отбрасывал лишнее: фронт слал
telegram_bot_token, chat_stop_word, analyze_preset, strict_verify — и они не
сохранялись НИКОГДА. Telegram-уведомления включить было невозможно, стоп-слово
сменить тоже. Тестов на эти ключи не было, поэтому дефект жил незамеченным.

Здесь проверяется контракт целиком: что объявлено в _DEFAULTS, то и сохраняется.
"""
import pytest

from app.automation import settings as auto_settings
from tests.conftest import register

# Секреты возвращаются замаскированными — сверять их по ответу нельзя.
SECRET_KEYS = {"weeek_token", "telegram_bot_token"}
# Значения-образцы по типу умолчания.
def _sample(default):
    if isinstance(default, bool):
        return not default
    if isinstance(default, int) and not isinstance(default, bool):
        return int(default) + 7
    if isinstance(default, str):
        return (default + "-x") if default else "проверка"
    return None


def _keys():
    """Скалярные настройки: словари и списки проверяются отдельными тестами."""
    return {k: v for k, v in auto_settings._DEFAULTS.items()
            if isinstance(v, (bool, int, str)) and k not in SECRET_KEYS}


def test_каждая_настройка_доходит_до_хранилища(client):
    register(client)
    payload = {k: _sample(v) for k, v in _keys().items()}
    r = client.post("/api/automation/settings", json=payload)
    assert r.status_code == 200, r.text
    saved = auto_settings.load("alice")
    lost = [k for k, v in payload.items() if saved.get(k) != v]
    assert not lost, f"не сохранились: {lost}"


def test_ключи_из_умолчаний_не_отброшены_моделью(client):
    """Прямая проверка того, что сломалось: модель обязана знать ВСЕ ключи."""
    from app.main import AutomationSettings
    body = AutomationSettings(**{k: _sample(v) for k, v in _keys().items()})
    known = body.known()
    missing = [k for k in _keys() if k not in known]
    assert not missing, f"модель отбрасывает: {missing}"


def test_посторонние_ключи_не_сохраняются(client):
    """extra=allow принимает что угодно — в хранилище должно уйти только своё."""
    register(client)
    client.post("/api/automation/settings",
                json={"timezone": "Asia/Omsk", "мусор": 1, "__proto__": "x"})
    saved = auto_settings.load("alice")
    assert saved.get("timezone") == "Asia/Omsk"
    assert "мусор" not in saved and "__proto__" not in saved


@pytest.mark.parametrize("key", ["chat_stop_word", "telegram_chat_id",
                                 "analyze_preset", "weeek_record_field"])
def test_пустая_строка_очищает_поле(client, key):
    """Пустая строка — это «очистить», а не «не трогали». Раньше очистить
    проект Weeek, фильтры или папку протоколов было невозможно."""
    register(client)
    client.post("/api/automation/settings", json={key: "значение"})
    client.post("/api/automation/settings", json={key: ""})
    assert auto_settings.load("alice").get(key) == ""
