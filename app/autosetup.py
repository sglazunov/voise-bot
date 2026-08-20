"""Подготовка при старте: предзагрузка модели распознавания речи.

Раньше здесь стояла автоустановка недостающих компонентов через pip и apt-get.
В контейнере она была невозможна: процесс идёт под пользователем `app` (uid
1000), sudo нет, а pip без root кладёт пакеты в /home/app/.local — каталог не на
томе, так что после пересоздания контейнера установленное пропадало. Всё
необходимое собрано в образе, поэтому автоустановки больше нет — осталась
только полезная часть: модель Whisper скачивается заранее, а не при первой
встрече, иначе первая запись ждёт полтора гигабайта загрузки.

Что грузить, задаёт VTX_PRELOAD_MODELS: "1" (по умолчанию) — только рабочую
модель (~1.6 ГБ), "all" — все предлагаемые (~6 ГБ), "0"/"none" — ничего.
Готовность компонентов при этом по-прежнему видна: см. deps_setup.status().
"""
from __future__ import annotations

import os
import threading

from . import deps_setup

_state: dict = {"state": "idle", "message": ""}
_started = False
_lock = threading.Lock()


def status() -> dict:
    """Прогресс подготовки + готовность компонентов, для интерфейса."""
    return {**_state, "components": deps_setup.status()}


def _run() -> None:
    scope = os.getenv("VTX_PRELOAD_MODELS", "1").strip().lower()
    if scope in ("0", "none", "false", "no"):
        _state.update(state="done", message="Предзагрузка моделей отключена.")
        return
    _state.update(state="running", message="Загружаю модель распознавания речи…")
    try:
        from . import whisper_setup
        models = whisper_setup.PRELOAD_MODELS if scope in ("all", "*") else None
        whisper_setup.preload_all(models)
        _state.update(state="done", message="Модель распознавания речи готова.")
    except Exception as e:      # noqa: BLE001 — подготовка не должна ронять старт
        _state.update(state="error",
                      message=f"Не удалось скачать модель заранее: {e}. "
                              "Она догрузится при первой расшифровке.")


def ensure_all() -> None:
    """Запустить подготовку один раз за процесс. Вызывать можно сколько угодно."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_run, daemon=True, name="vtx-autosetup").start()
