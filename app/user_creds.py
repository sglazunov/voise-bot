"""Per-user LLM provider API keys, encrypted at rest under each user's key.

Every login keeps its own keys (Groq/Claude/Gemini/YandexGPT/GigaChat): one
user's key is never visible or usable by another. A provider may hold SEVERAL
keys — the LLM layer rotates to the next one when the current key hits its rate
limit, so long protocol generation isn't interrupted. Server env keys (if an
admin set any) act as a shared fallback, but a user's own keys always win.

On disk: {provider: [{"key": <enc>, "extra": <enc>}, ...]}  (list per provider).
Old single-key format {provider: {"key","extra"}} is auto-migrated on read.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import db, security

# Providers that authenticate with an API key (Ollama is keyless).
KEY_PROVIDERS = ("anthropic", "groq", "gemini", "yandex", "gigachat")


def _path(user: str) -> Path:
    return security.user_dir(user) / "llm_keys.json"


def _read_raw(user: str) -> dict:
    if db.enabled():
        raw = db.creds_load(user)
    else:
        p = _path(user)
        if not p.exists():
            return {}
        try:
            raw = json.loads(p.read_text(encoding="utf-8")) or {}
        except (ValueError, OSError):
            return {}
    # Normalise every provider's value to a LIST of {key,extra} entries.
    out = {}
    for prov, val in raw.items():
        if isinstance(val, list):
            out[prov] = [e for e in val if isinstance(e, dict) and e.get("key")]
        elif isinstance(val, dict) and val.get("key"):
            out[prov] = [val]           # migrate old single-key format
    return out


def _write(user: str, raw: dict) -> None:
    if db.enabled():
        db.creds_save(user, raw)
        return
    p = _path(user)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def load(user: str) -> dict:
    """{provider: [{'key':.., 'extra':..}, ...]} with values DECRYPTED. Keys are
    shared per TEAM (stored/encrypted under the team-admin's login)."""
    user = security.team_of(user)
    out = {}
    for prov, entries in _read_raw(user).items():
        dec = []
        for e in entries:
            dec.append({"key": security.decrypt_secret(user, e.get("key", "")),
                        "extra": security.decrypt_secret(user, e.get("extra", ""))})
        if dec:
            out[prov] = dec
    return out


def add(user: str, provider: str, key: str, extra: str = "") -> None:
    """Append a key to the TEAM's `provider` pool (skips exact duplicates)."""
    user = security.team_of(user)
    raw = _read_raw(user)
    entries = raw.get(provider) or []
    for e in entries:  # de-dupe by decrypted key
        if security.decrypt_secret(user, e.get("key", "")) == key:
            e["extra"] = security.encrypt_secret(user, extra or "")
            _write(user, raw)
            return
    entries.append({"key": security.encrypt_secret(user, key or ""),
                    "extra": security.encrypt_secret(user, extra or "")})
    raw[provider] = entries
    _write(user, raw)


def remove_at(user: str, provider: str, index: int) -> None:
    user = security.team_of(user)
    raw = _read_raw(user)
    entries = raw.get(provider) or []
    if 0 <= index < len(entries):
        entries.pop(index)
        if entries:
            raw[provider] = entries
        else:
            raw.pop(provider, None)
        _write(user, raw)


def clear(user: str, provider: str) -> None:
    user = security.team_of(user)
    raw = _read_raw(user)
    if raw.pop(provider, None) is not None:
        _write(user, raw)


def counts(user: str) -> dict:
    """{provider: how many keys the TEAM has} — for the UI (no secrets)."""
    user = security.team_of(user)
    raw = _read_raw(user)
    return {p: len(raw.get(p) or []) for p in KEY_PROVIDERS}
