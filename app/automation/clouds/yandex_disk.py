"""Yandex Disk uploader via the REST API (cloud-api.yandex.net).

Auth: an OAuth token (header `Authorization: OAuth <token>`). Get one at
https://yandex.ru/dev/disk/poligon/ or via your own OAuth app with scope
`cloud_api:disk.write`. Flow: ensure folder → ask for an upload href →
PUT the file → publish to get a shareable link.
"""
from __future__ import annotations

from pathlib import Path

from ._http import CloudError, request, request_json

API = "https://cloud-api.yandex.net/v1/disk"


def _auth(cfg: dict) -> dict:
    token = (cfg.get("token") or "").strip()
    if not token:
        raise CloudError("Не задан OAuth-токен Яндекс.Диска.")
    return {"Authorization": f"OAuth {token}"}


def _read_auth(cfg: dict) -> dict:
    """Header for READ calls (getting the public link). Uses a separate read
    token if the user provided one (their main token may be write-only), else
    falls back to the main token. Must be the SAME Yandex account."""
    rt = (cfg.get("read_token") or "").strip()
    return {"Authorization": f"OAuth {rt}"} if rt else _auth(cfg)


def readiness(cfg: dict) -> dict:
    token = (cfg.get("token") or "").strip()
    if not token:
        return {"ready": False, "detail": "Нужен OAuth-токен Яндекс.Диска."}
    return {"ready": True, "detail": f"Папка: {cfg.get('folder') or 'disk:/'}"}


def _ensure_folder(folder: str, headers: dict) -> None:
    """Create the folder (and parents) if missing. 409 = already exists."""
    parts, acc = folder.replace("disk:/", "").strip("/").split("/"), "disk:/"
    for p in parts:
        if not p:
            continue
        acc = f"{acc.rstrip('/')}/{p}"
        status, _ = request("PUT", f"{API}/resources",
                            headers=headers, params={"path": acc})
        if status not in (201, 409):
            # Non-fatal for intermediate dirs, but surface a hard failure.
            if status in (401, 403):
                raise CloudError(
                    f"Яндекс.Диск отклонил токен ({status}). Нужен scope "
                    "cloud_api:disk.write.")


def upload(file_path: str, name: str, cfg: dict) -> dict:
    src = Path(file_path)
    if not src.exists():
        return {"ok": False, "backend": "yandex_disk", "error": "Файл не найден."}
    try:
        headers = _auth(cfg)
        folder = (cfg.get("folder") or "disk:/Телемост-записи").rstrip("/")
        _ensure_folder(folder, headers)
        remote = f"{folder}/{name}"

        status, info = request_json(
            "GET", f"{API}/resources/upload",
            headers=headers, params={"path": remote, "overwrite": "true"})
        if status == 401:
            raise CloudError("Яндекс.Диск: неверный или просроченный токен (401).")
        href = (info or {}).get("href")
        if not href:
            raise CloudError(f"Яндекс.Диск не дал ссылку на загрузку: {info}")

        # Big recordings take a while — stream from disk (an hour of video is
        # hundreds of MB; read_bytes() would hold it all in RAM) with a
        # generous timeout.
        with src.open("rb") as f:
            put_status, _ = request(
                "PUT", href, data=f,
                headers={"Content-Length": str(src.stat().st_size)},
                timeout=3600)
        if put_status not in (201, 202):
            raise CloudError(f"Загрузка не удалась (HTTP {put_status}).")

        # The file is uploaded. Publish it and fetch the PUBLIC share link
        # (https://disk.yandex.ru/d/…). Reading the link back needs the read
        # scope; with a write-only token this 403s — surface a clear note.
        url, note = None, None
        try:
            pub_status, _ = request("PUT", f"{API}/resources/publish",
                                    headers=headers, params={"path": remote})
            if pub_status in (200, 201):
                meta_status, meta = request_json(
                    "GET", f"{API}/resources", headers=_read_auth(cfg),
                    params={"path": remote, "fields": "public_url"})
                if meta_status == 200:
                    url = (meta or {}).get("public_url")
                elif meta_status in (401, 403):
                    note = ("Файл загружен, но публичную ссылку не получить: нужен доступ "
                            "на ЧТЕНИЕ (cloud_api:disk.read). Впишите отдельный «токен "
                            "чтения» Я.Диска в настройках, либо добавьте «Чтение всего "
                            "Диска» в приложении Яндекса и получите токен заново.")
        except CloudError as e:
            note = f"Публичная ссылка не получена: {e}"
        # `url` is the real share link (or None). Never return the internal
        # disk:/ path as a link — it isn't openable.
        return {"ok": True, "backend": "yandex_disk",
                "url": url, "path": remote, "public_note": note}
    except CloudError as e:
        return {"ok": False, "backend": "yandex_disk", "error": str(e)}
