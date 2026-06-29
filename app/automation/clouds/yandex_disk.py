"""Yandex Disk uploader via the REST API (cloud-api.yandex.net).

Auth: an OAuth token (header `Authorization: OAuth <token>`). Get one at
https://yandex.ru/dev/disk/poligon/ or via your own OAuth app with scope
`cloud_api:disk.write`. Flow: ensure folder → ask for an upload href →
PUT the file → publish to get a shareable link.
"""
from __future__ import annotations

import urllib.parse
from pathlib import Path

from ._http import CloudError, request, request_json

API = "https://cloud-api.yandex.net/v1/disk"


def _auth(cfg: dict) -> dict:
    token = (cfg.get("token") or "").strip()
    if not token:
        raise CloudError("Не задан OAuth-токен Яндекс.Диска.")
    return {"Authorization": f"OAuth {token}"}


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

        # Big recordings take a while — give the upload a generous timeout.
        put_status, _ = request("PUT", href, data=src.read_bytes(), timeout=1800)
        if put_status not in (201, 202):
            raise CloudError(f"Загрузка не удалась (HTTP {put_status}).")

        # The file is safely uploaded now. Getting a public link is best-effort:
        # a timeout/error here must NOT fail the whole job (the recording is saved).
        url = None
        try:
            pub_status, _ = request("PUT", f"{API}/resources/publish",
                                    headers=headers, params={"path": remote})
            if pub_status in (200, 201):
                _, meta = request_json("GET", f"{API}/resources",
                                       headers=headers, params={"path": remote})
                url = (meta or {}).get("public_url")
        except CloudError:
            pass  # no shareable link, but the file is on the Disk
        return {"ok": True, "backend": "yandex_disk",
                "url": url or remote, "path": remote}
    except CloudError as e:
        return {"ok": False, "backend": "yandex_disk", "error": str(e)}
