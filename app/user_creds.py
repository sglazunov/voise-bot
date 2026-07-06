"""Per-user LLM provider API keys, encrypted at rest under each user's key.

Every login keeps its own keys (Groq/Claude/Gemini/YandexGPT/GigaChat): one
user's key is never visible or usable by another. Server env keys (if an admin
set any) act as a shared fallback, but a user's own key always wins.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import security

# Providers that authenticate with an API key (Ollama is keyless).
KEY_PROVIDERS = ("anthropic", "groq", "gemini", "yandex", "gigachat")


def _path(user: str) -> Path:
    return security.user_dir(user) / "llm_keys.json"


def _read_raw(user: str) -> dict:
    p = _path(user)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")) or {}
        except (ValueError, OSError):
            return {}
    return {}


def _write(user: str, raw: dict) -> None:
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
    """{provider: {'key':.., 'extra':..}} with values DECRYPTED (in memory only)."""
    out = {}
    for prov, rec in _read_raw(user).items():
        if isinstance(rec, dict):
            out[prov] = {"key": security.decrypt_secret(user, rec.get("key", "")),
                         "extra": security.decrypt_secret(user, rec.get("extra", ""))}
    return out


def save(user: str, provider: str, key: str, extra: str = "") -> None:
    raw = _read_raw(user)
    raw[provider] = {"key": security.encrypt_secret(user, key or ""),
                     "extra": security.encrypt_secret(user, extra or "")}
    _write(user, raw)


def clear(user: str, provider: str) -> None:
    raw = _read_raw(user)
    if raw.pop(provider, None) is not None:
        _write(user, raw)


def present(user: str) -> dict:
    """{provider: bool} — which providers this user has a key for (for the UI)."""
    raw = _read_raw(user)
    return {p: bool((raw.get(p) or {}).get("key")) for p in KEY_PROVIDERS}
