"""Список движков должен говорить, ЧЕЙ каждый пункт.

Боевой случай: подключён один «свой ключ» к OpenRouter, и выбор движка стал
сотней строк вида «Свой ключ · deepseek/deepseek-v4-flash-0731», «Свой ключ ·
thinkingmachines/inkling-small». По ним не понять, где OpenRouter, где Yandex
Cloud, а где локальный сервер — а выбрать нужно осознанно: модели различаются и
качеством, и лимитами, и ценой.

Поэтому каждый пункт несёт поставщика (`provider`), человеческое имя группы
(`group`) и имя модели без префикса (`model`) — интерфейс раскладывает их на два
поля: сначала поставщик, потом его модель.
"""
from __future__ import annotations

import json

from app import config, main


def _engines(monkeypatch, keys):
    monkeypatch.setattr(config, "OLLAMA_ENABLED", False)
    return main._engine_list(keys)


def test_у_каждого_движка_есть_группа(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    keys = {"gemini": [{"key": "k", "extra": ""}]}
    for e in _engines(monkeypatch, keys):
        assert e.get("group"), f"без группы: {e}"
        assert e.get("provider"), f"без поставщика: {e}"


def test_свой_ключ_группируется_по_поставщику(monkeypatch):
    """Под «своим ключом» живут РАЗНЫЕ сервисы: имя поставщика берётся из
    подключения, иначе OpenRouter и Yandex Cloud сливаются в одну кучу."""
    keys = {"custom": [
        {"key": "k1", "extra": json.dumps({
            "base_url": "https://openrouter.ai/api/v1", "hint": "OpenRouter",
            "models": ["deepseek/deepseek-v4", "qwen/qwen3.8-max"]})},
        {"key": "k2", "extra": json.dumps({
            "base_url": "https://ai.api.cloud.yandex.net/v1",
            "hint": "Yandex Cloud AI Studio",
            "models": ["gpt://b1g/deepseek-v4-flash/latest"]})},
    ]}
    engines = _engines(monkeypatch, keys)
    groups = {e["group"] for e in engines if e["provider"] == "custom"}
    assert groups == {"OpenRouter", "Yandex Cloud AI Studio"}


def test_модель_отделена_от_поставщика(monkeypatch):
    """Во втором поле показывается имя модели, а не строка целиком — иначе
    поставщик дублировался бы в каждом пункте."""
    keys = {"custom": [{"key": "k", "extra": json.dumps({
        "base_url": "https://openrouter.ai/api/v1", "hint": "OpenRouter",
        "models": ["deepseek/deepseek-v4-flash-0731"]})}]}
    e = [x for x in _engines(monkeypatch, keys) if x["provider"] == "custom"][0]
    assert e["model"] == "deepseek/deepseek-v4-flash-0731"
    assert e["value"] == "custom:deepseek/deepseek-v4-flash-0731"


def test_имя_модели_с_двоеточиями_не_рвётся(monkeypatch):
    """У Yandex Cloud имя вида gpt://<folder>/<model>: делить значение движка
    можно только по ПЕРВОМУ двоеточию."""
    model = "gpt://b1g9/deepseek-v4-flash/latest"
    keys = {"custom": [{"key": "k", "extra": json.dumps({
        "base_url": "https://ai.api.cloud.yandex.net/v1",
        "hint": "Yandex Cloud AI Studio", "models": [model]})}]}
    e = [x for x in _engines(monkeypatch, keys) if x["provider"] == "custom"][0]
    base, _, tail = e["value"].partition(":")
    assert base == "custom" and tail == model


def test_env_подключение_видно_рядом_с_ключами(monkeypatch):
    """Подключение из .env — ЕЩЁ ОДНО подключение, а не запасное.

    Раньше оно применялось только когда в интерфейсе нет ни одного ключа:
    подключив свой ключ, человек терял заданный в .env Yandex Cloud и не
    понимал, почему тот не появляется в списке.
    """
    monkeypatch.setattr(config, "YANDEX_CLOUD_API_KEY", "AQVN-ключ")
    monkeypatch.setattr(config, "YANDEX_CLOUD_FOLDER", "b1g9")
    monkeypatch.setattr(config, "YANDEX_CLOUD_MODEL", "deepseek-v4-flash/latest")
    keys = {"custom": [{"key": "свой-ключ", "extra": json.dumps({
        "base_url": "https://api.example.com/v1", "hint": "Свой сервис",
        "models": ["их/модель"]})}]}

    groups = {e["group"] for e in _engines(monkeypatch, keys)
              if e["provider"] == "custom"}
    assert groups == {"Свой сервис", "Yandex Cloud AI Studio"}, (
        "видны оба подключения — и из интерфейса, и из .env")


def test_openrouter_больше_не_предлагается():
    """Убран по решению владельца: и из подсказок по виду ключа, и из перебора
    адресов для незнакомого ключа."""
    assert not any("openrouter" in url.lower()
                   for _, _, url in config.CUSTOM_KEY_PREFIXES)
    assert not any("openrouter" in url.lower() for url in config.CUSTOM_GUESS_URLS)


class TestОтключениеПровайдера:
    """Провайдера надо уметь выключить, не удаляя ключи.

    Убрать его из VTX_PROVIDER_ORDER недостаточно: недостающие провайдеры
    дописываются обратно — иначе новый провайдер не появился бы в интерфейсе
    вовсе. Поэтому отключение отдельное и явное.

    Понадобилось, когда NVIDIA стала отвечать так медленно, что каждая встреча
    теряла на ней пять минут, прежде чем уйти к запасному движку.
    """

    def test_отключённый_не_доступен(self, monkeypatch):
        monkeypatch.setattr(config, "PROVIDER_DISABLED", {"nvidia"})
        keys = {"nvidia": [{"key": "k", "extra": ""}],
                "gemini": [{"key": "k", "extra": ""}]}
        assert "nvidia" not in config.available_providers(keys)
        assert "gemini" in config.available_providers(keys)

    def test_ключи_остаются_на_месте(self, monkeypatch):
        """Отключение обратимо: ключ никуда не делся, вернуть провайдера —
        правка одной строки в .env."""
        monkeypatch.setattr(config, "PROVIDER_DISABLED", {"nvidia"})
        keys = {"nvidia": [{"key": "секрет", "extra": ""}]}
        assert config.provider_creds("nvidia", keys) == [("секрет", "")]

    def test_отключённого_нет_в_списке_движков(self, monkeypatch):
        monkeypatch.setattr(config, "PROVIDER_DISABLED", {"nvidia"})
        keys = {"nvidia": [{"key": "k", "extra": ""}]}
        assert not [e for e in _engines(monkeypatch, keys)
                    if e["provider"] == "nvidia"]
