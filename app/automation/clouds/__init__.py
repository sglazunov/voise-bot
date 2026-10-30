"""Cloud uploaders for finished meeting recordings.

One simple interface: pick a backend by the `cloud` setting and `upload()` a
file, getting back {ok, url, path, backend} (or {ok: False, error}). Backends:
local disk, Yandex Disk, Google Drive. Credentials come from app.automation
.settings (the per-backend sub-dict).
"""
from __future__ import annotations

from pathlib import Path

from . import gdrive, local, yandex_disk

# backend key -> (module, settings sub-key, human label)
BACKENDS = {
    "local": (local, "local_dir", "Локально на диск"),
    "yandex_disk": (yandex_disk, "yandex_disk", "Яндекс Диск"),
    "gdrive": (gdrive, "gdrive", "Google Drive"),
}


def _backend_cfg(settings: dict, key: str, owner: str | None = None) -> dict:
    """The settings sub-dict a backend expects. `owner` — команда: локальному
    диску нужна своя подпапка в общей папке по умолчанию (local.base_dir)."""
    if key == "local":
        # Без явного владельца — команда, чьи это настройки (`settings.load`
        # ставит `_team`): иначе статус показывал общую папку сервера.
        return {"local_dir": settings.get("local_dir"),
                "team": owner or settings.get("_team") or ""}
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
           backend: str | None = None, folder: str | None = None,
           subfolder: str | None = None, owner: str | None = None,
           overwrite: bool = True) -> dict:
    """Upload `file_path` as `name` to the chosen (or selected) cloud.

    `owner` — команда-владелец файла. Удалённым облакам он не нужен (у каждой
    команды свой токен), а локальному — обязателен: без него файл ложится в
    общую на весь сервер папку (ревью 30.10, коллизия имён между командами).

    `folder` overrides the destination folder for this one upload (e.g. put
    protocols in a different folder than the recordings). `subfolder` — папка
    встречи внутри неё (docs/ТЗ-ПАПКИ-ВСТРЕЧ.md): имя серии или «Разовые
    встречи»; создаётся при первой заливке.

    `overwrite=False` — записи встреч: одноимённый ДРУГОЙ файл в облаке не
    затирается, новый ложится рядом (второй раунд ревью 30.10: вторая часть
    встречи затирала первую вместе с её ссылкой в Weeek)."""
    key = backend or settings.get("cloud") or "local"
    entry = BACKENDS.get(key)
    if not entry:
        return {"ok": False, "backend": key, "error": f"Неизвестное облако: {key}"}
    mod = entry[0]
    bcfg = _backend_cfg(settings, key, owner)
    extra = {} if overwrite else {"overwrite": False}
    if folder:
        if key == "local":
            # Папка протоколов задана в форме Яндекс.Диска («disk:/…») — для
            # локального бэкенда это не путь: каталог получался относительным,
            # а as_uri() на относительном пути падает ValueError. В умолчаниях
            # стоит именно такое значение, поэтому доставка протокола ломалась
            # в конфигурации ПО УМОЛЧАНИЮ. Чужую форму игнорируем и кладём
            # протоколы в подпапку рядом с записями.
            # Абсолютная папка — только своя (local.folder_allowed, ревью 30.10,
            # второй раунд): старое значение, ведущее в чужую команду, при
            # чтении заменяется подпапкой рядом с записями.
            base = local.base_dir(bcfg)
            if Path(folder).is_absolute() and local.folder_allowed(
                    Path(folder), base, bcfg.get("team") or ""):
                bcfg["local_dir"] = folder
            else:
                bcfg["local_dir"] = str(base / "protocols")
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
    if subfolder:
        if key == "local":
            bcfg["local_dir"] = str(local.base_dir(bcfg) / subfolder)
        elif key == "gdrive":
            try:
                bcfg["folder_id"] = gdrive.child_folder(
                    bcfg, bcfg.get("folder_id") or None, subfolder)
            except Exception as e:  # noqa: BLE001 — без подпапки, но с файлом
                res = mod.upload(file_path, name, bcfg, **extra)
                res["sub_failed"] = True       # файл не в подпапке — не переезжает
                if res.get("ok"):
                    res["public_note"] = (f"Папку «{subfolder}» в Google Drive создать "
                                          f"не удалось ({e}) — файл лёг в общую.")
                return res
        else:  # yandex_disk
            bcfg["folder"] = f"{yandex_disk.meetings_root(bcfg)}/{subfolder}"
    return mod.upload(file_path, name, bcfg, **extra)


def move_to_sibling(item: dict, subfolder: str, settings: dict) -> dict:
    """Переложить уже выгруженный файл из его папки (обычно «Разовые встречи»)
    в соседнюю `subfolder` — папку серии. item: {path, backend}."""
    key = item.get("backend") or settings.get("cloud") or "local"
    path = str(item.get("path") or "")
    if not path:
        return {"ok": False, "retry": False, "error": "нет пути к файлу"}
    if key == "yandex_disk":
        parent, _, fname = path.rpartition("/")
        root = parent.rpartition("/")[0]
        if not root or not fname:
            return {"ok": False, "retry": False, "error": f"непонятный путь {path}"}
        return yandex_disk.move(path, f"{root}/{subfolder}/{fname}",
                                _backend_cfg(settings, key))
    if key == "gdrive":
        return gdrive.move_to_sibling(path, subfolder, _backend_cfg(settings, key))
    if key == "local":
        import os
        src = Path(path)
        if not src.exists():
            return {"ok": False, "retry": False, "error": "файла уже нет"}
        dst = src.parent.parent / subfolder / src.name
        if dst.exists():
            return {"ok": False, "retry": False,
                    "error": "в папке серии уже есть файл с таким именем"}
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        return {"ok": True, "backend": "local", "path": str(dst), "url": dst.as_uri()}
    return {"ok": False, "retry": False, "error": f"неизвестное облако {key}"}
