"""Атака на слой llm_base: что происходит при 429/503 без заголовка Retry-After.

Гипотеза: после разреза llm.py -> llm_base/llm в llm_base.py
потерялся `import re`, а `_retry_after()` этим `re` пользуется. Значит любой
429/503 БЕЗ заголовка Retry-After падает не в «подожди и повтори», а в
NameError — то есть ровно там, где вся защита от лимитов и должна была
сработать. Хуже того, NameError не опознаётся `_is_rate_limit`, поэтому ни
ротация ключей, ни «подожди окно TPM» не включаются.
"""
from __future__ import annotations

import email.message
import io
import urllib.error

import pytest

from app import llm_base


def _http_error(code: int, body: str, retry_after: str | None = None):
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        url="https://api.groq.com/openai/v1/chat/completions",
        code=code, msg="Too Many Requests", hdrs=hdrs,
        fp=io.BytesIO(body.encode("utf-8")))


class TestRetryAfter:
    def test_body_hint_is_parsed(self):
        """Groq не шлёт Retry-After, а пишет «try again in 12.34s» в теле.
        Именно ради этого случая в _retry_after есть регулярка."""
        body = ('{"error":{"message":"Rate limit reached for model ... '
                'Please try again in 12.34s. Need more?"}}')
        assert llm_base._retry_after(_http_error(429, body), body, default=8.0) == 12.34

    def test_no_hint_falls_back_to_default(self):
        """Ни заголовка, ни подсказки в теле — должен вернуться default."""
        body = '{"error":{"message":"Too many requests"}}'
        assert llm_base._retry_after(_http_error(429, body), body, default=8.0) == 8.0

    def test_header_wins_and_never_touches_the_body(self):
        """С заголовком regexp не нужен — эта ветка работает и сейчас."""
        body = "whatever"
        assert llm_base._retry_after(
            _http_error(429, body, retry_after="7"), body, default=8.0) == 7.0


class TestHttpPostJsonRetries:
    def test_429_without_header_is_retried_not_crashed(self, monkeypatch):
        """Сквозной сценарий: провайдер вернул 429 без Retry-After, второй
        запрос проходит. `_http_post_json` обязан переждать и вернуть ответ."""
        calls = {"n": 0}
        body = ('{"error":{"message":"Rate limit reached. '
                'Please try again in 1.5s"}}')

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _http_error(429, body)
            return _Resp(b'{"choices": [{"message": {"content": "ok"}}]}')

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        monkeypatch.setattr(llm_base.time, "sleep", lambda *_: None)

        out = llm_base._http_post_json("https://api.groq.com/openai/v1/chat/completions",
                                       {"model": "x"}, headers={})
        assert calls["n"] == 2, "второй попытки не было — ретрай не сработал"
        assert out["choices"][0]["message"]["content"] == "ok"

    def test_rate_limit_error_is_recognised_as_such(self, monkeypatch):
        """Даже если запрос в итоге провалился, наверх должна уйти ошибка,
        которую `_is_rate_limit` опознаёт: иначе ни ротация ключей
        (_RotatingProvider), ни ожидание окна TPM не включатся, и движок будет
        объявлен «сломанным» вместо «занят»."""
        body = '{"error":{"message":"Rate limit reached"}}'

        def fake_urlopen(req, timeout=None):
            raise _http_error(429, body)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        monkeypatch.setattr(llm_base.time, "sleep", lambda *_: None)

        with pytest.raises(Exception) as ei:
            llm_base._http_post_json("https://api.groq.com/openai/v1/chat/completions",
                                     {"model": "x"}, headers={}, max_retries=1)
        assert llm_base._is_rate_limit(ei.value), (
            f"ошибка {type(ei.value).__name__}: {ei.value} — не опознана как "
            "упор в лимит, ключи ротироваться не будут")
