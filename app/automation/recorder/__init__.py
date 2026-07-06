"""Record a Telemost meeting: a Chromium bot joins, ffmpeg captures A/V.

Public surface:
  readiness(cfg)                          -> what's installed / still missing
  record_meeting(url, out_path, cfg, ...) -> {ok, path, reason} | {ok: False, error}

The bot join (browser.py) and the capture (capture.py) are kept separate so the
capture backend can be swapped (Windows dshow now, Linux PulseAudio later) and so
each can be tuned independently. Live recording requires Playwright + Chromium +
ffmpeg + a loopback audio device on the host — see docs/automation-plan.md.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ... import config
from . import browser, capture

# --------------------------------------------------------------------------- #
# Parallel recording slots.
# Each concurrent recording gets an ISOLATED "slot": its own Xvfb display and its
# own PulseAudio null-sink. The browser is pinned to the slot's display + sink,
# and ffmpeg captures exactly that display + that sink's monitor — so two meetings
# recorded at the same time never bleed into each other's video or audio.
# Displays :99,:100,… and sinks meet0,meet1,… are created by docker/run.sh.
# --------------------------------------------------------------------------- #
MAX_SLOTS = max(1, min(int(os.getenv("VTX_MAX_CONCURRENT_RECORDINGS", "4") or "4"), 8))
_DISPLAY_BASE = int(os.getenv("VTX_DISPLAY_BASE", "99"))


@dataclass
class Slot:
    index: int
    display: str   # e.g. ":99"
    sink: str      # e.g. "meet0"
    source: str    # e.g. "meet0.monitor"


_slot_lock = threading.Lock()
_free_slots: list["Slot"] | None = None


def _init_slots() -> None:
    global _free_slots
    if _free_slots is None:
        _free_slots = [Slot(i, f":{_DISPLAY_BASE + i}", f"meet{i}", f"meet{i}.monitor")
                       for i in range(MAX_SLOTS)]


def acquire_slot() -> "Slot | None":
    """Take a free recording slot (display+sink), or None if all are busy."""
    with _slot_lock:
        _init_slots()
        return _free_slots.pop() if _free_slots else None


def release_slot(slot: "Slot | None") -> None:
    if slot is None:
        return
    with _slot_lock:
        _init_slots()
        if all(s.index != slot.index for s in _free_slots):
            _free_slots.append(slot)


def active_recordings() -> int:
    with _slot_lock:
        _init_slots()
        return MAX_SLOTS - len(_free_slots)


# The Telemost recorder bot is NOT part of this Linux "core" build. It needs a
# virtual display (Xvfb) + PulseAudio null-sink on the server, which is the next
# step. The full implementation below is kept intact and re-enabled by setting
# VTX_RECORDER_ENABLED=1 once that infrastructure is in place.
_RECORDER_ENABLED = os.getenv("VTX_RECORDER_ENABLED", "0") == "1"
_DISABLED_MSG = ("Бот-рекордер не входит в этот образ (Linux-ядро). Он будет добавлен "
                 "следующим шагом — с Xvfb и PulseAudio на сервере. "
                 "Распознавание, протоколы, Weeek и облако работают без него.")


def readiness(cfg: dict) -> dict:
    """What the screen recorder needs: a browser (Playwright) + ffmpeg + audio."""
    if not _RECORDER_ENABLED:
        return {"ready": False, "mode": "disabled", "detail": _DISABLED_MSG}
    b = browser.readiness(cfg)
    c = capture.readiness(cfg)
    return {"ready": bool(b.get("ready") and c.get("ready")), "mode": "screen",
            "browser": b, "capture": c,
            "auth_mode": cfg.get("auth_mode") or "guest"}


def record_meeting(url: str, out_path: str, cfg: dict,
                   on_log=None, should_stop=None, slot: "Slot | None" = None) -> dict:
    """Join `url` and screen-record the meeting (ffmpeg) until it ends, on the
    given isolated `slot` (its own Xvfb display + PulseAudio sink). The caller
    acquires the slot from the pool and releases it afterwards.
    """
    log = on_log or (lambda *_: None)
    if not _RECORDER_ENABLED:
        log(_DISABLED_MSG)
        return {"ok": False, "error": _DISABLED_MSG}
    if slot is None:
        return {"ok": False, "error": "Нет свободного слота записи."}
    # Pin the bot's browser to THIS slot's display + audio sink so its video and
    # sound are captured in isolation (never mixed with another parallel meeting).
    bot = browser.TelemostBot(cfg, on_log=log, display=slot.display, sink=slot.sink)
    rec = None
    try:
        if not bot.join(url, should_stop=should_stop):
            shot = str(Path(out_path).with_suffix(".join-failed.png"))
            bot.screenshot(shot)
            return {"ok": False,
                    "error": "Не удалось войти в встречу (см. скриншот). "
                             "Возможно, изменилась вёрстка Телемоста или встреча "
                             "требует входа в Яндекс.",
                    "screenshot": shot}

        max_sec = int(cfg.get("max_meeting_min", 240)) * 60
        alone_sec = int(cfg.get("end_when_alone_sec", 90))
        min_p = int(cfg.get("min_participants", 1))

        rec = capture.FFmpegRecorder(out_path, cfg, on_log=log,
                                     display=slot.display, source=slot.source)
        log(f"Бот в звонке (слот {slot.index}, экран {slot.display}). Запускаю запись…")
        rec.start()
        time.sleep(3)
        if not rec.running:
            return {"ok": False,
                    "error": "ffmpeg не смог записывать. " + (rec.error_tail() or
                             "Проверьте ffmpeg/дисплей/аудио слота.")}
        log("🔴 Идёт запись встречи — бот в звонке.")
        reason = bot.wait_until_end(should_stop, max_sec, alone_sec, min_p)
        log(f"Останавливаю запись (причина: {reason}).")
        rec.stop()
        p = Path(out_path)
        if not p.exists() or p.stat().st_size == 0:
            return {"ok": False, "reason": reason,
                    "error": "Файл записи пуст — проверьте аудио-устройство и ffmpeg."}
        return {"ok": True, "path": out_path, "reason": reason, "size": p.stat().st_size}
    except Exception as e:  # noqa: BLE001
        try:
            if rec:
                rec.stop()
        except Exception:
            pass
        return {"ok": False, "error": f"Ошибка записи: {e}"}
    finally:
        bot.close()
