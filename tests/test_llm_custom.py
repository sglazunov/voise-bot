"""Любой провайдер по одному ключу: адрес, авторизация и модели — сами.

Зачем это вообще. Под каждого поставщика писался свой класс, а поставщики
меняются: NVIDIA за неделю дважды поменяла состав бесплатных моделей и отобрала
DeepSeek, из-за чего протоколы молча уходили запасному движку. Универсальный
провайдер снимает саму причину: подключается что угодно OpenAI-совместимое.
"""
from __future__ import annotations

import json

import pytest

from app import llm_custom
from app.llm_custom import CustomProvider, detect, hint_for


class TestПодсказкаПоВидуКлюча:
    """Вид ключа сужает поиск: не перебирать же все известные адреса подряд."""

    def test_известные_ключи_узнаются(self):
        assert hint_for("nvapi-abc")[0] == "NVIDIA NIM"
        assert hint_for("gsk_abc")[0] == "Groq"
        assert hint_for("sk-or-abc")[0] == "OpenRouter"

    def test_незнакомый_ключ_не_выдумывает_поставщика(self):
        assert hint_for("abcdef")[0] == ""
        assert hint_for("")[0] == ""


class TestОпределение:
    def _fake_models(self, monkeypatch, ok_url, ok_style, models):
        def fake(base_url, key, style, timeout=15):
            return list(models) if (base_url == ok_url and style == ok_style) else []
        monkeypatch.setattr(llm_custom, "list_models", fake)

    def test_адрес_и_авторизация_подбираются(self, monkeypatch):
        self._fake_models(monkeypatch, "https://api.example.com/v1", "bearer",
                          ["m-1", "m-2"])
        got = detect("sk-whatever", "https://api.example.com/v1")
        assert got["ok"] and got["auth"] == "bearer"
        assert got["models"] == ["m-1", "m-2"]

    def test_нестандартный_заголовок_тоже_находится(self, monkeypatch):
        """MiMo от Xiaomi ждёт заголовок api-key, а не Authorization: Bearer."""
        self._fake_models(monkeypatch, "https://api.xiaomimimo.com/v1", "api-key",
                          ["mimo-v2.5-pro"])
        got = detect("любой-ключ", "https://api.xiaomimimo.com/v1")
        assert got["ok"] and got["auth"] == "api-key"

    def test_известный_ключ_не_требует_адреса(self, monkeypatch):
        self._fake_models(monkeypatch, "https://api.groq.com/openai/v1", "bearer",
                          ["llama"])
        got = detect("gsk_abc")
        assert got["ok"] and got["base_url"] == "https://api.groq.com/openai/v1"

    def test_негодный_ключ_даёт_понятный_отказ(self, monkeypatch):
        monkeypatch.setattr(llm_custom, "list_models",
                            lambda *a, **k: [])
        got = detect("мусор", "https://api.example.com/v1")
        assert not got["ok"] and "не принят" in got["error"]

    def test_пустой_ключ_не_ходит_в_сеть(self, monkeypatch):
        monkeypatch.setattr(llm_custom, "list_models", lambda *a, **k: pytest.fail(
            "при пустом ключе запросов быть не должно"))
        assert detect("")["ok"] is False


class TestПровайдер:
    def _prov(self, **cfg):
        base = {"base_url": "https://api.example.com/v1", "auth": "bearer",
                "model": "m-1"}
        base.update(cfg)
        return CustomProvider(api_key="k", extra=json.dumps(base))

    def test_настройки_читаются_из_extra(self):
        p = self._prov()
        assert p.base_url == "https://api.example.com/v1"
        assert p.model == "m-1"
        assert p._headers() == {"Authorization": "Bearer k"}

    def test_нестандартный_заголовок(self):
        assert self._prov(auth="api-key")._headers() == {"api-key": "k"}

    def test_без_настроек_понятная_ошибка(self):
        """Молчаливый сбой здесь означал бы протокол, ушедший запасному движку
        без объяснения причины."""
        p = CustomProvider(api_key="k", extra="")
        with pytest.raises(RuntimeError, match="не настроен"):
            p.complete("текст")

    def test_битый_extra_не_роняет_создание(self):
        p = CustomProvider(api_key="k", extra="{это не json")
        assert p.base_url == "" and p.model == ""

    def test_запрос_уходит_по_нужному_адресу(self, monkeypatch):
        seen = {}

        def fake_post(url, payload, headers, timeout=None, max_retries=None):
            seen.update(url=url, payload=payload, headers=headers)
            return {"choices": [{"message": {"content": '{"ok": 1}'}}]}

        monkeypatch.setattr(llm_custom, "_http_post_json", fake_post)
        assert self._prov().complete("текст") == '{"ok": 1}'
        assert seen["url"] == "https://api.example.com/v1/chat/completions"
        assert seen["payload"]["model"] == "m-1"
        assert seen["headers"]["Authorization"] == "Bearer k"

    def test_response_format_снимается_при_отказе(self, monkeypatch):
        """Его понимают не все модели, а JSON мы всё равно проверяем сами."""
        calls: list[dict] = []

        def fake_post(url, payload, headers, timeout=None, max_retries=None):
            calls.append(dict(payload))
            if "response_format" in payload:
                raise RuntimeError("HTTP 400: response_format is not supported")
            return {"choices": [{"message": {"content": "{}"}}]}

        monkeypatch.setattr(llm_custom, "_http_post_json", fake_post)
        self._prov().complete("текст")
        assert len(calls) == 2
        assert "response_format" in calls[0] and "response_format" not in calls[1]
