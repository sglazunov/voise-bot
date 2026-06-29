"""Cloud uploaders for finished meeting recordings.

One simple interface: pick a backend by the `cloud` setting and `upload()` a
file, getting back {ok, url, path, backend} (or {ok: False, error}). Backends:
local disk, Yandex Disk, Google Drive. Credentials come from app.automation
.settings (the per-backend sub-dict).
"""
from __future__ import annotations

from . import gdrive, local, yandex_disk
from ._http import CloudError

# backend key -> (module, settings sub-key, human label)
BACKENDS = {
    "local": (local, "local_dir", "Локально на диск"),
    "yandex_disk": (yandex_disk, "yandex_disk", "Яндекс Диск"),
    "gdrive": (gdrive, "gdrive", "Google Drive"),
}


def _backend_cfg(settings: dict, key: str) -> dict:
    """The settings sub-dict a backend expects."""
    if key == "local":
        return {"local_dir": settings.get("local_dir")}
    sub = settings.get(key)
    return dict(sub) if isinstance(sub, dict) else {}


def readiness(settings: dict) -> dict:
    """Per-backend readiness + which one is currently selected."""
    selected = settings.get("cloud") or "local"
    out = {"selected": selected, "backends": {}}
    for key, (mod, _subkey, label) in BACKENDS.items():
        try:
            r = mod.readiness(_backend_cfg(settings, key))
        except Exception as e:  # never let a backend crash the status call
            r = {"ready": False, "detail": str(e)}
        r["label"] = label
        out["backends"][key] = r
    return out


def upload(file_path: str, name: str, settings: dict,
           backend: str | None = None) -> dict:
    """Upload `file_path` as `name` to the chosen (or selected) cloud."""
    key = backend or settings.get("cloud") or "local"
    entry = BACKENDS.get(key)
    if not entry:
        return {"ok": False, "backend": key, "error": f"Неизвестное облако: {key}"}
    mod = entry[0]
    return mod.upload(file_path, name, _backend_cfg(settings, key))
