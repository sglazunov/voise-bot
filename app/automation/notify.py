"""Д15: notifications to the team's Telegram chat.

A plain bot-API call — no SDK. The bot token is stored encrypted in the team's
automation settings; both token and chat id come pre-decrypted in `cfg`
(auto_settings.load). Best-effort by design: a notification failure must never
affect the pipeline.

Setup (документируется в UI): создать бота у @BotFather → токен; добавить бота
в чат/группу; узнать chat_id (например, переслав сообщение боту @userinfobot
или через getUpdates).
"""
from __future__ import annotations

import json
import urllib.request


def send(cfg: dict, text: str) -> bool:
    token = str(cfg.get("telegram_bot_token") or "").strip()
    chat = str(cfg.get("telegram_chat_id") or "").strip()
    if not token or not chat:
        return False
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json.dumps({"chat_id": chat, "text": text[:4000],
                        "disable_web_page_preview": True}).encode("utf-8"),
            {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return bool(json.load(resp).get("ok"))
    except Exception:  # noqa: BLE001 — notifications are best-effort
        return False


def test(cfg: dict) -> dict:
    """The «проверить» button: send a test message, report the outcome."""
    if not str(cfg.get("telegram_bot_token") or "").strip():
        return {"ok": False, "error": "Не задан токен бота."}
    if not str(cfg.get("telegram_chat_id") or "").strip():
        return {"ok": False, "error": "Не задан chat_id."}
    ok = send(cfg, "✅ Уведомления MeetFlow настроены: это тестовое сообщение.")
    return {"ok": ok} if ok else {
        "ok": False, "error": "Telegram не принял сообщение — проверьте токен, "
                              "chat_id и что бот добавлен в чат."}
