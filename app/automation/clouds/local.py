"""Local-disk 'upload' — just copy the recording into a configured folder.

Useful as the default and as a fallback when no cloud is connected."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from ... import config, security

log = logging.getLogger("vtx.clouds")


def _norm(p: Path) -> Path:
    try:
        return p.expanduser().resolve()
    except OSError:
        return p.expanduser().absolute()


def _inside(p: Path, root: Path) -> bool:
    return p == root or root in p.parents


def _founder_team(team: str) -> bool:
    return any(security.is_super_admin(u) for u in security.team_members(team))


def foreign_dir(path: Path, team: str) -> bool:
    """Путь — общая папка записей сервера, папка ДРУГОЙ команды в ней или
    каталог другой команды в `users` (так же, как `browser._foreign_profile`)."""
    p = _norm(path)
    rec = _norm(config.DATA_DIR / "recordings")
    users = _norm(config.DATA_DIR / "users")
    if _inside(p, rec) and not _inside(p, _norm(rec / security.normalize_username(team))):
        return True
    return _inside(p, users) and not _inside(p, _norm(security.user_dir(team)))


def base_dir(cfg: dict) -> Path:
    """Папка «локального облака» команды.

    Своя папка (`local_dir`) — как задана. Папка по умолчанию — ОБЩАЯ на
    сервер, поэтому в ней у каждой команды своя подпапка (`team`): имя файла
    записи — «дата, время. - название», и две команды с одинаковыми
    названием и временем встречи перезаписывали файл друг друга (ревью 30.10).

    `local_dir` клиентской команды, ведущий в общую папку записей или в
    каталог другой команды, пропускается (ревью 30.10, второй раунд): такое
    значение осталось с тех пор, когда ключ меняла сама команда, а теперь он
    серверный — команда его даже очистить не может. Миграция при чтении, без
    записи в настройки (значение видно основателю, он его и очищает)."""
    raw = (cfg.get("local_dir") or "").strip()
    team = (cfg.get("team") or "").strip()
    if raw and team and not _founder_team(team) and foreign_dir(Path(raw), team):
        log.warning("Команда %s: local_dir=%s ведёт в общую папку записей или в "
                    "каталог другой команды — пишу в свою папку", team, raw)
        raw = ""
    if raw:
        return Path(raw)
    base = config.DATA_DIR / "recordings"
    return base / security.normalize_username(team) if team else base


def folder_allowed(folder: Path, base: Path, team: str) -> bool:
    """Абсолютная папка протоколов у локального облака: команде основателя —
    любая, клиентской — только внутри своей папки записей `base`. Иначе
    «/srv/…/alice» из `protocol_folder` писала бы протоколы в чужую команду."""
    if not team or _founder_team(team):
        return True
    return _inside(_norm(folder), _norm(base))


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


def upload(file_path: str, name: str, cfg: dict) -> dict:
    src = Path(file_path)
    if not src.exists():
        return {"ok": False, "backend": "local", "error": "Файл не найден."}
    dest = _target_dir(cfg) / name
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    return {"ok": True, "backend": "local", "url": dest.as_uri(),
            "path": str(dest)}
