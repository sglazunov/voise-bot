"""Готовность необязательных компонентов — только ПРОВЕРКА, без установки.

Раньше здесь же жила установка через pip и apt-get. В образе она работать не
могла: процесс идёт под пользователем `app` (uid 1000), sudo нет, а pip под
не-root кладёт пакеты в /home/app/.local — не на том, и всё пропадает при
пересоздании контейнера. Всё нужное собрано в образе (см. Dockerfile), поэтому
осталась только проверка:

  playwright     — установлен ли пакет и скачан ли Chromium
  diarization    — есть ли torch + pyannote.audio
  ffmpeg         — есть ли исполняемый файл
  ocr            — Pillow + pytesseract + av и движок Tesseract
  audio_loopback — существует ли PulseAudio-монитор (создаёт docker/run.sh)
"""
from __future__ import annotations

import glob
import importlib.util
import os
import shutil

COMPONENTS = ("playwright", "diarization", "ffmpeg", "ocr", "audio_loopback")
_LABELS = {
    "playwright": "Запись встреч (Playwright + Chromium)",
    "diarization": "«Кто говорил» (torch + pyannote.audio)",
    "ffmpeg": "ffmpeg (запись и конвертация видео/аудио)",
    "ocr": "Текст с экрана и имена говорящих (Pillow + pytesseract + Tesseract)",
    "audio_loopback": "Виртуальное аудио для записи звука (PulseAudio)",
}

def _has(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


def _chromium_installed() -> bool:
    """Whether Playwright's Chromium is on disk — PLAYWRIGHT_BROWSERS_PATH (e.g.
    /ms-playwright in the image) or the default ~/.cache/ms-playwright."""
    bases = []
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        bases.append(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    bases.append(os.path.expanduser("~/.cache/ms-playwright"))
    for base in bases:
        if base and glob.glob(os.path.join(base, "chromium-*")):
            return True
    try:  # definitive fallback: ask Playwright where the browser is
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return bool(p.chromium.executable_path and
                        os.path.exists(p.chromium.executable_path))
    except Exception:
        return False


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def find_tesseract() -> str | None:
    return shutil.which("tesseract")


def _loopback_device_present() -> bool:
    """True when a PulseAudio monitor source exists — that's the "virtual cable"
    ffmpeg records the meeting's sound from. The container creates the null-sinks
    at startup (docker/run.sh)."""
    try:
        from .automation.recorder import capture
        names = capture.list_audio_devices()
        target = os.environ.get("VTX_PULSE_MONITOR", "meet0.monitor")
        return any(n == target or n.endswith(".monitor") for n in names)
    except Exception:
        return False


def component_ready(c: str) -> bool:
    if c == "playwright":
        return _has("playwright") and _chromium_installed()
    if c == "diarization":
        return _has("torch") and _has("pyannote.audio")
    if c == "ffmpeg":
        return find_ffmpeg() is not None
    if c == "ocr":
        # `av` decodes the video frames we OCR (screen text) and read speaker
        # names from, so it's part of "OCR works" just like Pillow/Tesseract.
        return (_has("PIL") and _has("pytesseract") and _has("av")
                and find_tesseract() is not None)
    if c == "audio_loopback":
        return _loopback_device_present()
    return False


def label(c: str) -> str:
    return _LABELS.get(c, c)


def status() -> dict:
    """Готовность каждого компонента — для интерфейса."""
    return {c: {"label": _LABELS[c], "ready": component_ready(c)}
            for c in COMPONENTS}
