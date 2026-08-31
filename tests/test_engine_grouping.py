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
