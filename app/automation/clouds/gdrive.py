"""Google Drive uploader via the REST API (no google-api-python-client dep).

Auth: OAuth2. Store client_id + client_secret + refresh_token in settings
(gdrive). We exchange the refresh_token for a short-lived access_token, then do
a multipart upload, then (optionally) make the file link-shareable.

Getting a refresh_token: create an OAuth client (Desktop) in Google Cloud
Console, enable the Drive API, run the consent flow once with scope
https://www.googleapis.com/auth/drive.file and keep the refresh token.
"""
from __future__ import annotations

import json
import mimetypes
import os
import urllib.parse
import uuid
from pathlib import Path

from ._http import CloudError, request, request_json

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FILES_URL = "https://www.googleapis.com/drive/v3/files"

# Сколько ждать выгрузку целиком. Умолчание помощника — 120 секунд: этого
# хватает документу, но не часовому видео.
_UPLOAD_TIMEOUT = int(os.getenv("VTX_UPLOAD_TIMEOUT", "1800"))


class _MultipartBody:
    """Тело multipart, читаемое кусками: заголовок, файл, хвост.

    urllib дёргает read(n) у объекта-тела и требует заранее известной длины —
    её отдаём в Content-Length. Так файл ни разу не оказывается в памяти
    целиком.
    """

    def __init__(self, prefix: bytes, path: Path, suffix: bytes) -> None:
        self._prefix = prefix
        self._suffix = suffix
        self._file = path.open("rb")
        self._stage = 0
        self.length = len(prefix) + path.stat().st_size + len(suffix)

    def read(self, size: int = -1) -> bytes:
        if self._stage == 0:                       # заголовок части
            self._stage = 1
            return self._prefix
        if self._stage == 1:                       # сам файл
            chunk = self._file.read(size if size and size > 0 else 1 << 20)
            if chunk:
                return chunk
            self._stage = 2
        if self._stage == 2:                       # завершающая граница
            self._stage = 3
            return self._suffix
        return b""

    def close(self) -> None:
        try:
            self._file.close()
        except OSError:
            pass


def readiness(cfg: dict) -> dict:
    have = all(cfg.get(k) for k in ("client_id", "client_secret", "refresh_token"))
    if not have:
        return {"ready": False,
                "detail": "Нужны client_id, client_secret и refresh_token Google."}
    return {"ready": True,
            "detail": f"Папка: {cfg.get('folder_id') or 'корень My Drive'}"}


def _access_token(cfg: dict) -> str:
    for k in ("client_id", "client_secret", "refresh_token"):
        if not cfg.get(k):
            raise CloudError(f"Google Drive: не задан {k}.")
    body = urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "refresh_token": cfg["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    status, data = request_json(
        "POST", TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    if status != 200 or not isinstance(data, dict) or "access_token" not in data:
        raise CloudError(f"Google не выдал токен ({status}): {data}")
    return data["access_token"]


def upload(file_path: str, name: str, cfg: dict) -> dict:
    src = Path(file_path)
    if not src.exists():
        return {"ok": False, "backend": "gdrive", "error": "Файл не найден."}
    try:
        token = _access_token(cfg)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        meta = {"name": name}
        if cfg.get("folder_id"):
            meta["parents"] = [cfg["folder_id"]]

        boundary = f"vtx{uuid.uuid4().hex}"
        prefix = b"".join([
            f"--{boundary}\r\n".encode(),
            b"Content-Type: application/json; charset=UTF-8\r\n\r\n",
            json.dumps(meta, ensure_ascii=False).encode("utf-8"), b"\r\n",
            f"--{boundary}\r\n".encode(),
            f"Content-Type: {mime}\r\n\r\n".encode(),
        ])
        suffix = b"\r\n" + f"--{boundary}--\r\n".encode()
        # Тело отдаём потоком. Раньше здесь стоял src.read_bytes(): часовая
        # запись целиком попадала в память, и даже дважды — сам файл и склеенное
        # тело. Вместе со 120-секундным таймаутом на весь POST это означало, что
        # большая запись почти гарантированно не выгружалась и уходила в ретраи.
        body = _MultipartBody(prefix, src, suffix)
        try:
            status, data = request_json(
                "POST", UPLOAD_URL, params={"uploadType": "multipart", "fields": "id"},
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": f"multipart/related; boundary={boundary}",
                         "Content-Length": str(body.length)},
                data=body, timeout=_UPLOAD_TIMEOUT)
        finally:
            body.close()
        if status not in (200, 201) or not isinstance(data, dict) or "id" not in data:
            raise CloudError(f"Загрузка в Google Drive не удалась ({status}): {data}")
        file_id = data["id"]

        # Make it shareable by link (best effort).
        request("POST", f"{FILES_URL}/{file_id}/permissions",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                data=json.dumps({"role": "reader", "type": "anyone"}).encode())
        return {"ok": True, "backend": "gdrive",
                "url": f"https://drive.google.com/file/d/{file_id}/view",
                "path": file_id}
    except CloudError as e:
        return {"ok": False, "backend": "gdrive", "error": str(e)}
