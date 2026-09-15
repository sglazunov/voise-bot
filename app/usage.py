"""Расход модели: сколько токенов потратила команда и во сколько это обошлось.

Зачем отдельный модуль. Расход знает провайдер (он видит ответ шлюза), а
привязать его надо к ЗАДАЧЕ и к КОМАНДЕ — то есть к слою, который про
провайдеров ничего не знает. Тащить числа через `complete()` нельзя: у него
всюду сигнатура `(prompt, max_tokens, force_json) -> str`, и её меняли бы
разом в семи провайдерах, в ротации ключей и в цепочке отката.

Поэтому связь односторонняя и по потоку: `jobs` открывает сбор
(`with usage.collect() as acc:`), провайдеры внутри зовут `usage.record(...)`,
на выходе в `acc` лежит итог. Сбор — ПОТОКОВЫЙ (`threading.local`): распознавание
идёт в своём потоке, живая расшифровка встречи — в своём, пересборка — в
третьем, и путать их расход нельзя. Вложенные сборы складываются во внешний.

Чего этот модуль НЕ делает: не считает деньги, если цена модели не задана, и
не выдумывает её. Токены — измерение, цена — настройка (`VTX_MODEL_PRICES`).
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager

from . import logs

_LOG = logs.get("vtx.usage")
_local = threading.local()

# Стадии вызова модели (docs/ТЗ-МЕТРИКИ.md И8). Без них расход встречи —
# одно число, и вопрос «куда ушли деньги» остаётся без ответа: проверка цитат
# на длинной встрече стоит больше половины входа, а понять это можно, только
# разделив вызовы по стадиям.
MAP = "map"            # чтение фрагмента длинной встречи
MERGE = "merge"        # уплотнение заметок перед сведением
REDUCE = "reduce"      # сведение заметок в протокол (или единственный проход)
VERIFY = "verify"      # проверка цитат
REGEN = "regen"        # перегенерация одной темы
ASK = "ask"            # вопрос по встрече
_STAGES = (MAP, MERGE, REDUCE, VERIFY, REGEN, ASK)

# Сколько вызовов одной задачи запоминать поимённо. Часовая встреча — это
# десятки вызовов; потолок защищает от задачи, которую пересобирали весь день.
_MAX_LOG = 400


def _blank() -> dict:
    return {"calls": 0, "in": 0, "cached": 0, "out": 0, "by_model": {},
            # Поимённый список вызовов: модель, токены, СТАДИЯ и цена НА МОМЕНТ
            # ВЫЗОВА. ⚠️ Цену считаем здесь и больше не пересчитываем: прайсы
            # меняются, и себестоимость прошлого месяца, посчитанная сегодняшними
            # ценами, — выдумка.
            "log": []}


def _merge(dst: dict, src: dict) -> None:
    for k in ("calls", "in", "cached", "out"):
        dst[k] = int(dst.get(k) or 0) + int(src.get(k) or 0)
    for model, v in (src.get("by_model") or {}).items():
        e = dst.setdefault("by_model", {}).setdefault(
            model, {"calls": 0, "in": 0, "cached": 0, "out": 0})
        for k in ("calls", "in", "cached", "out"):
            e[k] += int(v.get(k) or 0)
    if src.get("log"):
        dst.setdefault("log", []).extend(src["log"])
        del dst["log"][:-_MAX_LOG]


@contextmanager
def stage(code: str):
    """Пометить, ЧЕМ занят движок внутри блока: карта, сведение, проверка…

    Стадия живёт в потоке рядом со сбором и не проходит через `complete()`: у
    него всюду сигнатура `(prompt, max_tokens, force_json) -> str`, и менять её
    в семи провайдерах ради метрики нельзя. Вложенные стадии не складываются —
    ближайшая побеждает (проверка внутри сборки протокола — это проверка).
    """
    prev = getattr(_local, "stage", None)
    _local.stage = code if code in _STAGES else None
    try:
        yield
    finally:
        _local.stage = prev


@contextmanager
def collect():
    """Собрать расход всех вызовов модели внутри блока — в этом потоке."""
    prev = getattr(_local, "acc", None)
    acc = _blank()
    _local.acc = acc
    try:
        yield acc
    finally:
        _local.acc = prev
        if prev is not None:          # вложенный сбор доливается во внешний
            _merge(prev, acc)


def record(model: str, in_tokens: int, cached: int, out_tokens: int) -> None:
    """Провайдер сообщает расход одного вызова. Вне сбора — молча никуда."""
    acc = getattr(_local, "acc", None)
    if acc is None:
        return
    name = model or "?"
    one = {"calls": 1, "in": int(in_tokens or 0), "cached": int(cached or 0),
           "out": int(out_tokens or 0)}
    _merge(acc, {**one, "by_model": {name: dict(one)}})
    # ⚠️ Цена берётся СЕЙЧАС и запоминается. Пересчитать потом нельзя: прайс
    # поставщика меняется, и себестоимость августа в сентябрьских ценах — не
    # измерение, а домысел (docs/ТЗ-МЕТРИКИ.md И8).
    acc.setdefault("log", []).append(
        {"model": name, "stage": getattr(_local, "stage", None), "at": time.time(),
         "in": one["in"], "cached": one["cached"], "out": one["out"],
         "usd": cost_usd({name: one})})
    del acc["log"][:-_MAX_LOG]


# --------------------------------------------------------------------------- #
# Цены
# --------------------------------------------------------------------------- #
# Долларов за МИЛЛИОН токенов: {"модель": [вход, выход]}. Совпадение по началу
# имени, поэтому «claude-opus-5» покрывает и «claude-opus-5-…».
#
# ⚠️ Здесь только те цены, которые я знаю из первых рук. Цены Gemini и
# «своего ключа» (шлюз выбирает владелец) СОЗНАТЕЛЬНО не проставлены: выдумать
# их — значит показать человеку уверенную цифру, которой нет. Задаются одной
# переменной, целиком заменяющей или дополняющей таблицу:
#
#   VTX_MODEL_PRICES={"gemini-3.1-flash-lite": [0.1, 0.4]}
#
# Пока цена не задана, считаются ТОЛЬКО токены, а деньги остаются пустыми —
# так честнее, чем «0 ₽ за встречу».
_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    # Groq, из его же каталога моделей (14.09.2026). Самое дешёвое, что есть
    # в проекте: вход у gpt-oss-120b в 13 раз дешевле Sonnet.
    "openai/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-20b": (0.075, 0.30),
}
# Доля цены входа, по которой считается токен, взятый из кэша. У Anthropic это
# около 0,1, у Gemini скидка около 75 % — берём осторожную середину, значение
# настраивается.
CACHED_RATE = float(os.getenv("VTX_CACHED_TOKEN_RATE", "0.25"))


def _load_extra_prices() -> None:
    raw = (os.getenv("VTX_MODEL_PRICES") or "").strip()
    if not raw:
        return
    try:
        for name, pair in (json.loads(raw) or {}).items():
            _PRICES[str(name)] = (float(pair[0]), float(pair[1]))
    except Exception as e:    # noqa: BLE001 — кривая настройка не должна ронять старт
        _LOG.warning("VTX_MODEL_PRICES не разобран (%s) — цены не заданы", e)


_load_extra_prices()


def price_of(model: str) -> tuple[float, float] | None:
    """Цена модели за миллион токенов (вход, выход) или None, если не задана."""
    name = (model or "").split("/", 1)[-1].strip()
    best = None
    for key, pair in _PRICES.items():       # самое длинное совпадение по началу
        if name.startswith(key) and (best is None or len(key) > len(best[0])):
            best = (key, pair)
    return best[1] if best else None


def cost_usd(by_model: dict) -> float | None:
    """Стоимость расхода. None — если хотя бы одна модель без цены: частичная
    сумма выглядела бы как полная и обманывала бы сильнее, чем пустое место."""
    total = 0.0
    seen = False
    for model, v in (by_model or {}).items():
        price = price_of(model)
        if price is None:
            return None
        fresh = max(0, int(v.get("in") or 0) - int(v.get("cached") or 0))
        total += (fresh * price[0]
                  + int(v.get("cached") or 0) * price[0] * CACHED_RATE
                  + int(v.get("out") or 0) * price[1]) / 1_000_000
        seen = True
    return round(total, 4) if seen else None
