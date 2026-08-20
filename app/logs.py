"""Единая настройка логов: один вызов на процесс, вывод в stdout контейнера.

Раньше логгер был ровно один — в sms.py, — а всё остальное молчало: десятки
`except Exception: pass` проглатывали сбои без следа. Когда встреча не
записалась или протокол не прикрепился, узнать причину было неоткуда.

Уровень задаётся VTX_LOG_LEVEL (по умолчанию INFO). Формат однострочный, с
именем модуля и потока: у приложения много фоновых потоков (запись, выгрузка,
live-расшифровка), и без имени потока строки не разложить по задачам.
"""
from __future__ import annotations

import logging
import os
import sys
import threading

_configured = False
_lock = threading.Lock()

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(threadName)s] %(message)s"


def setup() -> None:
    """Настроить корневой логгер один раз. Повторные вызовы безвредны."""
    global _configured
    with _lock:
        if _configured:
            return
        level = (os.getenv("VTX_LOG_LEVEL") or "INFO").strip().upper()
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
        root = logging.getLogger()
        # Не трогаем чужие обработчики (uvicorn ставит свои) — добавляем свой,
        # только если корневой пуст.
        if not root.handlers:
            root.addHandler(handler)
        root.setLevel(getattr(logging, level, logging.INFO))
        # Библиотеки шумят на DEBUG независимо от нашего уровня.
        for noisy in ("urllib3", "httpx", "httpcore", "playwright", "PIL"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        _configured = True


def get(name: str) -> logging.Logger:
    """Логгер модуля: logs.get("vtx.scheduler")."""
    setup()
    return logging.getLogger(name)
