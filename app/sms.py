"""Outbound SMS — used only to deliver one-time password-recovery codes.

Pluggable by env `VTX_SMS_PROVIDER`:
  * "smsru"    — SMS.ru HTTP API. Needs `VTX_SMSRU_API_ID` (your api_id from
                 https://sms.ru/#api). Optional `VTX_SMSRU_FROM` (approved sender
                 name), `VTX_SMSRU_TEST=1` to use SMS.ru's test mode (no real
                 send, no charge). This is the default provider once api_id is set.
  * "console"  — dev/fallback: no external call, the message is written to the
                 server log so recovery still works locally or before a gateway
                 is configured. This is the default when no api_id is present.

`send()` never raises; it returns (ok, detail) and logs failures. The caller
must NOT surface delivery success/failure to the client (anti-enumeration).
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
import urllib.request

log = logging.getLogger("vtx.sms")

# Recently sent messages — kept tiny, for the console provider and for tests to
# inspect. Never exposed over HTTP.
sent_messages: list[dict] = []
_MAX_KEPT = 50


def _record(to: str, text: str, provider: str) -> None:
    sent_messages.append({"to": to, "text": text, "provider": provider})
    if len(sent_messages) > _MAX_KEPT:
        del sent_messages[: len(sent_messages) - _MAX_KEPT]


def provider() -> str:
    """The active provider id, honouring explicit override then auto-detection."""
    explicit = (os.getenv("VTX_SMS_PROVIDER") or "").strip().lower()
    if explicit:
        return explicit
    return "smsru" if os.getenv("VTX_SMSRU_API_ID") else "console"


def send(phone: str, text: str) -> tuple[bool, str]:
    """Send `text` to `phone` (digits, with or without leading +). Best-effort."""
    prov = provider()
    if prov == "smsru":
        ok, detail = _send_smsru(phone, text)
    else:
        ok, detail = True, "console"
        log.info("[SMS:console] → %s : %s", phone, text)
    _record(phone, text, prov)
    if not ok:
        log.warning("[SMS:%s] delivery failed for %s: %s", prov, phone, detail)
    return ok, detail


def _send_smsru(phone: str, text: str) -> tuple[bool, str]:
    api_id = os.getenv("VTX_SMSRU_API_ID", "").strip()
    if not api_id:
        return False, "VTX_SMSRU_API_ID не задан"
    to = re.sub(r"\D", "", phone)
    params = {"api_id": api_id, "to": to, "msg": text, "json": 1}
    if os.getenv("VTX_SMSRU_FROM"):
        params["from"] = os.getenv("VTX_SMSRU_FROM")
    if os.getenv("VTX_SMSRU_TEST") == "1":
        params["test"] = 1
    url = "https://sms.ru/sms/send?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:  # network / parse — never propagate
        return False, f"сеть: {e}"
    # SMS.ru: top-level status "OK" + per-recipient status inside "sms".
    if data.get("status") != "OK":
        return False, f"{data.get('status_code')}: {data.get('status_text')}"
    sms = (data.get("sms") or {}).get(to) or {}
    if sms.get("status") != "OK":
        return False, f"{sms.get('status_code')}: {sms.get('status_text')}"
    return True, "sent"
