"""Разбор ответа модели: протокол не должен теряться из-за лишнего текста.

Боевой случай: движок вернул готовый протокол, а следом — пояснение. Разбор
падал с «Extra data: line 79 column 1», встреча уходила запасному движку, и
целая работа модели выбрасывалась. Модели дописывают вокруг JSON регулярно:
ограда кода, «Вот ваш протокол», следы размышлений у reasoning-моделей.
"""
from __future__ import annotations

import json

import pytest

from app.analyze import _extract_json

PROTO = '{"summary": "итог", "tasks": [{"task": "сделать", "owner": "Пётр"}]}'


@pytest.mark.parametrize("raw, why", [
    (PROTO, "чистый ответ"),
    (f"```json\n{PROTO}\n```", "в ограде кода"),
    (f"```\n{PROTO}\n```", "ограда без языка"),
    (f"{PROTO}\n\nВот ваш протокол встречи.", "пояснение ПОСЛЕ объекта"),
    (f"Готово:\n{PROTO}", "пояснение перед объектом"),
    (f"Думаю…\n{PROTO}\nГотово!", "текст с обеих сторон"),
    (f"{PROTO}\n{{\"другой\": 1}}", "второй объект следом"),
])
def test_протокол_достаётся(raw, why):
    got = _extract_json(raw)
    assert got["summary"] == "итог", why
    assert got["tasks"][0]["owner"] == "Пётр", why


def test_берётся_именно_первый_объект():
    """Второй объект — это не продолжение протокола, а мусор от модели."""
    got = _extract_json(PROTO + '\n{"summary": "не тот"}')
    assert got["summary"] == "итог"


def test_вложенные_скобки_не_ломают_разбор():
    raw = '{"a": {"b": {"c": [1, 2, {"d": "}"}]}}}\nхвост'
    assert _extract_json(raw)["a"]["b"]["c"][2]["d"] == "}"


def test_без_json_понятная_ошибка():
    with pytest.raises(json.JSONDecodeError):
        _extract_json("Извините, не могу выполнить эту просьбу.")
