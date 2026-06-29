"""Check and pre-download Whisper recognition models on demand.

The model selector lets a job pick accuracy (small/medium/large-v3/…). Until
now a not-yet-downloaded model was fetched only on the first "Распознать" — so
the user had to attach a file and wait. This module lets the UI download a model
on its own, with %/GB progress, before any transcription.

Downloading uses faster-whisper's own helper (``download_model``) which fetches
the model files into the HuggingFace cache WITHOUT loading them into RAM.
"""
from __future__ import annotations

import os
import threading
import time

from . import config

# Approximate on-disk size (MB) of each CT2 int8 model — used for % progress.
_TOTAL_MB = {
    "tiny": 75, "base": 145, "small": 484, "medium": 1530,
    "large-v3": 3090, "large-v3-turbo": 1620,
}

_dl: dict[str, dict] = {}   # model name -> {state, message, ok, percent}
_lock = threading.Lock()


def _eff(name: str) -> str:
    return (name or config.MODEL or "small").strip()


def is_downloaded(name: str) -> bool:
    """True if the model's files are already in the local cache."""
    try:
        from faster_whisper.utils import download_model
        download_model(_eff(name), local_files_only=True)
        return True
    except Exception:
        return False


def status(name: str) -> dict:
    eff = _eff(name)
    return {
        "name": eff,
        "downloaded": is_downloaded(name),
        "size_hint": f"~{_TOTAL_MB.get(eff, 0) / 1000:.1f} ГБ" if _TOTAL_MB.get(eff) else "",
        "download": dict(_dl.get(eff, {"state": "idle", "message": "", "ok": None})),
    }


def download(name: str) -> dict:
    """Kick off a background download of one model (idempotent while running)."""
    eff = _eff(name)
    with _lock:
        cur = _dl.get(eff, {})
        if cur.get("state") == "running":
            return dict(cur)
        _dl[eff] = {"state": "running", "message": "Подготовка…", "ok": None, "percent": 0}
    threading.Thread(target=_do_download, args=(eff,), daemon=True,
                     name=f"vtx-model-{eff}").start()
    return dict(_dl[eff])


def _repo_cache_dir(eff: str) -> str:
    try:
        from faster_whisper.utils import _MODELS
        repo = _MODELS.get(eff) or f"Systran/faster-whisper-{eff}"
    except Exception:
        repo = f"Systran/faster-whisper-{eff}"
    home = os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
    hub = os.environ.get("HF_HUB_CACHE") or os.path.join(home, "hub")
    return os.path.join(hub, "models--" + repo.replace("/", "--"))


def _dir_mb(path: str) -> float:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / 1e6


def _do_download(eff: str) -> None:
    total_mb = _TOTAL_MB.get(eff, 0)
    repo_dir = _repo_cache_dir(eff)
    err: dict = {}

    def _run():
        try:
            from faster_whisper.utils import download_model
            download_model(eff)
        except Exception as e:  # noqa: BLE001
            err["e"] = e

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    while th.is_alive():
        mb = _dir_mb(repo_dir)
        if total_mb:
            pct = min(int(mb * 100 / total_mb), 99)
            _dl[eff] = {"state": "running", "percent": pct, "ok": None,
                        "message": f"Скачивание модели {eff}… {pct}% "
                                   f"({mb:.0f}/{total_mb} МБ)"}
        else:
            _dl[eff] = {"state": "running", "percent": None, "ok": None,
                        "message": f"Скачивание модели {eff}… ({mb:.0f} МБ)"}
        time.sleep(1)

    if err:
        _dl[eff] = {"state": "error", "percent": None, "ok": False,
                    "message": f"Не удалось скачать {eff}: {err['e']}"}
    else:
        _dl[eff] = {"state": "done", "percent": 100, "ok": True,
                    "message": f"Модель {eff} скачана и готова."}
