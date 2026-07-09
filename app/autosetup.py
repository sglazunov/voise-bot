"""First-run auto-setup: make every feature work out of the box, on any machine.

On startup we check each optional component and install whatever is missing —
in the BACKGROUND, without a single click:

  * ocr            — Pillow + pytesseract + av + the Tesseract engine (rus+eng).
                     This is what powers "текст/код с экрана" and reading the
                     speaker's name from the video.
  * ffmpeg         — decoding and screen/audio capture.
  * playwright     — Chromium for the Telemost recorder bot (only when enabled).
  * audio_loopback — the PulseAudio null-sink used to capture meeting sound
                     (Linux only: it's just a `pactl` call, no downloads/prompts).
  * speech models  — all Whisper models, pre-downloaded (see whisper_setup).

Heavy installs stay opt-in: diarization pulls ~2.5 GB of torch, so it only runs
when VTX_DIARIZATION=1.

In Docker everything is already baked into the image, so this is a cheap no-op
there — it's what makes a bare-metal or first-run install work unattended.
Disable with VTX_AUTO_SETUP=0.
"""
from __future__ import annotations

import os
import threading
import time

from . import deps_setup

_state: dict = {"state": "idle", "message": "", "done": [], "failed": []}
_started = False
_lock = threading.Lock()


def status() -> dict:
    """Auto-setup progress + per-component readiness, for the UI."""
    return {**_state, "components": deps_setup.status()}


def _wanted() -> list[str]:
    """Components we install unattended on this machine."""
    want = ["ocr", "ffmpeg"]
    if os.getenv("VTX_RECORDER_ENABLED", "0") == "1":
        # The loopback is just a pactl null-sink: instant, no download.
        want += ["playwright", "audio_loopback"]
    if os.getenv("VTX_DIARIZATION", "0") == "1":
        want.append("diarization")   # ~2.5 GB, only when explicitly enabled
    return want


def _run() -> None:
    _state["state"] = "running"
    for c in _wanted():
        try:
            if deps_setup.component_ready(c):
                _state["done"].append(c)
                continue
            _state["message"] = f"Устанавливаю: {deps_setup.label(c)}…"
            deps_setup.install(c)
            while deps_setup.install_state(c).get("state") == "running":
                time.sleep(1)
            (_state["done"] if deps_setup.component_ready(c)
             else _state["failed"]).append(c)
        except Exception:          # one component must never stop the rest
            _state["failed"].append(c)

    # Speech recognition models — download them all in the background too.
    if os.getenv("VTX_PRELOAD_MODELS", "1") == "1":
        try:
            from . import whisper_setup
            _state["message"] = "Загружаю модели распознавания речи…"
            whisper_setup.preload_all()
        except Exception:
            pass

    _state["state"] = "done"
    _state["message"] = ("Всё готово." if not _state["failed"] else
                         "Готово частично — не удалось: "
                         + ", ".join(deps_setup.label(c) for c in _state["failed"]))


def ensure_all() -> None:
    """Kick off the background setup once per process. Safe to call repeatedly."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_run, daemon=True, name="vtx-autosetup").start()
