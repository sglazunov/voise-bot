"""Persistent, user-maintained CONTEXT for the protocol AI.

The problem it solves (raised by the director): reading protocols of meetings she
wasn't in, the AI confused participant roles, project names and what the projects
are actually about. So each user keeps a standing context — a small knowledge
base — that is injected into EVERY protocol generation:

    { "global":   "who's who, roles, terminology…"   (applies to all meetings),
      "projects": [ {"name": "Проект X", "text": "…"} ]  (added when relevant) }

`global` is always included. A project block is included when its name appears in
the "hint" (the meeting title for the bot, or the file name / picked project for
a manual job) — so a meeting about «Проект X» automatically gets that project's
context, without the user tagging anything.

Stored unencrypted per user (it's not a secret, just private): a name/role list
is not sensitive the way a token is, and keeping it plain makes it trivially
editable/inspectable on disk.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import db, security

_MAX_GLOBAL = 20_000       # generous, but bounded so it can't blow up the prompt
_MAX_PROJECT = 10_000
_MAX_PROJECTS = 50


def _path(user: str) -> Path:
    return security.user_dir(user) / "ai_context.json"


def load(user: str) -> dict:
    """Return {'global': str, 'projects': [{'name','text'}, ...]} — shared per TEAM."""
    user = security.team_of(user)
    if db.enabled():
        raw = db.aicontext_load(user) or {}
    else:
        try:
            raw = json.loads(_path(user).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
    projects = []
    for p in (raw.get("projects") or []):
        if isinstance(p, dict) and (p.get("name") or "").strip():
            projects.append({"name": str(p.get("name", "")).strip()[:120],
                             "text": str(p.get("text", "")).strip()[:_MAX_PROJECT]})
    return {"global": str(raw.get("global", "")).strip()[:_MAX_GLOBAL],
            "projects": projects[:_MAX_PROJECTS]}


def save(user: str, data: dict) -> dict:
    """Persist the TEAM's context (sanitised) and return the stored value."""
    user = security.team_of(user)
    clean = {
        "global": str((data or {}).get("global", "")).strip()[:_MAX_GLOBAL],
        "projects": [],
    }
    for p in ((data or {}).get("projects") or [])[:_MAX_PROJECTS]:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name", "")).strip()[:120]
        text = str(p.get("text", "")).strip()[:_MAX_PROJECT]
        if name:
            clean["projects"].append({"name": name, "text": text})
    if db.enabled():
        db.aicontext_save(user, clean)
        return clean
    p = _path(user)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return clean


def project_names(user: str) -> list[str]:
    return [p["name"] for p in load(user)["projects"]]


# Name-like tokens for the Whisper initial_prompt (Д3): capitalised pairs are
# almost always «Имя Фамилия»; a single capitalised word only counts when it
# sits MID-sentence (preceded by a lowercase letter) — that excludes ordinary
# sentence-start capitals without needing a stop-word list.
_NAME_PAIR = re.compile(r"\b([А-ЯЁA-Z][а-яёa-z]{2,})\s+([А-ЯЁA-Z][а-яёa-z]{2,})\b")
_NAME_MID = re.compile(r"[а-яёa-z][,;:—-]?\s+([А-ЯЁA-Z][а-яёa-z]{2,})\b")


def known_names(user: str, limit: int = 30) -> list[str]:
    """Participant names mined from the team's standing context text."""
    data = load(user)
    text = " ".join([data["global"]] + [p["text"] for p in data["projects"]])
    names: list[str] = []
    seen: set[str] = set()

    def add(n: str) -> None:
        k = n.lower()
        if k not in seen:
            seen.add(k)
            names.append(n)

    for m in _NAME_PAIR.finditer(text):
        add(f"{m.group(1)} {m.group(2)}")
    for m in _NAME_MID.finditer(text):
        add(m.group(1))
    return names[:limit]


def block_for(user: str, hint: str = "") -> str:
    """The context block to prepend to a protocol's analysis input.

    Always includes the global context; includes a project's context when its
    name occurs in `hint` (meeting title / file name / explicitly picked project).
    Returns "" when there's nothing to add.
    """
    data = load(user)
    parts: list[tuple[str, str]] = []
    if data["global"]:
        parts.append(("Общее", data["global"]))
    hint_low = (hint or "").lower()
    for p in data["projects"]:
        if p["text"] and p["name"].lower() in hint_low:
            parts.append((p["name"], p["text"]))
    if not parts:
        return ""
    out = ["=== ПОСТОЯННЫЙ КОНТЕКСТ (участники, роли, суть проектов — заданы "
           "пользователем; используй для понимания, но НЕ выдумывай факты встречи) ==="]
    for title, text in parts:
        out.append(f"[{title}]\n{text}")
    return "\n".join(out)
