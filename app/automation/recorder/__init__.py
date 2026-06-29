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
import sys
import threading
import time
from pathlib import Path

from ... import config
from . import browser, capture

# Machine-wide single-recording guard. A browser + ffmpeg are exclusive, and the
# bot must never join a meeting twice. Two layers: an in-process lock (threads in
# the SAME process share a PID, so a file lock alone can't tell them apart) plus
# a PID file lock that also blocks a second app instance. Covers every caller —
# the scheduler, the manual "test" endpoint, and a second process.
_LOCK_PATH = config.DATA_DIR / "recorder.lock"
_PROC_LOCK = threading.Lock()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _acquire_lock() -> bool:
    """Take the in-process lock, then the PID file lock (stealing it only if the
    previous owner process died). Returns False if a recording is already live."""
    if not _PROC_LOCK.acquire(blocking=False):
        return False  # another recording is running in THIS process
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                owner = int(_LOCK_PATH.read_text().strip() or "0")
            except (OSError, ValueError):
                owner = 0
            # Our own stale PID is safe to reclaim (the in-process lock above
            # already proved no live recording here); a dead foreign PID too.
            if owner == os.getpid() or not _pid_alive(owner):
                try:
                    _LOCK_PATH.unlink()
                except OSError:
                    break
                continue
            break  # a different, live process owns it
    _PROC_LOCK.release()
    return False


def _release_lock() -> None:
    try:
        if _LOCK_PATH.exists() and _LOCK_PATH.read_text().strip() == str(os.getpid()):
            _LOCK_PATH.unlink()
    except OSError:
        pass
    finally:
        try:
            _PROC_LOCK.release()
        except RuntimeError:
            pass


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
                   on_log=None, should_stop=None) -> dict:
    """Join `url` and screen-record the meeting (ffmpeg) until it ends.

    The bot joins (as a guest — no login needed) and ffmpeg captures the whole
    screen + the configured audio device for the full meeting (no 30-min limit).
    """
    log = on_log or (lambda *_: None)
    if not _RECORDER_ENABLED:
        log(_DISABLED_MSG)
        return {"ok": False, "error": _DISABLED_MSG}
    # Refuse to start a second recording anywhere on this machine — otherwise the
    # bot can join the same meeting twice (scheduler + manual test, or two app
    # instances), as seen with two "Протокол-бот" tiles in one call.
    if not _acquire_lock():
        log("Запись уже идёт (другой бот/экземпляр) — второй запуск отменён.")
        return {"ok": False, "error": "Запись уже идёт в этой системе "
                "(другой бот или второй экземпляр приложения). Второй бот не запущен."}
    bot = browser.TelemostBot(cfg, on_log=log)
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

        # Capture only the meeting's browser window; if ffmpeg can't grab that
        # window (title mismatch etc.) it dies in ~1s — detect that and fall
        # back to capturing the whole desktop so the recording isn't lost.
        title = bot.window_title()
        rec = capture.FFmpegRecorder(out_path, cfg, on_log=log, window_title=title)
        log(f"Бот в звонке. Запускаю запись окна «{title or '—'}»…")
        rec.start()
        time.sleep(3)
        if not rec.running:
            err = rec.error_tail()
            log(f"Захват окна не запустился ({err}). Перехожу на запись всего экрана.")
            rec = capture.FFmpegRecorder(out_path, cfg, on_log=log, window_title=None)
            rec.start()
            time.sleep(3)
            if not rec.running:
                return {"ok": False,
                        "error": "ffmpeg не смог записывать. " + (rec.error_tail() or
                                 "Проверьте ffmpeg и аудио-устройство (выберите рабочее "
                                 "из списка).")}
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
        _release_lock()
