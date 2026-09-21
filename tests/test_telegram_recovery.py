"""Коды восстановления пароля через Telegram (`app/telegram.py`).

Bot API подменяется на уровне `telegram._api` — весь остальной код модуля
(разбор обновлений, привязка, выбор канала в main) работает по-настоящему.
"""
import re
import time

import pytest

from app import security, sms, telegram
from tests.conftest import login, register

PHONE = "+79990000000"


class _FakeApi:
    """Заглушка Bot API: помнит вызовы, отдаёт заранее заданные ответы."""

    def __init__(self, fail_send=False):
        self.calls: list[tuple[str, dict]] = []
        self.fail_send = fail_send
        self.updates: list[dict] = []

    def __call__(self, method, params=None, timeout=15):
        self.calls.append((method, params or {}))
        if method == "getMe":
            return {"username": "meetflow_bot"}
        if method == "sendMessage":
            if self.fail_send:
                raise RuntimeError("403: Forbidden: bot was blocked by the user")
            return {"message_id": 1}
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return out
        raise RuntimeError(f"unexpected {method}")

    def sent(self):
        return [p for m, p in self.calls if m == "sendMessage"]


@pytest.fixture
def tg(monkeypatch):
    monkeypatch.setenv("VTX_TELEGRAM_BOT_TOKEN", "123:abc")
    fake = _FakeApi()
    monkeypatch.setattr(telegram, "_api", fake)
    monkeypatch.setattr(telegram, "_ME", None)
    telegram._LINK_CODES.clear()
    telegram.sent_messages.clear()
    yield fake
    telegram._LINK_CODES.clear()
    telegram.sent_messages.clear()


def _start(chat_id, text, username="ivan"):
    return {"update_id": 7, "message": {"text": text,
                                        "chat": {"id": chat_id},
                                        "from": {"username": username}}}


def _link(client, fake, chat_id=555):
    r = client.post("/api/profile/telegram/link", json={"password": "password123"})
    assert r.status_code == 200, r.text
    code = r.json()["code"]
    telegram.handle_update(_start(chat_id, f"/start {code}"))
    return code


# --------------------------------------------------------------------------- #
# Привязка
# --------------------------------------------------------------------------- #
def test_без_токена_канал_выключен(client, monkeypatch):
    monkeypatch.delenv("VTX_TELEGRAM_BOT_TOKEN", raising=False)
    register(client); login(client)
    assert client.get("/api/profile").json()["telegram_enabled"] is False
    r = client.post("/api/profile/telegram/link", json={"password": "password123"})
    assert r.status_code == 400
    assert telegram.send(1, "x") == (False, "VTX_TELEGRAM_BOT_TOKEN не задан")
    assert telegram.start_polling() is False


def test_привязка_требует_текущий_пароль(client, tg):
    register(client); login(client)
    r = client.post("/api/profile/telegram/link", json={"password": "wrong"})
    assert r.status_code == 400
    assert telegram._LINK_CODES == {}


def test_ссылка_и_код_привязки(client, tg):
    register(client); login(client)
    r = client.post("/api/profile/telegram/link", json={"password": "password123"}).json()
    assert r["bot"] == "meetflow_bot"
    assert r["url"] == f"https://t.me/meetflow_bot?start={r['code']}"
    assert r["ttl"] == telegram.LINK_CODE_TTL
    assert telegram._LINK_CODES[r["code"]]["user"] == "alice"


def test_start_с_кодом_привязывает_и_отвечает(client, tg):
    register(client); login(client)
    code = _link(client, tg, chat_id=555)
    assert security.telegram_of("alice") == {"chat_id": "555", "name": "@ivan"}
    assert code not in telegram._LINK_CODES, "код одноразовый"
    reply = tg.sent()[-1]
    assert reply["chat_id"] == "555" and "alice" in reply["text"]
    info = client.get("/api/profile").json()
    assert info["telegram_linked"] is True and info["telegram_name"] == "@ivan"
    assert "chat_id" not in info and "555" not in str(info)


def test_код_без_start_тоже_принимается(client, tg):
    register(client); login(client)
    r = client.post("/api/profile/telegram/link", json={"password": "password123"}).json()
    telegram.handle_update(_start(9, r["code"]))
    assert security.telegram_of("alice")["chat_id"] == "9"


def test_чужой_или_просроченный_код_не_привязывает(client, tg, monkeypatch):
    register(client); login(client)
    telegram.handle_update(_start(1, "/start nonsense"))
    assert security.telegram_of("alice") == {}
    assert "не подошёл" in tg.sent()[-1]["text"]
    r = client.post("/api/profile/telegram/link", json={"password": "password123"}).json()
    telegram._LINK_CODES[r["code"]]["exp"] = time.time() - 1
    telegram.handle_update(_start(1, f"/start {r['code']}"))
    assert security.telegram_of("alice") == {}


def test_start_без_кода_объясняет_как_привязать(tg):
    telegram.handle_update(_start(3, "/start"))
    assert "Привязать Telegram" in tg.sent()[-1]["text"]
    telegram.handle_update({"update_id": 1, "message": {"chat": {"id": 3}}})   # без текста
    assert len(tg.sent()) == 1


def test_новый_код_гасит_прежний_у_того_же_логина(client, tg):
    register(client); login(client)
    a = client.post("/api/profile/telegram/link", json={"password": "password123"}).json()["code"]
    b = client.post("/api/profile/telegram/link", json={"password": "password123"}).json()["code"]
    assert a not in telegram._LINK_CODES and b in telegram._LINK_CODES


def test_один_чат_один_аккаунт(client, tg):
    register(client); login(client)
    _link(client, tg, chat_id=555)
    c2 = client.__class__(client.app, raise_server_exceptions=True)
    register(c2, username="bob", phone="+79990000001"); login(c2, username="bob")
    _link(c2, tg, chat_id=555)
    assert security.telegram_of("bob")["chat_id"] == "555"
    assert security.telegram_of("alice") == {}, "чат переехал к bob — у alice снят"


def test_отвязка_требует_пароль(client, tg):
    register(client); login(client)
    _link(client, tg)
    assert client.post("/api/profile/telegram/unlink", json={"password": "no"}).status_code == 400
    assert security.telegram_of("alice")
    assert client.post("/api/profile/telegram/unlink",
                       json={"password": "password123"}).status_code == 200
    assert security.telegram_of("alice") == {}
    assert client.get("/api/profile").json()["telegram_linked"] is False


def test_привязка_переживает_перечитывание_users(client, tg):
    register(client); login(client)
    _link(client, tg, chat_id=42)
    users = security._load_users()
    assert users["alice"]["telegram_chat_id"] == "42"
    assert users["alice"]["telegram_name"] == "@ivan"


# --------------------------------------------------------------------------- #
# Восстановление пароля
# --------------------------------------------------------------------------- #
def _request(client, username="alice", phone=PHONE):
    return client.post("/api/auth/recover/request",
                       json={"username": username, "phone": phone})


def test_код_уходит_в_telegram_а_не_по_sms(client, tg):
    register(client); login(client)
    _link(client, tg, chat_id=555)
    client.post("/api/auth/logout")
    sms.sent_messages.clear()
    r = _request(client)
    assert r.status_code == 200
    assert sms.sent_messages == [], "SMS не должно быть — есть Telegram"
    msg = tg.sent()[-1]
    assert msg["chat_id"] == "555"
    code = re.search(r"\b(\d{6})\b", msg["text"]).group(1)
    r = client.post("/api/auth/recover/verify",
                    json={"username": "alice", "phone": PHONE, "code": code,
                          "new_password": "brand-new-pass1"})
    assert r.status_code == 200
    assert login(client, password="brand-new-pass1").status_code == 200


def test_без_привязки_код_идёт_по_sms(client, tg):
    register(client)
    sms.sent_messages.clear()
    _request(client)
    assert tg.sent() == []
    assert sms.sent_messages and "Код восстановления" in sms.sent_messages[-1]["text"]


def test_если_telegram_не_доставил_код_уходит_по_sms(client, tg):
    register(client); login(client)
    _link(client, tg, chat_id=555)
    client.post("/api/auth/logout")
    tg.fail_send = True
    sms.sent_messages.clear()
    _request(client)
    assert sms.sent_messages, "бот заблокирован — код должен уйти по SMS"
    tg_code = re.search(r"\b(\d{6})\b", telegram.sent_messages[-1]["text"]).group(1)
    sms_code = re.search(r"\b(\d{6})\b", sms.sent_messages[-1]["text"]).group(1)
    assert tg_code == sms_code, "тот же код, а не второй"


def test_ответ_не_выдаёт_канал_и_существование(client, tg):
    register(client); login(client)
    _link(client, tg)
    client.post("/api/auth/logout")
    ok = _request(client).json()
    bad = _request(client, username="nobody").json()
    assert ok == bad
    assert "telegram" not in ok["detail"].lower() or "sms" in ok["detail"].lower()


def test_телефон_остаётся_вторым_фактором(client, tg):
    """Telegram — канал доставки, а не замена телефона: без верного номера код
    не выдаётся вовсе."""
    register(client); login(client)
    _link(client, tg)
    client.post("/api/auth/logout")
    _request(client, phone="+79990009999")
    assert tg.sent()[-1]["text"].startswith("Готово"), "новых сообщений нет"


# --------------------------------------------------------------------------- #
# Опрос и отправка
# --------------------------------------------------------------------------- #
def test_poll_once_обрабатывает_и_сдвигает_offset(client, tg, monkeypatch):
    register(client); login(client)
    code = client.post("/api/profile/telegram/link", json={"password": "password123"}).json()["code"]
    monkeypatch.setattr(telegram, "_OFFSET", 0)
    tg.updates = [{"update_id": 10, "message": {"text": "/start bad", "chat": {"id": 1}, "from": {}}},
                  {"update_id": 11, "message": {"text": f"/start {code}", "chat": {"id": 2},
                                                "from": {"first_name": "Иван", "last_name": "П"}}}]
    assert telegram.poll_once(timeout=0) == 2
    assert telegram._OFFSET == 12
    assert security.telegram_of("alice") == {"chat_id": "2", "name": "Иван П"}
    m, p = tg.calls[[m for m, _ in tg.calls].index("getUpdates")]
    assert p["offset"] == 0 and p["allowed_updates"] == ["message"]


def test_send_не_бросает_и_пишет_журнал(tg):
    assert telegram.send(5, "привет") == (True, "sent")
    assert telegram.sent_messages[-1] == {"to": "5", "text": "привет"}
    tg.fail_send = True
    ok, detail = telegram.send(5, "ещё")
    assert ok is False and "403" in detail
