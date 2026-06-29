"""Tiny urllib helpers shared by the cloud uploaders (no extra dependencies)."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class CloudError(RuntimeError):
    """A cloud backend returned an error or was misconfigured."""


def request(method: str, url: str, *, headers: dict | None = None,
            params: dict | None = None, data: bytes | None = None,
            timeout: int = 120) -> tuple[int, bytes]:
    """Raw HTTP request. Returns (status, body). Raises CloudError on transport
    errors; HTTP error statuses are returned (so callers can treat e.g. 409 as
    'already exists')."""
    if params:
        url += "?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        # A read timeout surfaces as URLError(reason=timeout) or a bare
        # TimeoutError; treat both as a transport error, not a crash.
        raise CloudError(f"Сеть недоступна: {e.reason}") from e
    except (TimeoutError, OSError) as e:
        raise CloudError(f"Таймаут/сетевая ошибка: {e}") from e


def request_json(method: str, url: str, **kw) -> tuple[int, Any]:
    status, body = request(method, url, **kw)
    try:
        parsed = json.loads(body.decode("utf-8")) if body else None
    except ValueError:
        parsed = {"_raw": body.decode("utf-8", "replace")[:300]}
    return status, parsed
