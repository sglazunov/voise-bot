"""Telegram-бот для доставки одноразовых кодов восстановления пароля.

Канал включается одной переменной — `VTX_TELEGRAM_BOT_TOKEN` (токен от
@BotFather). Без неё модуль молчит: `configured()` = False, коды по-прежнему
идут по SMS (`app/sms.py`).

Как человек привязывает Telegram к аккаунту:
  1. в профиле жмёт «Привязать Telegram» (нужен текущий пароль — как при смене
     телефона: угнанная сессия не должна перевести восстановление на чужой
     Telegram) — сервер выдаёт одноразовый код привязки `new_link_code()`;
  2. открывает ссылку `https://t.me/<бот>?start=<код>` (или пишет боту
     `/start <код>` руками);
  3. бот получает сообщение через long-polling `getUpdates` (поток
     `start_polling()`), `consume_link_code()` находит логин по коду, chat_id
     сохраняется в учётке (`security.set_telegram`), человеку уходит ответ.
Написать человеку первым бот не может: Telegram разрешает боту писать только
тем, кто сам ему написал, — поэтому без шага 2 привязка невозможна.

`send()` НИКОГДА не бросает: возвращает (ok, detail) и пишет в лог. Как и в
`sms`, вызывающий не должен показывать клиенту, дошло ли сообщение
(анти-энумерация логинов при восстановлении).

⚠️ Один поток опроса на весь сервер: `getUpdates` с двух процессов даёт 409 у
Telegram. Приложение работает одним uvicorn-процессом (см. docker/run.sh) —
это инвариант, на который здесь тоже опираемся.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request

from . import logs

log = logs.get("vtx.telegram")

# Отправленные сообщения — крошечный журнал для тестов и режима без токена.
# Наружу по HTTP не отдаётся.
sent_messages: list[dict] = []
_MAX_KEPT = 50

LINK_CODE_TTL = int(os.getenv("VTX_TELEGRAM_LINK_TTL_SEC", "600"))

_LOCK = threading.RLock()
# код привязки → {"user": логин, "exp": срок}
_LINK_CODES: dict[str, dict] = {}
_ME: dict | None = None            # кэш getMe (имя бота для ссылки t.me)
_POLL_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_OFFSET = 0


def token() -> str:
    return (os.getenv("VTX_TELEGRAM_BOT_TOKEN") or "").strip()


def configured() -> bool:
    return bool(token())


def _record(chat_id: str, text: str) -> None:
    sent_messages.append({"to": str(chat_id), "text": text})
    if len(sent_messages) > _MAX_KEPT:
        del sent_messages[: len(sent_messages) - _MAX_KEPT]


def _api(method: str, params: dict | None = None, timeout: int = 15) -> dict:
    """Один вызов Bot API. Возвращает `result` ответа; бросает RuntimeError с
    внятным текстом (без токена в нём — он часть адреса, и в лог попасть не
    должен). Подменяется в тестах."""
    tok = token()
    if not tok:
        raise RuntimeError("VTX_TELEGRAM_BOT_TOKEN не задан")
    url = f"https://api.telegram.org/bot{tok}/{method}"
    body = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            data = {}
        raise RuntimeError(f"HTTP {e.code}: {data.get('description') or e.reason}") from None
    except Exception as e:  # сеть, разбор — наверх одной строкой
        raise RuntimeError(f"сеть: {e}") from None
    if not data.get("ok"):
        raise RuntimeError(f"{data.get('error_code')}: {data.get('description')}")
    return data.get("result")


def bot_username() -> str:
    """Имя бота (@без собаки) для ссылки t.me — из getMe, кэшируется."""
    global _ME
    if not configured():
        return ""
    with _LOCK:
        if _ME is None:
            try:
                _ME = _api("getMe") or {}
            except RuntimeError as e:
                log.warning("getMe не ответил: %s", e)
                return ""
        return str(_ME.get("username") or "")


def send(chat_id: str | int, text: str) -> tuple[bool, str]:
    """Отправить `text` в чат. Никогда не бросает."""
    if not configured():
        return False, "VTX_TELEGRAM_BOT_TOKEN не задан"
    try:
        _api("sendMessage", {"chat_id": str(chat_id), "text": text,
                             "disable_web_page_preview": True})
        ok, detail = True, "sent"
    except RuntimeError as e:
        ok, detail = False, str(e)
    _record(str(chat_id), text)
    if not ok:
        log.warning("сообщение в чат %s не доставлено: %s", chat_id, detail)
    return ok, detail


# --------------------------------------------------------------------------- #
# Привязка аккаунта: одноразовый код → /start <код> → chat_id в учётке
# --------------------------------------------------------------------------- #
def _purge_codes(now: float) -> None:
    for code in [c for c, v in _LINK_CODES.items() if v["exp"] <= now]:
        _LINK_CODES.pop(code, None)


def new_link_code(username: str) -> str:
    """Выдать код привязки для логина. Прежний код того же логина гасится —
    живёт только последний."""
    now = time.time()
    code = secrets.token_urlsafe(9)   # 12 символов, годится в ?start=
    with _LOCK:
        _purge_codes(now)
        for c in [c for c, v in _LINK_CODES.items() if v["user"] == username]:
            _LINK_CODES.pop(c, None)
        _LINK_CODES[code] = {"user": username, "exp": now + LINK_CODE_TTL}
    return code


def consume_link_code(code: str) -> str | None:
    """Логин по коду привязки; код сгорает. None — неизвестен или просрочен."""
    now = time.time()
    with _LOCK:
        _purge_codes(now)
        rec = _LINK_CODES.pop((code or "").strip(), None)
    return rec["user"] if rec else None


def link_url(code: str) -> str:
    name = bot_username()
    return f"https://t.me/{name}?start={code}" if name else ""


def _sender_name(msg: dict) -> str:
    frm = msg.get("from") or {}
    if frm.get("username"):
        return "@" + str(frm["username"])
    return " ".join(p for p in (frm.get("first_name"), frm.get("last_name")) if p)


def handle_update(upd: dict) -> None:
    """Разобрать одно обновление. Интересует только «/start <код>» (deep link
    `?start=` приходит именно так) или просто код текстом."""
    msg = upd.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not text or chat_id is None:
        return
    parts = text.split(maxsplit=1)
    if parts[0].startswith("/start"):
        code = parts[1].strip() if len(parts) > 1 else ""
    elif parts[0].startswith("/"):
        return
    else:
        code = text
    if not code:
        send(chat_id, "Это бот восстановления пароля MeetFlowAI. Чтобы привязать "
                      "Telegram к аккаунту, откройте профиль на сайте, нажмите "
                      "«Привязать Telegram» и перейдите по ссылке.")
        return
    user = consume_link_code(code)
    if not user:
        send(chat_id, "Код привязки не подошёл или устарел. Запросите новый в "
                      "профиле на сайте.")
        return
    from . import security   # поздний импорт: security не должен тянуть telegram
    security.set_telegram(user, str(chat_id), _sender_name(msg))
    log.info("Telegram привязан к аккаунту %s (chat %s)", user, chat_id)
    send(chat_id, f"Готово: Telegram привязан к аккаунту «{user}». Сюда будут "
                  f"приходить коды восстановления пароля.")


def poll_once(timeout: int = 25) -> int:
    """Один запрос getUpdates (long-polling). Возвращает число обработанных."""
    global _OFFSET
    updates = _api("getUpdates", {"offset": _OFFSET, "timeout": timeout,
                                  "allowed_updates": ["message"]},
                   timeout=timeout + 10) or []
    n = 0
    for upd in updates:
        _OFFSET = max(_OFFSET, int(upd.get("update_id", 0)) + 1)
        try:
            handle_update(upd)
            n += 1
        except Exception:
            log.warning("обновление Telegram не разобрано", exc_info=True)
    return n


def _poll_loop() -> None:
    log.info("опрос Telegram запущен (бот @%s)", bot_username() or "?")
    backoff = 5
    while not _STOP.is_set():
        try:
            poll_once()
            backoff = 5
        except RuntimeError as e:
            log.warning("getUpdates: %s — повтор через %d с", e, backoff)
            if _STOP.wait(backoff):
                break
            backoff = min(backoff * 2, 300)


def start_polling() -> bool:
    """Поднять поток опроса, если задан токен. Повторный вызов безвреден."""
    global _POLL_THREAD
    if not configured():
        return False
    with _LOCK:
        if _POLL_THREAD and _POLL_THREAD.is_alive():
            return True
        _STOP.clear()
        _POLL_THREAD = threading.Thread(target=_poll_loop, name="telegram-poll",
                                        daemon=True)
        _POLL_THREAD.start()
    return True


def stop_polling() -> None:
    _STOP.set()
