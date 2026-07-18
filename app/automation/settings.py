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
from pathlib import Path
from typing import Any

from .. import config, db, security

_LOCK = threading.Lock()

# Secret fields are stored ENCRYPTED on disk (per-user key) and decrypted only in
# memory. Dotted paths reach into the nested cloud sub-dicts.
_SECRET_PATHS = ("weeek_token", "yandex_disk.token", "yandex_disk.read_token",
                 "gdrive.client_secret", "gdrive.refresh_token")


def _path(user: str) -> Path:
    """Per-user settings file. Each login keeps its own tokens, isolated."""
    return security.user_dir(user) / "automation.json"


def _get_path(d: dict, dotted: str):
    cur = d
    parts = dotted.split(".")
    for p in parts[:-1]:
        cur = cur.get(p) if isinstance(cur, dict) else None
        if not isinstance(cur, dict):
            return None, None, None
    return cur, parts[-1], (cur.get(parts[-1]) if isinstance(cur, dict) else None)


def _transform_secrets(user: str, data: dict, fn) -> None:
    """Apply `fn(user, value)` in place to every secret path that holds a str."""
    for dotted in _SECRET_PATHS:
        parent, key, val = _get_path(data, dotted)
        if parent is not None and isinstance(val, str) and val:
            parent[key] = fn(user, val)

# Default shape. Anything missing from the on-disk file falls back to these.
_DEFAULTS: dict[str, Any] = {
    "enabled": False,                 # master switch for the scheduler
    # --- Weeek task tracker ---
    "weeek_token": "",
    "weeek_project_id": None,         # optional: limit polling to one project
    "timezone": "Europe/Moscow",      # workspace tz for naive Weeek date/times
    # --- where to put finished recordings ---
    "cloud": "local",                 # "local" | "gdrive" | "yandex_disk"
    "yandex_disk": {"token": "", "read_token": "", "folder": "disk:/Телемост-записи"},
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
    "audio_device": "",               # PulseAudio source to record (default: meet<slot>.monitor)
    "capture_video": True,            # record the screen too (slides/screen-share)
    "join_timeout_sec": 60,           # how long to wait to get into the call
    "end_when_alone_sec": 90,         # stop after the room sits at/below the threshold this long
    "min_participants": 1,            # stop when total in room (incl. bot) drops to <= this
    "chat_stop_word": "стоп",         # message in the Telemost chat that ends the
                                      # recording (bot leaves); "" disables
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
    "identify_speakers": True,        # read WHO spoke from the video (active-tile name)
    "analyze_provider": "auto",       # which LLM builds the protocol
    "strict_verify": True,            # grounding pass: every task/decision needs a
                                      # verbatim quote; unverified ones get flagged (Д5)
    "live_transcribe": True,          # Д10: transcribe the growing recording every N min
    "live_interval_min": 5,           # ...this often (the page shows text mid-meeting)
    # --- Weeek checkbox that decides whether to record this meeting ---
    "weeek_use_record_field": True,   # let a Weeek toggle decide record / skip
    "weeek_record_field": "Запись встречи",  # name of that checkbox custom field
    "post_back_to_weeek": True,       # attach protocol link as a task comment
    # --- write the recording link into a Weeek custom field of the task ---
    "weeek_set_video_field": True,    # put the cloud link into a custom field
    "weeek_video_field": "Видео встречи",  # name of that link custom field
    # --- after the protocol (.docx) is built: upload it to the cloud ---
    "upload_protocol": True,          # send the generated protocol to the cloud too
    "protocol_folder": "disk:/Телемост-протоколы",  # SEPARATE folder for protocols
    "weeek_set_protocol_field": True,  # write its link into a Weeek custom field
    "weeek_protocol_field": "Протокол встречи",  # name of that link custom field
}


def _atomic_write(user: str, data: dict[str, Any]) -> None:
    if db.enabled():
        db.settings_save(user, data)
        return
    path = _path(user)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.replace(tmp, path)
    finally:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # unsupported FS — best effort only


def _read_raw(user: str) -> dict[str, Any]:
    """Stored dict (secrets still ENCRYPTED), merged onto defaults."""
    import copy
    data = copy.deepcopy(_DEFAULTS)
    if db.enabled():
        stored = db.settings_load(user)
        if isinstance(stored, dict):
            _deep_update(data, stored)
        return data
    path = _path(user)
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                _deep_update(data, stored)
        except (ValueError, OSError):
            pass  # corrupt file -> fall back to defaults
    return data


def _deep_update(base: dict, extra: dict) -> None:
    for k, v in extra.items():
        if k in _NESTED_KEYS and isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k].update(v)
        else:
            base[k] = v


def load(user: str) -> dict[str, Any]:
    """Return the TEAM's settings (shared across the team) with secrets DECRYPTED.
    Storage + encryption key are the team-admin's login, so members read exactly
    what the admin configured."""
    user = security.team_of(user)
    with _LOCK:
        data = _read_raw(user)
    _transform_secrets(user, data, security.decrypt_secret)
    return data


# Keys whose values are sub-dicts that should be MERGED, not replaced, so a
# partial update (e.g. just the Yandex token) keeps the rest (folder).
_NESTED_KEYS = ("yandex_disk", "gdrive")


def save(user: str, values: dict[str, Any]) -> dict[str, Any]:
    """Merge `values` (plaintext) into the TEAM's settings and persist, with
    secrets ENCRYPTED at rest under the team-admin's key. Returns the new state."""
    user = security.team_of(user)
    with _LOCK:
        # Work in plaintext: decrypt current, apply update, then re-encrypt to disk.
        data = _read_raw(user)
        _transform_secrets(user, data, security.decrypt_secret)
        for k, v in values.items():
            if k in _NESTED_KEYS and isinstance(v, dict):
                base = dict(data.get(k) or {})
                base.update({kk: vv for kk, vv in v.items() if vv is not None})
                data[k] = base
            else:
                data[k] = v
        on_disk = json.loads(json.dumps(data))  # deep copy
        _transform_secrets(user, on_disk, security.encrypt_secret)
        _atomic_write(user, on_disk)
        return data


def get(user: str, key: str, default: Any = None) -> Any:
    return load(user).get(key, default)


def redacted(user: str) -> dict[str, Any]:
    """Settings safe to send to the UI — secrets replaced with a presence flag."""
    data = load(user)
    out = dict(data)
    out["weeek_token"] = bool(data.get("weeek_token"))
    yd = dict(data.get("yandex_disk") or {})
    yd["token"] = bool(yd.get("token"))
    yd["read_token"] = bool(yd.get("read_token"))
    out["yandex_disk"] = yd
    gd = dict(data.get("gdrive") or {})
    for secret in ("client_secret", "refresh_token"):
        gd[secret] = bool(gd.get(secret))
    # client_id isn't very secret but no need to ship it back; show presence.
    gd["client_id"] = bool(gd.get("client_id"))
    out["gdrive"] = gd
    return out
