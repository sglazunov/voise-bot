"""Local-disk 'upload' — just copy the recording into a configured folder.

Useful as the default and as a fallback when no cloud is connected."""
from __future__ import annotations

import filecmp
import shutil
from pathlib import Path

from ... import config, security


def base_dir(cfg: dict) -> Path:
    """Папка «локального облака» команды.

    Своя папка (`local_dir`) — как задана. Папка по умолчанию — ОБЩАЯ на
    сервер, поэтому в ней у каждой команды своя подпапка (`team`): имя файла
    записи — «дата, время. - название», и две команды с одинаковыми
    названием и временем встречи перезаписывали файл друг друга (ревью 30.10)."""
    raw = (cfg.get("local_dir") or "").strip()
    if raw:
        return Path(raw)
    base = config.DATA_DIR / "recordings"
    team = (cfg.get("team") or "").strip()
    return base / security.normalize_username(team) if team else base


def _target_dir(cfg: dict) -> Path:
    base = base_dir(cfg)
    base.mkdir(parents=True, exist_ok=True)
    return base


def readiness(cfg: dict) -> dict:
    try:
        d = _target_dir(cfg)
        return {"ready": True, "detail": f"Папка: {d}"}
    except OSError as e:
        return {"ready": False, "detail": f"Нет доступа к папке: {e}"}


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size and filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False


def _free_dest(src: Path, dest: Path) -> Path:
    """Куда положить `src`, не затирая ДРУГОЙ файл: то же имя, если там пусто
    или лежит этот же файл (прошлая попытка), иначе «имя (2)…»."""
    stem, suffix = dest.stem, dest.suffix
    for n in range(1, 100):
        cand = dest if n == 1 else dest.with_name(f"{stem} ({n}){suffix}")
        if not cand.exists() or _same_file(src, cand):
            return cand
    raise OSError(f"в папке слишком много файлов с именем {dest.name}")


def upload(file_path: str, name: str, cfg: dict, overwrite: bool = True) -> dict:
    """`overwrite=False` — запись встречи: одноимённый ДРУГОЙ файл не
    затирается (второй раунд ревью 30.10 — вторая часть встречи затирала
    первую), файл ложится рядом под «имя (2)…»."""
    src = Path(file_path)
    if not src.exists():
        return {"ok": False, "backend": "local", "error": "Файл не найден."}
    dest = _target_dir(cfg) / name
    if src.resolve() != dest.resolve():
        if not overwrite:
            dest = _free_dest(src, dest)
        if overwrite or not dest.exists():
            shutil.copy2(src, dest)
    return {"ok": True, "backend": "local", "url": dest.as_uri(),
            "path": str(dest)}
