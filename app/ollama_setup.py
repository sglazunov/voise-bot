"""Необязательный локальный движок (Ollama): только проверка состояния.

В этом развёртывании Ollama — отдельная служба по HTTP (`OLLAMA_URL`): либо
профиль compose `local-ai`, либо Ollama на хосте или другой машине. Внутри
контейнера приложения ставить нечего, поэтому здесь осталась одна проверка:
поднят ли сервер и есть ли на нём нужная модель. Скачивание модели делается на
стороне самой Ollama (`ollama pull`), а не из веб-интерфейса — раньше здесь была
кнопка, которой не существовало ни в одном экране.
"""
from __future__ import annotations

import json
import shutil
import urllib.request

from . import config


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


def status() -> dict:
    up = _server_up()
    ready = up and (_has_model("vtx-protocol") or _has_model(config.OLLAMA_MODEL))
    return {
        "installed": up or bool(_exe()),
        "server_up": up,
        "has_model": up and (_has_model("vtx-protocol") or _has_model(config.OLLAMA_MODEL)),
        "model": config.OLLAMA_MODEL,
        "ready": ready,
    }
