"""Атака: перебор «есть ли такой аккаунт с таким телефоном».

Шаг 1 восстановления пароля намеренно отвечает ОДИНАКОВО в любом случае —
об этом прямо сказано в коде (`_RECOVER_SENT`, main.py:337, и docstring
`generate_recovery_code`: «the caller … must otherwise stay silent (no account
enumeration)»).

Но ветка «cooldown» отвечает НЕ так же: она добавляет поле `retry_after`
(main.py:358). В cooldown можно попасть только после успешной проверки
`verify_phone`, то есть только когда логин И телефон угаданы верно. Два запроса
подряд превращают ответ в оракул: «этот номер принадлежит этому логину».

Цена: телефон сотрудника + логин — половина того, что нужно для социальной
инженерии со сбросом пароля; регистрация в сервисе открыта всем, кто знает
адрес (см. CLAUDE.md, «Особенности»).
"""
from __future__ import annotations

from conftest import register


PHONE = "+7 999 000-00-00"


def _recover(client, username, phone):
    return client.post("/api/auth/recover/request",
                       json={"username": username, "phone": phone})


class TestRecoveryOracle:
    def test_answer_is_identical_for_right_and_wrong_phone(self, client):
        register(client, username="alice", phone=PHONE)

        # Верная пара: два запроса подряд — второй попадает в cooldown.
        _recover(client, "alice", PHONE)
        right = _recover(client, "alice", PHONE).json()

        # Неверная пара: тот же ритм запросов.
        _recover(client, "alice", "+7 999 111-11-11")
        wrong = _recover(client, "alice", "+7 999 111-11-11").json()

        assert right == wrong, (
            "ответ различается: по нему видно, что логин+телефон угаданы "
            f"верно ({right} против {wrong})")

    def test_unknown_login_is_indistinguishable_too(self, client):
        register(client, username="alice", phone=PHONE)

        _recover(client, "alice", PHONE)
        known = _recover(client, "alice", PHONE).json()

        _recover(client, "nosuchuser", PHONE)
        unknown = _recover(client, "nosuchuser", PHONE).json()

        assert known == unknown, (
            f"существование логина видно по ответу: {known} против {unknown}")
