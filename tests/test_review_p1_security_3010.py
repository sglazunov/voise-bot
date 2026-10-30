"""Сводное ревью 30.10.2026: P1 безопасности.

P1-1 — пароль в профиле менялся по одному номеру телефона: ни кода, ни лимита
попыток, старые сессии оставались живыми. Угнанной сессии хватало, чтобы
забрать аккаунт насовсем.
P1-2 — битый users.json читался как «пользователей нет», и следующая запись
(регистрация, смена пароля) затирала все учётки в файловом режиме.
P1-3 — login CSRF: проверка Sec-Fetch-Site/Origin включалась только при живой
сессии, поэтому чужая страница могла незаметно залогинить браузер жертвы в
аккаунт атакующего.
P1-4 — GigaChat при ошибке сертификата молча уходил на TLS без проверки, и
ключ авторизации ехал по непроверенному каналу.
"""
from __future__ import annotations

import re
import ssl
import urllib.error
import urllib.request

import pytest
from starlette.testclient import TestClient

from app import config, llm, security, sms
from app.main import app
from conftest import login, register

PHONE = "+79990000000"


def _last_code() -> str:
    assert sms.sent_messages, "SMS с кодом не отправлено"
    m = re.search(r"\b(\d{6})\b", sms.sent_messages[-1]["text"])
    assert m, sms.sent_messages[-1]["text"]
    return m.group(1)


# --------------------------------------------------------------------------- #
# P1-1
# --------------------------------------------------------------------------- #
class TestСменаПароляВПрофиле:
    def test_одного_телефона_мало(self, client):
        register(client)
        r = client.post("/api/profile/password", json={
            "new_password": "changed-by-phone1", "phone": PHONE})
        assert r.status_code == 400
        client.post("/api/auth/logout")
        assert login(client, "alice", "password123").status_code == 200

    def test_телефон_и_код_из_смс_меняют_пароль(self, client):
        register(client)
        assert client.post("/api/auth/recover/request",
                           json={"username": "alice", "phone": PHONE}).status_code == 200
        r = client.post("/api/profile/password", json={
            "new_password": "changed-by-code1", "phone": PHONE, "code": _last_code()})
        assert r.status_code == 200, r.text
        client.post("/api/auth/logout")
        assert login(client, "alice", "changed-by-code1").status_code == 200

    def test_неверный_код_не_меняет_пароль(self, client):
        register(client)
        client.post("/api/auth/recover/request", json={"username": "alice", "phone": PHONE})
        r = client.post("/api/profile/password", json={
            "new_password": "changed-by-code1", "phone": PHONE, "code": "000000"})
        assert r.status_code == 400
        client.post("/api/auth/logout")
        assert login(client, "alice", "password123").status_code == 200

    def test_перебор_текущего_пароля_упирается_в_лимит(self, client):
        register(client)
        for _ in range(security.BRUTE_MAX_ATTEMPTS):
            r = client.post("/api/profile/password", json={
                "new_password": "changed-pass-1", "old_password": "wrong-guess"})
            assert r.status_code == 400
        # Даже верный пароль теперь ждёт: лимит считается по логину, а не по IP.
        r = client.post("/api/profile/password", json={
            "new_password": "changed-pass-1", "old_password": "password123"})
        assert r.status_code == 429

    def test_смена_гасит_чужие_сессии_а_свою_оставляет(self, client):
        register(client)
        other = TestClient(app)
        assert login(other, "alice", "password123").status_code == 200
        assert other.get("/api/profile").status_code == 200
        r = client.post("/api/profile/password", json={
            "new_password": "changed-pass-1", "old_password": "password123"})
        assert r.status_code == 200, r.text
        assert other.get("/api/profile").status_code == 401   # угнанная — мертва
        assert client.get("/api/profile").status_code == 200  # своя — новая кука


# --------------------------------------------------------------------------- #
# P1-2
# --------------------------------------------------------------------------- #
class TestБитыйUsersJson:
    def _break(self) -> str:
        broken = '{"alice": {"pw": "x", "phone": "79990000000"'   # оборван посреди
        (config.DATA_DIR / "users.json").write_text(broken, encoding="utf-8")
        return broken

    def test_чтение_падает_а_не_возвращает_пустоту(self):
        self._break()
        with pytest.raises(security.UsersStoreError):
            security._load_users()

    def test_регистрация_не_затирает_файл(self, client):
        broken = self._break()
        r = register(client, "mallory", phone="+79995556677")
        assert r.status_code == 503
        assert (config.DATA_DIR / "users.json").read_text(encoding="utf-8") == broken

    def test_вход_отвечает_503(self, client):
        self._break()
        assert login(client).status_code == 503

    def test_не_словарь_тоже_битый(self):
        (config.DATA_DIR / "users.json").write_text("[]", encoding="utf-8")
        with pytest.raises(security.UsersStoreError):
            security._load_users()

    def test_нет_файла_это_пустой_сервер(self):
        assert security._load_users() == {}


# --------------------------------------------------------------------------- #
# P1-3
# --------------------------------------------------------------------------- #
class TestLoginCSRF:
    def test_вход_с_чужого_сайта_отклонён(self, client):
        register(client); client.post("/api/auth/logout")
        r = client.post("/api/auth/login",
                        json={"username": "alice", "password": "password123"},
                        headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403
        assert security.SESSION_COOKIE not in r.cookies

    def test_вход_с_соседнего_поддомена_отклонён(self, client):
        register(client); client.post("/api/auth/logout")
        r = client.post("/api/auth/login",
                        json={"username": "alice", "password": "password123"},
                        headers={"Sec-Fetch-Site": "same-site"})
        assert r.status_code == 403

    def test_старый_браузер_с_чужим_origin(self, client):
        register(client); client.post("/api/auth/logout")
        r = client.post("/api/auth/login",
                        json={"username": "alice", "password": "password123"},
                        headers={"Origin": "https://evil.example"})
        assert r.status_code == 403

    def test_регистрация_с_чужого_сайта_отклонена(self, client):
        r = client.post("/api/auth/register",
                        json={"username": "alice", "password": "password123",
                              "code": "", "phone": PHONE},
                        headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403
        assert not security.user_exists("alice")

    def test_вход_со_своего_сайта_проходит(self, client):
        register(client); client.post("/api/auth/logout")
        r = client.post("/api/auth/login",
                        json={"username": "alice", "password": "password123"},
                        headers={"Sec-Fetch-Site": "same-origin",
                                 "Origin": "http://testserver"})
        assert r.status_code == 200


# --------------------------------------------------------------------------- #
# P1-4
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, body: bytes):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestGigaChatTLS:
    @pytest.fixture
    def calls(self, monkeypatch):
        seen: list = []

        def fake(req, timeout=None, context=None):
            seen.append(context)
            if context is llm.GigaChatProvider._ctx:
                raise urllib.error.URLError(ssl.SSLCertVerificationError("self signed"))
            return _Resp(b"{}")

        monkeypatch.setattr(urllib.request, "urlopen", fake)
        monkeypatch.setattr(llm.GigaChatProvider, "_strict", False)
        monkeypatch.setattr(llm.GigaChatProvider, "_lax_hosts", set())
        monkeypatch.delenv("VTX_GIGACHAT_INSECURE_TLS", raising=False)
        return seen

    def _req(self):
        return urllib.request.Request("https://ngw.devices.sberbank.ru:9443/api/v2/oauth")

    def test_без_сертификата_соединения_нет(self, calls):
        with pytest.raises(urllib.error.URLError):
            llm.GigaChatProvider._open(self._req(), 5)
        assert calls == [llm.GigaChatProvider._ctx]   # без второй, непроверенной попытки

    def test_откат_только_по_явной_переменной(self, calls, monkeypatch):
        monkeypatch.setenv("VTX_GIGACHAT_INSECURE_TLS", "1")
        llm.GigaChatProvider._open(self._req(), 5)
        assert calls == [llm.GigaChatProvider._ctx, llm.GigaChatProvider._ctx_lax]
