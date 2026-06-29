"""Optional local engine (Ollama): check status and install on demand.

Ollama is NOT installed automatically. The UI lets the user choose local vs
cloud; only if they pick the local engine (and it's missing) do we download and
install Ollama + the protocol model — all from here, reporting progress.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from . import config

_NOWINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Install progress, polled by the UI. `percent` is the model-download progress
# (0-100) or None when not downloading.
_install = {"state": "idle", "message": "", "ok": None, "percent": None}
_lock = threading.Lock()
_cancel = False


class _InstallCancelled(Exception):
    pass


def cancel() -> dict:
    """Request cancellation of an in-progress install."""
    global _cancel
    if _install["state"] == "running":
        _cancel = True
    return {"ok": True}


def _exe() -> str | None:
    p = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    if p.exists():
        return str(p)
    return shutil.which("ollama")


def _server_up() -> bool:
    try:
        urllib.request.urlopen(config.OLLAMA_URL.rstrip("/") + "/api/version", timeout=2)
        return True
    except Exception:
        return False


def _has_model(name: str) -> bool:
    exe = _exe()
    if not exe:
        return False
    try:
        out = subprocess.run([exe, "list"], capture_output=True, text=True,
                             timeout=20, creationflags=_NOWINDOW).stdout or ""
        return name.split(":")[0] in out
    except Exception:
        return False


def _modelfile() -> Path | None:
    mf = Path(__file__).resolve().parent.parent / "Modelfile"
    return mf if mf.exists() else None


def status() -> dict:
    exe = _exe()
    return {
        "installed": bool(exe),
        "server_up": _server_up(),
        "has_model": _has_model("vtx-protocol") or _has_model(config.OLLAMA_MODEL),
        "model": config.OLLAMA_MODEL,
        "ready": bool(exe) and (_has_model("vtx-protocol") or _has_model(config.OLLAMA_MODEL)),
        "install": dict(_install),
    }


def _set(state: str, message: str, ok=None) -> None:
    _install.update(state=state, message=message, ok=ok)


def _pull_api(name: str) -> None:
    """Pull a model via Ollama's streaming API so we get %/GB progress."""
    import json
    url = config.OLLAMA_URL.rstrip("/") + "/api/pull"
    body = json.dumps({"name": name, "stream": True}).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        for raw in resp:  # newline-delimited JSON events
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if _cancel:
                raise _InstallCancelled()
            total, completed = ev.get("total"), ev.get("completed")
            st = ev.get("status", "")
            if total and completed:
                pct = int(completed * 100 / total)
                _install["percent"] = pct
                _set("running", f"Скачивание модели qwen2.5:7b… {pct}% "
                     f"({completed / 1e9:.1f}/{total / 1e9:.1f} ГБ)")
            elif st:
                _set("running", f"Модель: {st}")
    _install["percent"] = None


def install() -> dict:
    """Kick off install in the background (idempotent while running)."""
    global _cancel
    with _lock:
        if _install["state"] == "running":
            return dict(_install)
        _cancel = False
        _install["percent"] = None
        _set("running", "Подготовка…")
    threading.Thread(target=_do_install, daemon=True, name="vtx-ollama-install").start()
    return dict(_install)


def _do_install() -> None:
    try:
        exe = _exe()
        if not exe:
            _set("running", "Скачивание и установка Ollama (~700 МБ)…")
            winget = shutil.which("winget")
            if winget:
                subprocess.run([winget, "install", "--id", "Ollama.Ollama", "--silent",
                                "--accept-package-agreements", "--accept-source-agreements"],
                               timeout=1800, creationflags=_NOWINDOW)
            else:
                # Fallback: download the official installer and run it.
                setup = Path(os.environ.get("TEMP", ".")) / "OllamaSetup.exe"
                urllib.request.urlretrieve(
                    "https://ollama.com/download/OllamaSetup.exe", str(setup))
                subprocess.run([str(setup), "/VERYSILENT", "/NORESTART"],
                               timeout=1800, creationflags=_NOWINDOW)
            exe = _exe()
            if not exe:
                _set("error", "Не удалось установить Ollama автоматически. "
                     "Установите вручную: https://ollama.com/download", ok=False)
                return

        if not _server_up():
            _set("running", "Запуск Ollama…")
            subprocess.Popen([exe, "serve"], creationflags=_NOWINDOW)
            for _ in range(30):
                if _server_up():
                    break
                time.sleep(1)

        if _cancel:
            raise _InstallCancelled()

        if not _has_model("qwen2.5:7b") and not _has_model("vtx-protocol"):
            _set("running", "Скачивание модели qwen2.5:7b (~4.7 ГБ, один раз)…")
            try:
                _pull_api("qwen2.5:7b")  # streaming progress (%/GB)
            except _InstallCancelled:
                raise
            except Exception:
                # Fall back to the CLI if the streaming API isn't reachable.
                subprocess.run([exe, "pull", "qwen2.5:7b"],
                               timeout=7200, creationflags=_NOWINDOW)

        if _cancel:
            raise _InstallCancelled()

        mf = _modelfile()
        if mf and not _has_model("vtx-protocol"):
            _set("running", "Создание модели протокола «vtx-protocol»…")
            subprocess.run([exe, "create", "vtx-protocol", "-f", str(mf)],
                           timeout=900, creationflags=_NOWINDOW)

        _set("done", "Готово — локальный движок установлен и готов.", ok=True)
    except _InstallCancelled:
        _install["percent"] = None
        _set("cancelled", "Установка отменена. Уже скачанная часть сохранена "
             "— можно продолжить позже.", ok=False)
    except Exception as e:  # noqa: BLE001
        _set("error", f"Ошибка установки: {e}", ok=False)
