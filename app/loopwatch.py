"""Сторож цикла событий: кто держит сайт.

Сайт и вся фоновая работа (распознавание, сборка протокола, планировщик)
живут в ОДНОМ процессе, а Python исполняет одну строку кода за раз (GIL).
Тяжёлый чистый Python в любом потоке — и цикл событий uvicorn отвечает на
запросы с опозданием на секунды. Так уже было дважды: список встреч на 6 МБ
и поиск опоры в проверке протокола (HAR владельца 15.10). Оба раза причину
искали по браузерному HAR, где видно только «сервер долго молчал».

Сторож — отдельный поток: раз в полсекунды просит цикл выполнить пустой
вызов и ждёт. Не дождался за `VTX_LOOP_WATCH_MS` — в этот самый момент
снимает стеки всех потоков, где выполняется код приложения, и пишет в лог
`vtx.loopwatch`, сколько цикл стоял и чем был занят процесс. Не чаще раза в
минуту: при долгой занятости лог не должен превратиться в простыню.

    docker compose logs app | grep vtx.loopwatch | tail
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback

from . import logs

log = logs.get("vtx.loopwatch")

THRESHOLD_MS = int(os.getenv("VTX_LOOP_WATCH_MS", "300"))
_PERIOD = 0.5
_REPORT_EVERY = 60.0
_APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _app_stacks(skip: int) -> list[str]:
    """Стеки потоков, у которых в стеке есть код приложения: что они делают
    прямо сейчас (самый глубокий кадр приложения и три кадра над ним)."""
    names = {t.ident: t.name for t in threading.enumerate()}
    out: list[str] = []
    for ident, frame in sys._current_frames().items():
        if ident == skip:
            continue
        stack = traceback.extract_stack(frame)
        app = [i for i, f in enumerate(stack) if f.filename.startswith(_APP_DIR)]
        if not app:
            continue
        top = stack[app[-1]:][:4]
        where = " → ".join(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}"
                           for f in top)
        out.append(f"[{names.get(ident, ident)}] {where}")
    return out


def _watch(loop) -> None:
    me = threading.get_ident()
    last_report = 0.0
    while True:
        time.sleep(_PERIOD)
        if loop.is_closed() or not loop.is_running():
            return
        done = threading.Event()
        t0 = time.monotonic()
        try:
            loop.call_soon_threadsafe(done.set)
        except RuntimeError:          # цикл закрыли между проверкой и вызовом
            return
        if done.wait(THRESHOLD_MS / 1000):
            continue
        # Цикл опаздывает — снимок СЕЙЧАС, пока виновник ещё работает.
        stacks = _app_stacks(skip=me)
        while not done.wait(1.0):
            if loop.is_closed() or not loop.is_running():
                return
        lag = (time.monotonic() - t0) * 1000
        now = time.monotonic()
        if now - last_report < _REPORT_EVERY:
            continue
        last_report = now
        log.warning("Цикл событий стоял %d мс — все запросы сайта ждали. "
                    "Чем был занят процесс:\n  %s", lag,
                    "\n  ".join(stacks) or "(код приложения не выполнялся)")


def start(loop) -> bool:
    """Запустить сторожа для цикла `loop`. 0 в VTX_LOOP_WATCH_MS — выключен."""
    if THRESHOLD_MS <= 0:
        return False
    threading.Thread(target=_watch, args=(loop,), daemon=True,
                     name="vtx-loopwatch").start()
    return True
