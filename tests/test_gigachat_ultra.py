"""GigaChat на новом адресе api.giga.chat с моделью Ultra (2026-09-15).

Сеть до Сбера из тестов закрыта — подменяется `urllib.request.urlopen` внутри
`app.llm`, поэтому отрабатывает весь настоящий код провайдера: OAuth, кэш
токена, заголовки, разбор ответа, учёт расхода, откат TLS и ожидание 429.
"""
from __future__ import annotations

import io
import json
import ssl
import threading
import urllib.error
import urllib.request

import pytest

from app import config, llm, usage


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _http_error(code: int, body: str = "", headers: dict | None = None):
    return urllib.error.HTTPError("https://x", code, "err",
                                  headers or {}, io.BytesIO(body.encode()))


def _chat_answer(text='{"topics": []}', usage_=None):
    d = {"choices": [{"message": {"role": "assistant", "content": text}}]}
    if usage_ is not None:
        d["usage"] = usage_
    return d


class _Fake:
    """Подмена urlopen: запоминает запросы, отдаёт ответы по адресу."""

    def __init__(self, chat=None, oauth=None):
        self.calls: list[tuple[urllib.request.Request, ssl.SSLContext]] = []
        self.chat = chat or [_chat_answer()]
        self.oauth = oauth or {"access_token": "tok-1", "expires_at": 4102444800000}
        self.errors: list = []

    def __call__(self, req, timeout=None, context=None):
        self.calls.append((req, context))
        if self.errors:
            e = self.errors.pop(0)
            if e is not None:
                raise e
        if "oauth" in req.full_url:
            return _Resp(json.dumps(self.oauth).encode())
        body = self.chat.pop(0) if len(self.chat) > 1 else self.chat[0]
        return _Resp(json.dumps(body).encode())


@pytest.fixture
def fake(monkeypatch):
    f = _Fake()
    monkeypatch.setattr(urllib.request, "urlopen", f)
    monkeypatch.setattr(llm.GigaChatProvider, "_TOKENS", {})
    monkeypatch.setattr(llm.GigaChatProvider, "_lax_hosts", set())
    monkeypatch.setattr(llm.time, "sleep", lambda *_: None)
    return f


def _hdr(req, name):
    return req.get_header(name.replace("-", "_").title().replace("_", "-")) \
        or req.get_header(name)


class TestАдресаИМодель:
    def test_запрос_идёт_на_новый_хост_а_токен_на_старый(self, fake):
        p = llm.GigaChatProvider(api_key="a2V5", extra="GIGACHAT_API_PERS")
        assert p.complete("текст") == '{"topics": []}'
        urls = [r.full_url for r, _ in fake.calls]
        assert urls == ["https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
                        "https://api.giga.chat/v1/chat/completions"]

    def test_модель_по_умолчанию_ultra(self, fake):
        p = llm.GigaChatProvider(api_key="a2V5")
        p.complete("текст")
        payload = json.loads(fake.calls[1][0].data)
        assert payload["model"] == "GigaChat-3-Ultra"
        assert config.PROVIDER_MODELS["gigachat"][0]["value"] == "GigaChat-3-Ultra"

    def test_ключ_basic_на_oauth_и_bearer_на_запросе(self, fake):
        llm.GigaChatProvider(api_key="a2V5", extra="GIGACHAT_API_B2B").complete("x")
        oauth, chat = fake.calls[0][0], fake.calls[1][0]
        assert oauth.get_header("Authorization") == "Basic a2V5"
        assert oauth.data == b"scope=GIGACHAT_API_B2B"
        assert oauth.get_header("Rquid")
        assert chat.get_header("Authorization") == "Bearer tok-1"

    def test_адреса_настраиваются(self, fake, monkeypatch):
        monkeypatch.setattr(config, "GIGACHAT_BASE_URL",
                            "https://gigachat.devices.sberbank.ru/api/v1/")
        llm.GigaChatProvider(api_key="a2V5").complete("x")
        assert fake.calls[1][0].full_url == \
            "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

    def test_модель_через_двоеточие(self, fake):
        p = llm.get_provider("gigachat:GigaChat-Max",
                             {"gigachat": [{"key": "a2V5", "extra": ""}]})
        p.complete("x")
        payload = json.loads(fake.calls[1][0].data)
        assert payload["model"] == "GigaChat-Max"


class TestТокен:
    def test_токен_берётся_один_раз_на_ключ(self, fake):
        llm.GigaChatProvider(api_key="a2V5").complete("x")
        llm.GigaChatProvider(api_key="a2V5").complete("y")
        oauth_calls = [r for r, _ in fake.calls if "oauth" in r.full_url]
        assert len(oauth_calls) == 1

    def test_у_другого_ключа_свой_токен(self, fake):
        llm.GigaChatProvider(api_key="a2V5").complete("x")
        llm.GigaChatProvider(api_key="b2V5").complete("y")
        oauth_calls = [r for r, _ in fake.calls if "oauth" in r.full_url]
        assert len(oauth_calls) == 2

    def test_пустой_токен_внятная_ошибка(self, fake):
        fake.oauth = {"error": "bad"}
        with pytest.raises(RuntimeError, match="нет access_token"):
            llm.GigaChatProvider(api_key="a2V5").complete("x")

    def test_401_на_oauth_считается_отвергнутым_ключом(self, fake):
        fake.errors = [_http_error(401, "Unauthorized")]
        with pytest.raises(RuntimeError) as ei:
            llm.GigaChatProvider(api_key="a2V5").complete("x")
        assert "OAuth HTTP 401" in str(ei.value)
        assert llm.is_key_rejected(ei.value)


class TestРасходИОтвет:
    def test_расход_попадает_в_учёт(self, fake):
        fake.chat = [_chat_answer(usage_={"prompt_tokens": 120,
                                          "completion_tokens": 30,
                                          "total_tokens": 150})]
        with usage.collect() as acc:
            llm.GigaChatProvider(api_key="a2V5").complete("x")
        assert acc["in"] == 120 and acc["out"] == 30
        assert "gigachat/GigaChat-3-Ultra" in acc["by_model"]

    def test_пустой_content_внятная_ошибка(self, fake):
        fake.chat = [{"choices": [{"message": {"content": None}}]}]
        with pytest.raises(RuntimeError, match="пустой ответ"):
            llm.GigaChatProvider(api_key="a2V5").complete("x")


class TestОдинПоток:
    def test_429_ждётся_и_повторяется(self, fake):
        # Первый запрос — OAuth (ок), второй — чат 429, третий — чат ок.
        fake.errors = [None, _http_error(429, "Too Many Requests",
                                         {"Retry-After": "1"})]
        assert llm.GigaChatProvider(api_key="a2V5").complete("x") == '{"topics": []}'
        chat_calls = [r for r, _ in fake.calls if "chat" in r.full_url]
        assert len(chat_calls) == 2

    def test_при_ротации_ключей_429_отдаётся_сразу(self, fake):
        p = llm.GigaChatProvider(api_key="a2V5")
        p.max_retries = 0
        fake.errors = [None, _http_error(429, "Too Many Requests")]
        with pytest.raises(RuntimeError) as ei:
            p.complete("x")
        assert llm._is_rate_limit(ei.value)

    def test_вызовы_сериализуются_замком(self, fake):
        """Freemium — один поток: второй запрос не уходит, пока идёт первый."""
        inside = threading.Event()
        release = threading.Event()
        overlap: list[bool] = []
        real = fake

        def slow(req, timeout=None, context=None):
            if "chat" in req.full_url:
                if inside.is_set():
                    overlap.append(True)
                inside.set()
                release.wait(2)
                inside.clear()
            return real(req, timeout=timeout, context=context)

        import app.llm as m
        m.urllib.request.urlopen = slow
        p = llm.GigaChatProvider(api_key="a2V5")
        ts = [threading.Thread(target=p.complete, args=("x",)) for _ in range(2)]
        for t in ts:
            t.start()
        release.set()
        for t in ts:
            t.join(5)
        assert not overlap
        assert len([r for r, _ in real.calls if "chat" in r.full_url]) == 2


class TestTLS:
    def _cert_error(self):
        return urllib.error.URLError(ssl.SSLCertVerificationError("self signed"))

    def test_без_сертификата_минцифры_откат_на_непроверенное(self, fake, monkeypatch):
        monkeypatch.setattr(llm.GigaChatProvider, "_strict", False)
        fake.errors = [self._cert_error()]
        llm.GigaChatProvider(api_key="a2V5").complete("x")
        ctxs = [c for _, c in fake.calls]
        assert ctxs[0] is llm.GigaChatProvider._ctx
        assert ctxs[1] is llm.GigaChatProvider._ctx_lax
        assert ctxs[1].verify_mode == ssl.CERT_NONE

    def test_с_сертификатом_минцифры_отката_нет(self, fake, monkeypatch):
        monkeypatch.setattr(llm.GigaChatProvider, "_strict", True)
        fake.errors = [self._cert_error()]
        with pytest.raises(RuntimeError, match="Не удалось подключиться"):
            llm.GigaChatProvider(api_key="a2V5").complete("x")
        assert len(fake.calls) == 1

    def test_обрыв_сети_не_считается_проблемой_сертификата(self, fake, monkeypatch):
        monkeypatch.setattr(llm.GigaChatProvider, "_strict", False)
        fake.errors = [urllib.error.URLError(ConnectionResetError())]
        with pytest.raises(RuntimeError, match="Не удалось подключиться"):
            llm.GigaChatProvider(api_key="a2V5").complete("x")
        assert len(fake.calls) == 1
