"""Общий фундамент провайдеров: интерфейс, HTTP-обвязка и работа по API-ключу.

Вынесено из llm.py, чтобы отдельные модули провайдеров могли опираться на это,
не замыкая импорт на сам llm.py. Слои: llm_base -> модули провайдеров -> llm.
"""
from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

from . import config


class GenerationCancelled(Exception):
    """Raised from inside a streaming generation when the caller asks to stop,
    so cancellation is responsive mid-stream (not only between chunks)."""


class LLMProvider(Protocol):
    name: str

    def complete(self, prompt: str, max_tokens: int = 2000, force_json: bool = True) -> str:
        """Send the prompt to the model and return its text response.

        force_json asks the backend to constrain the answer to valid JSON
        (used for the real analysis). Set False for a plain ping, e.g. when
        validating an API key.
        """
        ...


# ---------------------------------------------------------------------------
def _retry_after(e: urllib.error.HTTPError, body: str, default: float) -> float:
    """Seconds to wait before retrying a 429/503, from header or response body."""
    ra = e.headers.get("Retry-After") if e.headers else None
    if ra:
        try:
            return float(ra)
        except ValueError:
            pass
    # Groq's body says e.g. "Please try again in 12.34s".
    m = re.search(r"try again in ([\d.]+)\s*s", body or "")
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return default


def _safe_url(url: str) -> str:
    """Адрес без query — в query у некоторых провайдеров лежит API-ключ.

    Текст ошибки уходит далеко: в причину отката, в карточку задачи и в шапку
    Word-протокола, который потом попадает в облако и в Weeek. Секрету там не
    место.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:      # noqa: BLE001 — диагностика не должна падать
        return url.split("?")[0]


def _http_post_json(url: str, payload: dict, headers: dict, timeout: int = 180,
                    max_retries: int = 3) -> dict:
    """POST a JSON body and return the parsed JSON response.

    Retries on 429 (rate limit) / 503 / 529 (overloaded), honouring Retry-After — free cloud tiers
    (e.g. Groq) rate-limit easily when a long transcript is analysed in chunks.
    """
    data = json.dumps(payload).encode("utf-8")
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        # A browser-like User-Agent: some providers (e.g. Groq) sit behind
        # Cloudflare, which rejects the default "Python-urllib/x.y" agent with a
        # 403 / error 1010 ("banned by browser signature").
        req.add_header("User-Agent",
                       "Mozilla/5.0 (X11; Linux x86_64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0 Safari/537.36")
        req.add_header("Accept", "application/json")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            # 529 — «Service temporarily overloaded»: временная перегрузка,
            # ровно как 503, так отвечают несколько облаков. Без неё запрос
            # сразу считался провалом движка и встреча уходила запасному — по
            # журналу протоколов за август так потерялись четыре встречи из
            # двадцати одной.
            if e.code in (429, 503, 529) and attempt < max_retries:
                wait = min(_retry_after(e, body, default=8 * (attempt + 1)), 30)
                time.sleep(wait + 0.5)
                attempt += 1
                continue
            raise RuntimeError(f"HTTP {e.code} от {_safe_url(url)}: {body[:300]}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Не удалось подключиться к {_safe_url(url)}: {e.reason}") from e




class _KeyProviderMixin:
    """Общее для провайдеров, работающих по API-ключу.

    `_retries` — сколько раз ждать при 429 ВНУТРИ одного ключа. Обёртка ротации
    выставляет 0, когда ключей несколько: смысл ротации в том, чтобы при лимите
    сразу уйти на следующий ключ, а не спать по 30 секунд на исчерпанном.
    """

    max_retries: int | None = None

    @property
    def _retries(self) -> int:
        return 3 if self.max_retries is None else int(self.max_retries)


def is_overloaded(e: Exception) -> bool:
    """Сервис перегружен и просит зайти позже (503/529), а не отказывает.

    529 «Service temporarily overloaded» — это НЕ исчерпанный лимит ключа и не
    отсутствие прав: ключ жив, модель доступна, просто инференс сейчас занят.
    Отличать важно — по такой ошибке нужно подождать и повторить, а не менять
    ключ и не объявлять движок отказавшим.
    """
    s = f"{type(e).__name__} {e}".lower()
    return ("529" in s or "overloaded" in s or "503" in s
            or "service unavailable" in s or "temporarily" in s)


def _is_rate_limit(e: Exception) -> bool:
    """Whether an error means the current API key hit its rate/quota limit."""
    s = f"{type(e).__name__} {e}".lower()
    return ("429" in s or "too many requests" in s or "rate limit" in s
            or "rate_limit" in s or "ratelimit" in s or "quota" in s
            or "resource_exhausted" in s or "insufficient_quota" in s)


def is_key_rejected(e: Exception) -> bool:
    """Ключ отвергнут насовсем: не авторизован или лишён прав (401/403).

    Отличается и от лимита, и от перегрузки: ждать бессмысленно, повторять по
    этому же ключу — тоже. А вот СОСЕДНИЙ ключ в пуле к ошибке отношения не
    имеет, и именно это стоило пользователю протокола: под «своим ключом»
    лежали два РАЗНЫХ подключения, первое отвечало 401, и весь движок считался
    отказавшим — до второго, рабочего, дело не доходило.
    """
    s = f"{type(e).__name__} {e}".lower()
    if "429" in s:            # лимит — это не отказ ключа, у него своя обработка
        return False
    return ("401" in s or "403" in s or "unauthorized" in s
            or "missing authentication" in s or "invalid api key"  in s
            or "invalid_api_key" in s or "api_key_invalid" in s
            or "permission denied" in s or "permission_denied" in s
            or "forbidden" in s)
