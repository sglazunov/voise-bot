"""Optional local engine (Ollama): check status and prepare the model on demand.

In this deploy Ollama is a SEPARATE service reached over HTTP (`OLLAMA_URL`) —
either the `local-ai` compose profile or an Ollama on the host/another machine.
So there is nothing to "install" inside the app container: we talk to the server's
API, pull the protocol model with progress, and build `vtx-protocol` from the
Modelfile when the `ollama` CLI happens to be available locally.

If the server isn't reachable we say exactly how to bring it up instead of trying
to install a daemon into the container.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from . import config

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
    """The local `ollama` CLI, if this machine happens to have one."""
    return shutil.which("ollama")


def _api(path: str, timeout: int = 3):
    return urllib.request.urlopen(config.OLLAMA_URL.rstrip("/") + path, timeout=timeout)


def _server_up() -> bool:
    try:
        _api("/api/version")
        return True
    except Exception:
        return False


def _models() -> list[str]:
    """Model names the Ollama server has, via its HTTP API (works remotely)."""
    try:
        with _api("/api/tags", timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", [])]
    except Exception:
        return []


def _has_model(name: str) -> bool:
    short = name.split(":")[0]
    return any(m.split(":")[0] == short for m in _models())


def _modelfile() -> Path | None:
    mf = Path(__file__).resolve().parent.parent / "Modelfile"
    return mf if mf.exists() else None


def status() -> dict:
    up = _server_up()
    ready = up and (_has_model("vtx-protocol") or _has_model(config.OLLAMA_MODEL))
    return {
        "installed": up or bool(_exe()),
        "server_up": up,
        "has_model": up and (_has_model("vtx-protocol") or _has_model(config.OLLAMA_MODEL)),
        "model": config.OLLAMA_MODEL,
        "ready": ready,
        "install": dict(_install),
    }


def _set(state: str, message: str, ok=None) -> None:
    _install.update(state=state, message=message, ok=ok)


def _pull_api(name: str) -> None:
    """Pull a model via Ollama's streaming API so we get %/GB progress."""
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
                _set("running", f"Скачивание модели {name}… {pct}% "
                     f"({completed / 1e9:.1f}/{total / 1e9:.1f} ГБ)")
            elif st:
                _set("running", f"Модель: {st}")
    _install["percent"] = None


def install() -> dict:
    """Kick off model preparation in the background (idempotent while running)."""
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
        if not _server_up() and exe:
            _set("running", "Запускаю локальный Ollama…")
            subprocess.Popen([exe, "serve"])
            for _ in range(30):
                if _server_up():
                    break
                time.sleep(1)

        if not _server_up():
            _set("error",
                 f"Сервер Ollama недоступен по {config.OLLAMA_URL}. Поднимите его: "
                 "`docker compose --profile local-ai up -d` — или укажите адрес "
                 "работающего Ollama в переменной OLLAMA_URL.", ok=False)
            return

        if _cancel:
            raise _InstallCancelled()

        model = config.OLLAMA_MODEL
        if not _has_model(model) and not _has_model("vtx-protocol"):
            _set("running", f"Скачивание модели {model} (~4.7 ГБ, один раз)…")
            _pull_api(model)

        if _cancel:
            raise _InstallCancelled()

        # The tuned protocol model is built from the Modelfile — needs the CLI,
        # which only exists when Ollama runs on this machine. Optional.
        mf = _modelfile()
        if exe and mf and not _has_model("vtx-protocol"):
            _set("running", "Создание модели протокола «vtx-protocol»…")
            subprocess.run([exe, "create", "vtx-protocol", "-f", str(mf)], timeout=900)

        _set("done", "Готово — локальный движок готов к работе.", ok=True)
    except _InstallCancelled:
        _install["percent"] = None
        _set("cancelled", "Установка отменена. Уже скачанная часть сохранена "
             "— можно продолжить позже.", ok=False)
    except Exception as e:  # noqa: BLE001
        _set("error", f"Ошибка подготовки: {e}", ok=False)
