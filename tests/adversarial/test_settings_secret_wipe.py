"""Атака: круговой рейс настроек уничтожает токен Weeek, а интерфейс молчит.

`GET /api/automation/settings` отдаёт секреты как ПРИЗНАК НАЛИЧИЯ
(`settings.redacted`: `weeek_token: true`). `POST /api/automation/settings`
принимает любые ключи из `_DEFAULTS` без проверки типа, а
`settings._transform_secrets` шифрует ТОЛЬКО строки (`isinstance(val, str)`).
Значит `true` кладётся в хранилище как есть.

Последствия:
  * настоящий токен затёрт безвозвратно (в файле/БД теперь `true`);
  * `redacted()` снова отдаёт `true` — карточка Weeek показывает «подключено»;
  * планировщик пропускает проверку `if not cfg.get("weeek_token")` (True —
    истина) и каждые две минуты ходит в Weeek с заголовком «Bearer True»,
    получая отказ. Встречи просто перестают появляться.

Никакого «злоумышленника» тут не требуется: это обычный round-trip
«прочитал настройки — сохранил настройки», который любой клиент API сделает
первым делом. Сервер обязан такой ввод отбить, а не молча съесть.
"""
from __future__ import annotations

import pytest

from app.automation import settings as auto_settings
from conftest import register


class TestSecretRoundTrip:
    def test_module_level_roundtrip_keeps_the_token(self):
        auto_settings.save("alice", {"weeek_token": "wk-REAL-TOKEN"})
        shown = auto_settings.redacted("alice")["weeek_token"]
        assert shown is True                      # предпосылка: отдаём признак

        auto_settings.save("alice", {"weeek_token": shown})

        assert auto_settings.load("alice")["weeek_token"] == "wk-REAL-TOKEN", (
            "круговой рейс подменил токен на "
            f"{auto_settings.load('alice')['weeek_token']!r}")

    def test_api_roundtrip_keeps_the_token(self, client):
        register(client)
        auto_settings.save("alice", {"weeek_token": "wk-REAL-TOKEN"})

        shown = client.get("/api/automation/settings").json()
        assert shown["weeek_token"] is True

        r = client.post("/api/automation/settings", json=shown)
        assert r.status_code in (200, 400, 422)

        assert auto_settings.load("alice")["weeek_token"] == "wk-REAL-TOKEN", (
            "POST тем, что отдал GET, стёр токен Weeek")

    def test_nested_cloud_secret_survives_roundtrip(self, client):
        register(client)
        auto_settings.save("alice", {"gdrive": {"refresh_token": "1//0_REAL"}})
        shown = auto_settings.redacted("alice")["gdrive"]
        assert shown["refresh_token"] is True

        auto_settings.save("alice", {"gdrive": shown})

        assert auto_settings.load("alice")["gdrive"]["refresh_token"] == "1//0_REAL", (
            "круговой рейс стёр refresh_token Google Drive")

    def test_ui_indicator_matches_reality(self):
        """Индикатор «подключено» обязан совпадать с тем, что есть на самом деле.

        Раньше круговой рейс затирал токен, а `redacted` продолжал отдавать
        True — карточка Weeek показывала «подключено», планировщик ходил в API с
        «Bearer True», и причину искали на стороне Weeek. Теперь True из
        `redacted` означает «не меняли» и игнорируется, поэтому проверяем оба
        конца: после рейса токен цел и индикатор горит честно, а после явной
        очистки пустой строкой он гаснет.
        """
        auto_settings.save("alice", {"weeek_token": "wk-REAL-TOKEN"})
        auto_settings.save("alice", {"weeek_token": True})

        assert auto_settings.load("alice")["weeek_token"] == "wk-REAL-TOKEN", (
            "круговой рейс не должен трогать секрет")
        assert auto_settings.redacted("alice")["weeek_token"] is True, (
            "токен на месте — индикатор обязан это показывать")

        auto_settings.save("alice", {"weeek_token": ""})    # осознанная очистка
        assert auto_settings.load("alice")["weeek_token"] == ""
        assert auto_settings.redacted("alice")["weeek_token"] is False, (
            "токен очищен — карточка не должна показывать «подключено»")
