"""Local-disk 'upload' — just copy the recording into a configured folder.

Useful as the default and as a fallback when no cloud is connected."""
from __future__ import annotations

import shutil
from pathlib import Path

from ... import config


def _target_dir(cfg: dict) -> Path:
    raw = (cfg.get("local_dir") or "").strip()
    base = Path(raw) if raw else (config.DATA_DIR / "recordings")
    base.mkdir(parents=True, exist_ok=True)
    return base


def readiness(cfg: dict) -> dict:
    try:
        d = _target_dir(cfg)
        return {"ready": True, "detail": f"Папка: {d}"}
    except OSError as e:
        return {"ready": False, "detail": f"Нет доступа к папке: {e}"}


def upload(file_path: str, name: str, cfg: dict) -> dict:
    src = Path(file_path)
    if not src.exists():
        return {"ok": False, "backend": "local", "error": "Файл не найден."}
    dest = _target_dir(cfg) / name
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    return {"ok": True, "backend": "local", "url": dest.as_uri(),
            "path": str(dest)}
