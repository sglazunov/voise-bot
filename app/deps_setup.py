"""On-demand install of optional dependencies — Linux only.

The whole point of the pip part: installs run via ``sys.executable -m pip``, so
packages land in the SAME interpreter the app runs from (the project ``.venv`` /
the container's Python), not some other one. That is the fix for the classic
"I ran pip install but the app still says it's missing".

System packages go through ``apt-get`` (needs root, or passwordless sudo).

Mirrors ``ollama_setup.py``: ``status()`` / ``install(component)``, with progress
polled by the UI. Components:

  playwright     -> pip install playwright  +  `playwright install chromium`
  diarization    -> pip install torch pyannote.audio  (~2.5 GB)
  ffmpeg         -> apt install ffmpeg
  ocr            -> pip Pillow/pytesseract/av + apt tesseract-ocr(+rus)
  audio_loopback -> a PulseAudio null-sink (created by the container at startup)

Normally you don't touch any of this: `autosetup.ensure_all()` runs it for you on
first launch, and the Docker image already ships everything.
"""
from __future__ import annotations

import glob
import importlib.util
import os
import shutil
import subprocess
import sys
import threading

COMPONENTS = ("playwright", "diarization", "ffmpeg", "ocr", "audio_loopback")
_LABELS = {
    "playwright": "Запись встреч (Playwright + Chromium)",
    "diarization": "«Кто говорил» (torch + pyannote.audio)",
    "ffmpeg": "ffmpeg (запись и конвертация видео/аудио)",
    "ocr": "Текст с экрана и имена говорящих (Pillow + pytesseract + Tesseract)",
    "audio_loopback": "Виртуальное аудио для записи звука (PulseAudio)",
}

_install = {c: {"state": "idle", "message": "", "ok": None} for c in COMPONENTS}
_lock = threading.Lock()


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
        names = capture.list_audio_devices("ffmpeg")
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


def install_state(c: str) -> dict:
    """Current install progress of one component (public view of _install)."""
    return dict(_install.get(c, {"state": "idle", "message": "", "ok": None}))


def label(c: str) -> str:
    return _LABELS.get(c, c)


def status() -> dict:
    """Per-component readiness + current install progress, for the UI."""
    return {
        c: {"label": _LABELS[c], "ready": component_ready(c),
            "install": dict(_install[c])}
        for c in COMPONENTS
    }


def install(component: str) -> dict:
    """Kick off a background install of one component (idempotent while running)."""
    if component not in COMPONENTS:
        return {"ok": False, "error": "Неизвестный компонент"}
    with _lock:
        if _install[component]["state"] == "running":
            return dict(_install[component])
        _set(component, "running", "Подготовка…")
    threading.Thread(target=_do_install, args=(component,), daemon=True,
                     name=f"vtx-install-{component}").start()
    return dict(_install[component])


def _set(c: str, state: str, message: str, ok=None, percent=None) -> None:
    _install[c] = {"state": state, "message": message, "ok": ok, "percent": percent}


def _run(cmd: list[str], timeout: int, env: dict | None = None):
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                          timeout=timeout, env=env)


def _apt_install(pkgs: list[str], timeout: int = 1800) -> bool:
    """Install system packages with apt-get. Works as root (the usual case in a
    container) or with passwordless sudo; otherwise reports failure so the caller
    can print the exact command to run by hand."""
    apt = shutil.which("apt-get")
    if not apt:
        return False
    prefix: list[str] = []
    if os.geteuid() != 0:
        sudo = shutil.which("sudo")
        if not sudo:
            return False
        prefix = [sudo, "-n"]
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    try:
        _run(prefix + [apt, "update"], timeout, env)
        r = _run(prefix + [apt, "install", "-y", "--no-install-recommends"] + pkgs,
                 timeout, env)
        return r.returncode == 0
    except Exception:
        return False


def _do_install(c: str) -> None:
    try:
        if c == "playwright":
            if not _has("playwright"):
                _set(c, "running", "Устанавливаю playwright (pip)…")
                r = _run([sys.executable, "-m", "pip", "install", "playwright"], 1800)
                if r.returncode != 0:
                    _set(c, "error", "pip: " + (r.stderr or "")[-300:], ok=False)
                    return
            _set(c, "running", "Скачиваю браузер Chromium (~150 МБ, один раз)…")
            r = _run([sys.executable, "-m", "playwright", "install", "chromium"], 1800)
            if r.returncode != 0:
                _set(c, "error", "chromium: " + (r.stderr or "")[-300:], ok=False)
                return
            _set(c, "done", "Готово — автоматическая запись встреч доступна.", ok=True)

        elif c == "diarization":
            _set(c, "running", "Скачиваю torch + pyannote.audio (~2.5 ГБ, один раз)…")
            r = _run([sys.executable, "-m", "pip", "install", "torch", "pyannote.audio"], 7200)
            if r.returncode != 0:
                _set(c, "error", "pip: " + (r.stderr or "")[-300:], ok=False)
                return
            _set(c, "done", "Готово. Осталось задать токен HuggingFace для «кто говорил».", ok=True)

        elif c == "ffmpeg":
            _set(c, "running", "Устанавливаю ffmpeg (apt)…")
            _apt_install(["ffmpeg"])
            if not find_ffmpeg():
                _set(c, "error", "Не удалось установить ffmpeg автоматически (нужен root "
                     "или sudo без пароля). Выполните: sudo apt install ffmpeg", ok=False)
                return
            _set(c, "done", f"Готово — ffmpeg: {find_ffmpeg()}", ok=True)

        elif c == "ocr":
            missing = [p for p, m in (("Pillow", "PIL"), ("pytesseract", "pytesseract"),
                                      ("av", "av")) if not _has(m)]
            if missing:
                _set(c, "running", f"Устанавливаю {', '.join(missing)} (pip)…")
                r = _run([sys.executable, "-m", "pip", "install", *missing], 1800)
                if r.returncode != 0:
                    _set(c, "error", "pip: " + (r.stderr or "")[-300:], ok=False)
                    return
            if not find_tesseract():
                _set(c, "running", "Устанавливаю движок Tesseract OCR (+ русский язык)…")
                _apt_install(["tesseract-ocr", "tesseract-ocr-rus"])
            if not find_tesseract():
                _set(c, "error", "Python-пакеты поставлены, но движок Tesseract не найден "
                     "(нужен root или sudo без пароля). Выполните: "
                     "sudo apt install tesseract-ocr tesseract-ocr-rus", ok=False)
                return
            _set(c, "done", "Готово — распознавание текста/кода с экрана и имён "
                 "говорящих с видео доступно.", ok=True)

        elif c == "audio_loopback":
            # The "virtual cable" is a PulseAudio null-sink; the container makes
            # them at startup (docker/run.sh). Here we just create one if missing.
            _set(c, "running", "Проверяю виртуальное аудио (PulseAudio)…")
            if not _loopback_device_present():
                pactl = shutil.which("pactl")
                if pactl:
                    try:
                        _run([pactl, "load-module", "module-null-sink",
                              "sink_name=meet0",
                              "sink_properties=device.description=meet0"], 30)
                        _run([pactl, "set-default-sink", "meet0"], 30)
                    except Exception:
                        pass
            if _loopback_device_present():
                _set(c, "done", "Готово — виртуальное аудио настроено "
                     "(PulseAudio null-sink «meet0.monitor»).", ok=True)
            else:
                _set(c, "error", "PulseAudio-монитор не найден. Запустите контейнер с "
                     "VTX_RECORDER_ENABLED=1 — тогда null-sink создаётся при старте.",
                     ok=False)

    except subprocess.TimeoutExpired:
        _set(c, "error", "Превышено время установки. Попробуйте ещё раз.", ok=False)
    except Exception as e:  # noqa: BLE001
        _set(c, "error", f"Ошибка установки: {e}", ok=False)
