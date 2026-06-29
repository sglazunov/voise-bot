"""Persistent settings for meeting automation.

Unlike the core app (which keeps API keys in memory only), the automation runs
unattended on a 24/7 server and must remember its credentials across restarts.
So these live in a JSON file on disk: DATA_DIR/automation.json.

SECURITY NOTE: this file contains the Weeek token and cloud OAuth tokens in
plain text. Keep the data directory private (chmod 700 on the server). The file
is created with 0600 permissions where the OS supports it.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any

from .. import config

_PATH = config.DATA_DIR / "automation.json"
_LOCK = threading.Lock()

# Default shape. Anything missing from the on-disk file falls back to these.
_DEFAULTS: dict[str, Any] = {
    "enabled": False,                 # master switch for the scheduler
    # --- Weeek task tracker ---
    "weeek_token": "",
    "weeek_project_id": None,         # optional: limit polling to one project
    "timezone": "Europe/Moscow",      # workspace tz for naive Weeek date/times
    # --- where to put finished recordings ---
    "cloud": "local",                 # "local" | "gdrive" | "yandex_disk"
    "yandex_disk": {"token": "", "folder": "disk:/Телемост-записи"},
    "gdrive": {"client_id": "", "client_secret": "", "refresh_token": "",
               "folder_id": ""},
    "local_dir": "",                  # empty -> DATA_DIR/recordings
    # --- scheduler / bot behaviour ---
    "poll_interval_sec": 120,         # how often to re-read Weeek
    "lookahead_min": 2,               # join the meeting this many min early
    "max_meeting_min": 240,           # hard cap on a single recording
    "bot_join_name": "Протокол-бот",  # display name shown in Telemost
    "headless": True,
    # --- recorder (bot joins Telemost and records) ---
    # Recording = ffmpeg screen capture (full length; no Yandex 30-min browser
    # limit and no host-only restriction). The "telemost" native path was dropped.
    "record_mode": "screen",
    "auth_mode": "guest",             # "guest" (link only) | "profile" (logged in)
    "browser_profile_dir": "",        # profile dir for auth_mode=profile; "" -> DATA_DIR/browser-profile
    "ffmpeg_path": "ffmpeg",          # ffmpeg binary (PATH or absolute)
    "audio_device": "",               # Windows dshow audio device to capture (loopback/virtual cable)
    "capture_video": True,            # record the screen too (slides/screen-share)
    "join_timeout_sec": 60,           # how long to wait to get into the call
    "end_when_alone_sec": 90,         # stop after the room sits at/below the threshold this long
    "min_participants": 1,            # stop when total in room (incl. bot) drops to <= this
                                      # 1 = stop only when everyone left; 3 = ignore a small lingering tail
    # --- which meetings to auto-record (all empty = record everything) ---
    "rec_time_from": "",              # "HH:MM" local — record only meetings starting at/after
    "rec_time_to": "",                # "HH:MM" local — ...and at/before this time
    "rec_days": [],                   # weekday numbers 0=Mon..6=Sun; empty = any day
    "rec_include": "",                # keywords (comma/line); if set, the title MUST contain one
    "rec_exclude": "",                # keywords (comma/line); a title containing any is skipped
    # --- per-meeting manual choice (overrides the keyword/time filters) ---
    "rec_default_on": True,           # record meetings that have no explicit choice
                                      # True = record all (minus ones turned off);
                                      # False = record ONLY ones turned on
    "rec_decisions": {},              # {task_id(str): true=record | false=skip}
    # --- after recording: independently toggleable stages ---
    "do_transcribe": True,            # run speech recognition on the recording
    "do_protocol": True,              # build the Word protocol (needs do_transcribe)
    "ocr_screen": True,               # recognise on-screen text (slides/code) too
    "analyze_provider": "auto",       # which LLM builds the protocol
    "post_back_to_weeek": True,       # attach protocol link as a task comment
}


def _atomic_write(data: dict[str, Any]) -> None:
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.replace(tmp, _PATH)
    finally:
        try:
            os.chmod(_PATH, 0o600)
        except OSError:
            pass  # Windows / unsupported FS — best effort only


def load() -> dict[str, Any]:
    """Return the full settings dict (defaults merged with the on-disk file)."""
    with _LOCK:
        data = dict(_DEFAULTS)
        if _PATH.exists():
            try:
                stored = json.loads(_PATH.read_text(encoding="utf-8"))
                if isinstance(stored, dict):
                    data.update(stored)
            except (ValueError, OSError):
                pass  # corrupt file -> fall back to defaults
        return data


# Keys whose values are sub-dicts that should be MERGED, not replaced, so a
# partial update (e.g. just the Yandex token) keeps the rest (folder).
_NESTED_KEYS = ("yandex_disk", "gdrive")


def save(values: dict[str, Any]) -> dict[str, Any]:
    """Merge `values` into the stored settings and persist. Returns new state."""
    with _LOCK:
        data = dict(_DEFAULTS)
        if _PATH.exists():
            try:
                stored = json.loads(_PATH.read_text(encoding="utf-8"))
                if isinstance(stored, dict):
                    data.update(stored)
            except (ValueError, OSError):
                pass
        for k, v in values.items():
            if k in _NESTED_KEYS and isinstance(v, dict):
                base = dict(data.get(k) or {})
                base.update({kk: vv for kk, vv in v.items() if vv is not None})
                data[k] = base
            else:
                data[k] = v
        _atomic_write(data)
        return data


def get(key: str, default: Any = None) -> Any:
    return load().get(key, default)


def redacted() -> dict[str, Any]:
    """Settings safe to send to the UI — secrets replaced with a presence flag."""
    data = load()
    out = dict(data)
    out["weeek_token"] = bool(data.get("weeek_token"))
    yd = dict(data.get("yandex_disk") or {})
    yd["token"] = bool(yd.get("token"))
    out["yandex_disk"] = yd
    gd = dict(data.get("gdrive") or {})
    for secret in ("client_secret", "refresh_token"):
        gd[secret] = bool(gd.get(secret))
    # client_id isn't very secret but no need to ship it back; show presence.
    gd["client_id"] = bool(gd.get("client_id"))
    out["gdrive"] = gd
    return out
