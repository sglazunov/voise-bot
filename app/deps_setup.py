"""On-demand install of optional dependencies — INTO THIS app's venv.

The whole point: pip installs run via ``sys.executable -m pip``, so packages
land in the SAME interpreter the app runs from (the project ``.venv``), not some
global Python. This is the fix for the classic "I ran pip install but the app
still says it's missing" — that happens when pip installs into a different
Python than the one the app uses.

Mirrors ``ollama_setup.py``: ``status()`` / ``install(component)`` /
``cancel(component)``, with progress polled by the UI. Components:

  playwright   -> pip install playwright  +  `playwright install chromium`
  diarization  -> pip install torch pyannote.audio
  ffmpeg       -> winget install Gyan.FFmpeg (a binary; its path is saved to
                  the automation settings so the running server finds it).
"""
from __future__ import annotations

import glob
import importlib.util
import os
import shutil
import subprocess
import sys
import threading
import time

_NOWINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

COMPONENTS = ("playwright", "diarization", "ffmpeg", "ocr", "audio_loopback")
_LABELS = {
    "playwright": "Запись встреч (Playwright + Chromium)",
    "diarization": "«Кто говорил» (torch + pyannote.audio)",
    "ffmpeg": "ffmpeg (запись и конвертация видео/аудио)",
    "ocr": "Текст с экрана (Pillow + pytesseract + Tesseract)",
    "audio_loopback": "Виртуальное аудио для записи звука (VB-CABLE)",
}

# Audio devices that let ffmpeg capture the meeting's sound (system loopback).
_LOOPBACK_KEYS = ("cable", "voicemeeter", "stereo mix", "стерео микшер",
                  "loopback", "what u hear", "what you hear")

_install = {c: {"state": "idle", "message": "", "ok": None} for c in COMPONENTS}
_lock = threading.Lock()


def _has(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


def _chromium_installed() -> bool:
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright")
    return bool(glob.glob(os.path.join(base, "chromium-*")))


def find_ffmpeg() -> str | None:
    """Locate ffmpeg: PATH first, then a winget/Gyan install location."""
    p = shutil.which("ffmpeg")
    if p:
        return p
    patterns = [
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\**\bin\ffmpeg.exe"),
        os.path.expandvars(r"%ProgramFiles%\ffmpeg\bin\ffmpeg.exe"),
    ]
    for pat in patterns:
        hits = glob.glob(pat, recursive=True)
        if hits:
            return hits[0]
    return None


def find_tesseract() -> str | None:
    """Locate the Tesseract OCR binary: PATH, then common install locations."""
    p = shutil.which("tesseract")
    if p:
        return p
    patterns = [
        os.path.expandvars(r"%ProgramFiles%\Tesseract-OCR\tesseract.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\tesseract.exe"),
    ]
    for pat in patterns:
        hits = glob.glob(pat)
        if hits:
            return hits[0]
    return None


def component_ready(c: str) -> bool:
    if c == "playwright":
        return _has("playwright") and _chromium_installed()
    if c == "diarization":
        return _has("torch") and _has("pyannote.audio")
    if c == "ffmpeg":
        return find_ffmpeg() is not None
    if c == "ocr":
        return _has("PIL") and _has("pytesseract") and find_tesseract() is not None
    if c == "audio_loopback":
        return _loopback_device_present()
    return False


def _loopback_device_present() -> bool:
    """True if ffmpeg can see a loopback/virtual audio device to capture sound."""
    try:
        from .automation.recorder import capture
        from .automation import settings as auto_settings
        ff = auto_settings.load().get("ffmpeg_path") or "ffmpeg"
        names = capture.list_audio_devices(ff)
        return any(any(k in n.lower() for k in _LOOPBACK_KEYS) for n in names)
    except Exception:
        return False


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


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                          timeout=timeout, creationflags=_NOWINDOW)


def _do_install(c: str) -> None:
    try:
        if c == "playwright":
            _set(c, "running", "Устанавливаю playwright в .venv (pip)…")
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
            _set(c, "running", "Скачиваю torch + pyannote.audio в .venv (~2.5 ГБ, один раз)…")
            r = _run([sys.executable, "-m", "pip", "install", "torch", "pyannote.audio"], 7200)
            if r.returncode != 0:
                _set(c, "error", "pip: " + (r.stderr or "")[-300:], ok=False)
                return
            _set(c, "done", "Готово. Осталось задать токен HuggingFace для «кто говорил».", ok=True)

        elif c == "ffmpeg":
            winget = shutil.which("winget")
            if not winget:
                _set(c, "error", "winget не найден. Скачайте ffmpeg с ffmpeg.org "
                     "и укажите путь в поле ниже.", ok=False)
                return
            _set(c, "running", "Устанавливаю ffmpeg (winget)…")
            _run([winget, "install", "--id", "Gyan.FFmpeg", "-e", "--silent",
                  "--accept-package-agreements", "--accept-source-agreements"], 1800)
            path = find_ffmpeg()
            if not path:
                _set(c, "error", "Не удалось установить ffmpeg автоматически. "
                     "Скачайте с ffmpeg.org и укажите путь в поле ниже.", ok=False)
                return
            # Save the path so the running server uses it without a PATH refresh.
            try:
                from .automation import settings as auto_settings
                cfg = auto_settings.load()
                cfg["ffmpeg_path"] = path
                auto_settings.save(cfg)
            except Exception:
                pass
            _set(c, "done", f"Готово — ffmpeg установлен: {path}", ok=True)

        elif c == "ocr":
            _set(c, "running", "Устанавливаю Pillow + pytesseract в .venv (pip)…")
            r = _run([sys.executable, "-m", "pip", "install", "Pillow", "pytesseract"], 1800)
            if r.returncode != 0:
                _set(c, "error", "pip: " + (r.stderr or "")[-300:], ok=False)
                return
            if not find_tesseract():
                winget = shutil.which("winget")
                if winget:
                    _set(c, "running", "Устанавливаю движок Tesseract OCR (winget)…")
                    _run([winget, "install", "--id", "UB-Mannheim.TesseractOCR", "-e",
                          "--silent", "--accept-package-agreements",
                          "--accept-source-agreements"], 1800)
            if not find_tesseract():
                _set(c, "error", "Pillow/pytesseract поставлены, но движок Tesseract не найден. "
                     "Установите Tesseract OCR (github.com/UB-Mannheim/tesseract) с русским языком.",
                     ok=False)
                return
            _set(c, "done", "Готово — распознавание текста с экрана доступно "
                 "(для русского нужен языковой пакет rus в Tesseract).", ok=True)

        elif c == "audio_loopback":
            import tempfile
            import urllib.request
            import zipfile
            _set(c, "running", "Скачивание VB-CABLE с vb-audio.com…", percent=0)
            tmp = tempfile.mkdtemp(prefix="vbcable_")
            zip_path = os.path.join(tmp, "vbcable.zip")

            def _hook(block, bsize, total):
                if total and total > 0:
                    pct = min(int(block * bsize * 100 / total), 100)
                    _set(c, "running", f"Скачивание VB-CABLE… {pct}%", percent=pct)

            urllib.request.urlretrieve(
                "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack43.zip",
                zip_path, reporthook=_hook)
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(tmp)
            setup = os.path.join(tmp, "VBCABLE_Setup_x64.exe")
            if not os.path.exists(setup):
                _set(c, "error", "Не нашёл установщик VB-CABLE в архиве. "
                     "Установите вручную с vb-audio.com/Cable.", ok=False)
                return
            _set(c, "running", "Запускаю установщик драйвера ОТ ИМЕНИ АДМИНИСТРАТОРА — "
                 "подтвердите запрос Windows (UAC), затем нажмите Install в окне VB-CABLE…")
            # The VB-CABLE driver writes to the registry → needs admin. ShellExecute
            # with the "runas" verb triggers the UAC elevation prompt.
            elevated = False
            try:
                import ctypes
                rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", setup, "-i",
                                                         os.path.dirname(setup), 1)
                elevated = int(rc) > 32  # >32 = launched (user accepted UAC)
            except Exception:
                elevated = False
            if not elevated:
                _set(c, "error", "Нужны права администратора (вы отклонили запрос UAC?). "
                     "Откройте папку " + tmp + " и запустите VBCABLE_Setup_x64.exe правой "
                     "кнопкой → «Запуск от имени администратора» → Install.", ok=False)
                return
            # Wait a bit for the driver to register (or a reboot to be needed).
            for _ in range(40):
                if _loopback_device_present():
                    break
                time.sleep(1)
            if _loopback_device_present():
                _set(c, "done", "Готово — VB-CABLE установлен. Дальше: сделайте «CABLE Input» "
                     "устройством вывода по умолчанию (Windows → Звук), и выберите "
                     "«CABLE Output» в списке аудио-устройств здесь.", ok=True)
            else:
                _set(c, "done", "Установщик VB-CABLE запущен с правами админа. Если устройство "
                     "не появилось — закончите установку в его окне (Install) и, скорее всего, "
                     "ПЕРЕЗАГРУЗИТЕ компьютер. После: «CABLE Input» — устройство вывода по "
                     "умолчанию, «CABLE Output» — выберите здесь.", ok=True)
    except subprocess.TimeoutExpired:
        _set(c, "error", "Превышено время установки. Попробуйте ещё раз.", ok=False)
    except Exception as e:  # noqa: BLE001
        _set(c, "error", f"Ошибка установки: {e}", ok=False)
