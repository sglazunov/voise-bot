"""Yandex Disk uploader via the REST API (cloud-api.yandex.net).

Auth: an OAuth token (header `Authorization: OAuth <token>`). Get one at
https://yandex.ru/dev/disk/poligon/ or via your own OAuth app with scope
`cloud_api:disk.write`. Flow: ensure folder → ask for an upload href →
PUT the file → publish to get a shareable link.
"""
from __future__ import annotations

import time
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


# Публикация — отдельный шаг ПОСЛЕ заливки, и падает он отдельно: 17.09 запись
# и протокол «Ревью веб-дизайнеры» легли на Диск, а PUT /publish не прошёл.
# Раньше это возвращалось как ok=True без url и без пояснения: карточка писала
# «запись локально», Weeek не получал ссылки, локальную копию удаляли, а
# поздняя дозагрузка не запускалась — ok же. Теперь: три попытки с паузой, а
# при неудаче — внятная причина с HTTP-кодом.
_PUBLISH_ATTEMPTS = 3
_PUBLISH_PAUSE = (3, 8)


def _publish(remote: str, cfg: dict, headers: dict) -> tuple[str | None, str | None]:
    """Опубликовать файл и вернуть (публичная ссылка, пояснение при неудаче)."""
    last = "неизвестная ошибка"
    for i in range(_PUBLISH_ATTEMPTS):
        if i:
            time.sleep(_PUBLISH_PAUSE[min(i - 1, len(_PUBLISH_PAUSE) - 1)])
        try:
            pub_status, body = request("PUT", f"{API}/resources/publish",
                                       headers=headers, params={"path": remote})
            if pub_status not in (200, 201):
                last = f"публикация отклонена (HTTP {pub_status}): {body[:120].decode('utf-8', 'replace')}"
                if pub_status in (401, 403, 404):
                    break
                continue
            meta_status, meta = request_json(
                "GET", f"{API}/resources", headers=_read_auth(cfg),
                params={"path": remote, "fields": "public_url"})
            if meta_status == 200:
                url = (meta or {}).get("public_url")
                if url:
                    return url, None
                last = "Диск не вернул public_url после публикации"
                continue
            if meta_status in (401, 403):
                return None, ("Файл загружен, но публичную ссылку не получить: нужен доступ "
                              "на ЧТЕНИЕ (cloud_api:disk.read). Впишите отдельный «токен "
                              "чтения» Я.Диска в настройках, либо добавьте «Чтение всего "
                              "Диска» в приложении Яндекса и получите токен заново.")
            last = f"чтение ссылки не удалось (HTTP {meta_status})"
        except CloudError as e:
            last = str(e)
    return None, f"Файл загружен на Диск, но публичная ссылка не получена: {last}"


def publish(remote: str, cfg: dict) -> dict:
    """Повторная публикация уже лежащего на Диске файла (поздняя дозагрузка
    и «Прикрепить заново»): заливать сотни мегабайт заново не нужно."""
    try:
        url, note = _publish(remote, cfg, _auth(cfg))
    except CloudError as e:
        return {"ok": False, "backend": "yandex_disk", "path": remote, "error": str(e)}
    if url:
        return {"ok": True, "backend": "yandex_disk", "url": url, "path": remote}
    return {"ok": False, "backend": "yandex_disk", "path": remote, "error": note}


def _public_url(path: str, cfg: dict) -> tuple[str | None, int]:
    status, meta = request_json("GET", f"{API}/resources", headers=_read_auth(cfg),
                                params={"path": path, "fields": "public_url"})
    return ((meta or {}).get("public_url") if status == 200 else None), status


def _move_once(src: str, dst: str, headers: dict) -> tuple[bool, str]:
    """Переместить файл; асинхронный ответ (202) дождаться до 30 с."""
    status, info = request_json("POST", f"{API}/resources/move", headers=headers,
                                params={"from": src, "path": dst, "overwrite": "false"})
    if status == 201:
        return True, ""
    if status == 409:
        return False, "в папке серии уже есть файл с таким именем"
    if status == 202 and isinstance(info, dict) and info.get("href"):
        for _ in range(30):
            st, op = request_json("GET", info["href"], headers=headers)
            state = (op or {}).get("status") if st == 200 else None
            if state == "success":
                return True, ""
            if state == "failed":
                return False, "Диск не смог переместить файл"
            time.sleep(1)
        return False, "перемещение не закончилось за 30 с"
    return False, f"перемещение отклонено (HTTP {status})"


def move(src: str, dst: str, cfg: dict) -> dict:
    """Переложить уже опубликованный файл в другую папку (docs/ТЗ-ПАПКИ-ВСТРЕЧ.md).

    ⚠️ Ссылка на файл уже стоит в Weeek. Поэтому ссылку сверяем ДО и ПОСЛЕ:
    если после переезда она другая или пропала — файл возвращается на место, а
    переезд считается несостоявшимся. Сохранность ссылки при перемещении на
    живом Диске не проверена — проверка здесь и есть страховка."""
    try:
        headers = _auth(cfg)
        before, st = _public_url(src, cfg)
        if st == 404:
            return {"ok": False, "retry": False, "error": "файла уже нет на Диске"}
        if st != 200:
            return {"ok": False, "retry": True, "error": f"Диск не ответил (HTTP {st})"}
        _ensure_folder(dst.rsplit("/", 1)[0], headers)
        ok, why = _move_once(src, dst, headers)
        if not ok:
            return {"ok": False, "retry": "уже есть" not in why, "error": why}
        after, _st = _public_url(dst, cfg)
        if before and after != before:
            _move_once(dst, src, headers)
            return {"ok": False, "retry": False,
                    "error": "после переезда сменилась публичная ссылка — файл "
                             "возвращён на место, чтобы ссылка в Weeek не сломалась"}
        return {"ok": True, "backend": "yandex_disk", "path": dst, "url": after}
    except CloudError as e:
        return {"ok": False, "retry": True, "error": str(e)}


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
        # (https://disk.yandex.ru/d/…). Без ссылки файл людям недоступен —
        # причина уходит в public_note, а delivery решает, что делать дальше.
        url, note = _publish(remote, cfg, headers)
        # `url` is the real share link (or None). Never return the internal
        # disk:/ path as a link — it isn't openable.
        return {"ok": True, "backend": "yandex_disk", "published": bool(url),
                "url": url, "path": remote, "public_note": note}
    except CloudError as e:
        return {"ok": False, "backend": "yandex_disk", "error": str(e)}
