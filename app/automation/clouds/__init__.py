"""Cloud uploaders for finished meeting recordings.

One simple interface: pick a backend by the `cloud` setting and `upload()` a
file, getting back {ok, url, path, backend} (or {ok: False, error}). Backends:
local disk, Yandex Disk, Google Drive. Credentials come from app.automation
.settings (the per-backend sub-dict).
"""
from __future__ import annotations

from pathlib import Path

from ... import config

from . import gdrive, local, yandex_disk

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


def publish(remote_path: str, settings: dict, backend: str | None = None) -> dict:
    """Получить публичную ссылку на УЖЕ загруженный файл (повторная публикация).
    Умеет только Яндекс.Диск: у Google ссылка приходит вместе с загрузкой, у
    локального диска ссылки нет вовсе."""
    key = backend or settings.get("cloud") or "local"
    entry = BACKENDS.get(key)
    mod = entry[0] if entry else None
    fn = getattr(mod, "publish", None)
    if not fn or not remote_path:
        return {"ok": False, "backend": key,
                "error": "у этого облака нет повторной публикации"}
    return fn(remote_path, _backend_cfg(settings, key))


def upload(file_path: str, name: str, settings: dict,
           backend: str | None = None, folder: str | None = None) -> dict:
    """Upload `file_path` as `name` to the chosen (or selected) cloud.

    `folder` overrides the destination folder for this one upload (e.g. put
    protocols in a different folder than the recordings)."""
    key = backend or settings.get("cloud") or "local"
    entry = BACKENDS.get(key)
    if not entry:
        return {"ok": False, "backend": key, "error": f"Неизвестное облако: {key}"}
    mod = entry[0]
    bcfg = _backend_cfg(settings, key)
    if folder:
        if key == "local":
            # Папка протоколов задана в форме Яндекс.Диска («disk:/…») — для
            # локального бэкенда это не путь: каталог получался относительным,
            # а as_uri() на относительном пути падает ValueError. В умолчаниях
            # стоит именно такое значение, поэтому доставка протокола ломалась
            # в конфигурации ПО УМОЛЧАНИЮ. Чужую форму игнорируем и кладём
            # протоколы в подпапку рядом с записями.
            if Path(folder).is_absolute():
                bcfg["local_dir"] = folder
            else:
                bcfg["local_dir"] = str(Path(
                    (settings.get("local_dir") or "").strip()
                    or (config.DATA_DIR / "recordings")) / "protocols")
        elif key == "gdrive":
            # Та же беда, что и с локальным диском: папка протоколов может быть
            # записана в форме Яндекс.Диска («disk:/…») — так стоит В
            # УМОЛЧАНИЯХ. Для Google это не идентификатор папки, запрос ушёл бы
            # с мусорным parents и вернул 404. Идентификатор — непрозрачный
            # токен без слэшей и двоеточий; всё остальное игнорируем, тогда
            # протокол ляжет туда же, куда запись.
            if "/" not in folder and ":" not in folder:
                bcfg["folder_id"] = folder
        else:  # yandex_disk
            bcfg["folder"] = folder
    return mod.upload(file_path, name, bcfg)
