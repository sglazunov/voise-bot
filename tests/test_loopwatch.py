"""Сторож цикла событий и замер времени запроса в приложении (15.10).

Два раза подряд «сайт тормозит» разбирали по браузерному HAR, где видно лишь
«сервер молчал секунду». Теперь в каждом ответе есть Server-Timing, медленные
запросы попадают в лог, а остановка цикла событий — в лог со стеками потоков."""
from __future__ import annotations

import asyncio
import logging
import threading
import time

from app import loopwatch
from app.jobs import store
from conftest import register


def _run_loop():
    loop = asyncio.new_event_loop()
    th = threading.Thread(target=loop.run_forever, daemon=True)
    th.start()
    while not loop.is_running():
        time.sleep(0.01)
    return loop, th


def test_остановка_цикла_попадает_в_лог(monkeypatch, caplog):
    monkeypatch.setattr(loopwatch, "THRESHOLD_MS", 100)
    monkeypatch.setattr(loopwatch, "_PERIOD", 0.05)
    monkeypatch.setattr(loopwatch, "_REPORT_EVERY", 0.0)
    loop, th = _run_loop()
    caplog.set_level(logging.WARNING, logger="vtx.loopwatch")
    assert loopwatch.start(loop)
    time.sleep(0.2)
    loop.call_soon_threadsafe(time.sleep, 0.6)       # «тяжёлый» код в цикле
    deadline = time.time() + 5
    while time.time() < deadline and not any("стоял" in r.message for r in caplog.records):
        time.sleep(0.05)
    loop.call_soon_threadsafe(loop.stop)
    th.join(2)
    assert any("стоял" in r.message for r in caplog.records)


def test_сторож_уходит_вместе_с_циклом(monkeypatch):
    monkeypatch.setattr(loopwatch, "_PERIOD", 0.05)
    loop, th = _run_loop()
    before = {t.ident for t in threading.enumerate() if t.name == "vtx-loopwatch"}
    loopwatch.start(loop)
    loop.call_soon_threadsafe(loop.stop)
    th.join(2)
    time.sleep(0.3)
    alive = {t.ident for t in threading.enumerate() if t.name == "vtx-loopwatch"} - before
    assert not alive


def test_стеки_показывают_код_приложения():
    stop = threading.Event()

    class _Loop:                                     # вечно «работает»
        def is_closed(self): return stop.is_set()
        def is_running(self): return not stop.is_set()
        def call_soon_threadsafe(self, fn): fn()

    th = threading.Thread(target=loopwatch._watch, args=(_Loop(),), daemon=True)
    th.start()
    time.sleep(0.1)
    stacks = loopwatch._app_stacks(skip=threading.get_ident())
    stop.set()
    assert any("loopwatch.py" in s for s in stacks)


def test_server_timing_в_каждом_ответе(client):
    register(client, "alice", phone="+7 999 000-00-01")
    r = client.get("/api/jobs")
    assert r.headers["server-timing"].startswith("app;dur=")


def test_список_с_etag_отдаёт_304_без_изменений(client):
    register(client, "alice", phone="+7 999 000-00-01")
    store.create("Планёрка.mp4", "/data/uploads/x.mp4", "ru", False, owner="alice")
    r = client.get("/api/jobs")
    etag = r.headers["etag"]
    n = len(r.json())
    assert r.status_code == 200 and n >= 1
    r2 = client.get("/api/jobs", headers={"If-None-Match": etag})
    assert r2.status_code == 304 and r2.content == b""
    store.create("Ещё.mp4", "/data/uploads/y.mp4", "ru", False, owner="alice")
    r3 = client.get("/api/jobs", headers={"If-None-Match": etag})
    assert r3.status_code == 200 and len(r3.json()) == n + 1
