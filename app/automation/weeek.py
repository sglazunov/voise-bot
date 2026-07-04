"""Minimal Weeek (api.weeek.net) client for discovering meeting tasks.

We only need to *read* tasks: find the ones that carry a Telemost link and work
out when the meeting starts, so the scheduler can record them. Writing is limited
to an optional comment posted back after the protocol is built.

The exact set of date/time fields Weeek returns varies between workspaces, so the
parsing here is deliberately tolerant: it scans several candidate field names and
takes the first that yields a valid datetime. Use `probe_task()` against a real
task to see the raw JSON and pin field names if needed.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from typing import Any

API_BASE = "https://api.weeek.net/public/v1"

# Telemost links look like https://telemost.yandex.ru/j/1234567890123456.
# Stop at whitespace, quotes or angle brackets so we cleanly extract the URL even
# when Weeek stores the description as HTML (e.g. <a href="...">текст</a>).
_TELEMOST_RE = re.compile(
    r"https?://telemost\.yandex\.(?:ru|com)/[^\s\"'<>)\]]+", re.IGNORECASE)

# Candidate fields that may hold the meeting moment, richest first.
# Confirmed against a real Weeek task: dueDate is ISO "YYYY-MM-DD", `date` is
# localized "DD.MM.YYYY", and the time (when set) lives in `time`/`timeStart`.
_DATETIME_FIELDS = ("dueDateTime", "startDateTime", "dateTime", "datetime")
_DATE_FIELDS = ("dueDate", "startDate", "date", "dateStart", "day")
_TIME_FIELDS = ("time", "timeStart", "startTime", "dueTime")


class WeeekError(RuntimeError):
    """Weeek API returned an error or unreachable response."""


@dataclass
class Meeting:
    task_id: Any
    title: str
    url: str                       # the Telemost link
    start: datetime | None         # meeting start (tz-aware, UTC) if known
    project_id: Any = None
    raw: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _request(method: str, path: str, token: str,
             params: dict | None = None, body: dict | None = None,
             timeout: int = 30) -> dict:
    if not token:
        raise WeeekError("Не задан токен Weeek.")
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None})
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if e.code in (401, 403):
            raise WeeekError(
                f"Weeek отклонил токен ({e.code}). Проверьте токен в настройках "
                f"воркспейса. {detail}") from e
        raise WeeekError(f"Weeek API {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise WeeekError(f"Не удалось подключиться к Weeek: {e.reason}") from e
    except (TimeoutError, OSError) as e:
        raise WeeekError(f"Таймаут/сетевая ошибка Weeek: {e}") from e
    try:
        out = json.loads(payload)
    except ValueError as e:
        raise WeeekError(f"Weeek вернул не-JSON: {payload[:200]}") from e
    if isinstance(out, dict) and out.get("success") is False:
        raise WeeekError(f"Weeek: {out.get('message') or out}")
    return out


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def list_tasks(token: str, project_id: Any = None,
               extra_params: dict | None = None) -> list[dict]:
    """Return raw task dicts. `project_id` narrows to one project if given."""
    params = {"perPage": 100}
    if project_id is not None:
        params["projectId"] = project_id
    if extra_params:
        params.update(extra_params)
    out = _request("GET", "/tm/tasks", token, params=params)
    tasks = out.get("tasks") if isinstance(out, dict) else None
    if tasks is None and isinstance(out, list):
        tasks = out
    return tasks or []


def list_projects(token: str) -> list[dict]:
    """Return the workspace's projects as [{id, name}], so the user can pick the
    `projectId` to scope recording to one project."""
    out = _request("GET", "/tm/projects", token, params={"perPage": 100})
    projects = out.get("projects") if isinstance(out, dict) else None
    if projects is None and isinstance(out, list):
        projects = out
    result = []
    for p in projects or []:
        if isinstance(p, dict):
            result.append({"id": p.get("id"),
                           "name": p.get("name") or p.get("title") or f"Проект {p.get('id')}"})
    return result


def get_task(token: str, task_id: Any) -> dict:
    out = _request("GET", f"/tm/tasks/{task_id}", token)
    if isinstance(out, dict) and "task" in out:
        return out["task"] or {}
    return out if isinstance(out, dict) else {}


def probe_task(token: str, task_id: Any) -> dict:
    """Return the raw task JSON for inspection (used to pin field names)."""
    return get_task(token, task_id)


# --------------------------------------------------------------------------- #
# Parsing helpers (unit-testable, no network)
# --------------------------------------------------------------------------- #
def extract_telemost(*texts: str | None) -> str | None:
    """Return the first Telemost link found across the given text blobs."""
    for t in texts:
        if not t:
            continue
        m = _TELEMOST_RE.search(str(t))
        if m:
            # Trim trailing punctuation/markup that often clings to URLs.
            return m.group(0).rstrip(').,>"\'»')
    return None


_STRPTIME_FORMATS = (
    "%d.%m.%YT%H:%M:%S", "%d.%m.%YT%H:%M", "%d.%m.%Y",  # localized DD.MM.YYYY
)


def _parse_dt(value: Any, local_tz: tzinfo = timezone.utc) -> datetime | None:
    """Parse a Weeek date/time string. Naive values (no offset) are treated as
    `local_tz` — Weeek stores meeting times in the workspace's local zone."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=local_tz)
    except ValueError:
        pass
    for fmt in _STRPTIME_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=local_tz)
        except ValueError:
            continue
    return None


def parse_start(task: dict, local_tz: tzinfo = timezone.utc) -> datetime | None:
    """Best-effort meeting start time from a task, tz-aware in UTC.

    `local_tz` is the workspace timezone used for naive Weeek values (e.g.
    Europe/Moscow). Returns None when no usable date is present."""
    for f in _DATETIME_FIELDS:
        dt = _parse_dt(task.get(f), local_tz)
        if dt:
            return dt.astimezone(timezone.utc)
    # date + optional time split across two fields
    date_val = next((task.get(f) for f in _DATE_FIELDS if task.get(f)), None)
    if date_val:
        time_val = next((task.get(f) for f in _TIME_FIELDS if task.get(f)), None)
        combined = f"{date_val}T{time_val}" if time_val else str(date_val)
        dt = _parse_dt(combined, local_tz)
        if dt:
            return dt.astimezone(timezone.utc)
    return None


def _value_as_text(value: Any) -> str:
    """Flatten a custom-field value (str / dict / list) to a searchable string."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        # link-type fields may nest the URL under url/value/link
        for k in ("url", "value", "link", "href"):
            if isinstance(value.get(k), str):
                return value[k]
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return " ".join(_value_as_text(v) for v in value)
    return ""


def _customfield_values(task: dict) -> list[str]:
    """Searchable values of a task's custom fields, link-type fields first.

    A task can have many custom fields (ответственный, документы, заметки …);
    we return them all so the Telemost-specific regex can pick the right one,
    but put `type: "link"` fields up front so a clean Telemost link wins over a
    link that merely appears inside some free-text field."""
    links, others = [], []
    for cf in task.get("customFields") or []:
        if not isinstance(cf, dict):
            continue
        text = _value_as_text(cf.get("value"))
        if not text:
            continue
        (links if cf.get("type") == "link" else others).append(text)
    return links + others


def task_to_meeting(task: dict, local_tz: tzinfo = timezone.utc) -> Meeting | None:
    """Convert a raw task to a Meeting if it carries a Telemost link.

    The link may live in a custom field (preferred — link-type fields first),
    the description, or the title. The Telemost-specific regex ensures only a
    telemost.yandex.ru URL is picked, ignoring any other links on the task."""
    url = extract_telemost(
        *_customfield_values(task),
        task.get("description"), task.get("title"), task.get("name"),
    )
    if not url:
        return None
    proj = task.get("projectId") or task.get("project_id")
    if proj is None and isinstance(task.get("project"), dict):
        proj = task["project"].get("id")
    return Meeting(
        task_id=task.get("id"),
        title=task.get("title") or task.get("name") or f"Задача {task.get('id')}",
        url=url,
        start=parse_start(task, local_tz),
        project_id=proj,
        raw=task,
    )


def upcoming_meetings(token: str, project_id: Any = None,
                      local_tz: tzinfo = timezone.utc) -> list[Meeting]:
    """Tasks with a Telemost link, as Meetings. When `project_id` is set, only
    that project's meetings are returned — enforced BOTH server-side (the API
    filter) and client-side (belt-and-suspenders, in case the API ignores it)."""
    meetings = []
    for task in list_tasks(token, project_id=project_id):
        m = task_to_meeting(task, local_tz)
        if m:
            meetings.append(m)
    pid = str(project_id).strip() if project_id not in (None, "", 0) else None
    if pid and any(m.project_id is not None for m in meetings):
        # Only filter when we can actually read project ids, so an unexpected
        # field name can't silently drop every meeting.
        meetings = [m for m in meetings if str(m.project_id) == pid]
    meetings.sort(key=lambda m: (m.start is None, m.start or datetime.max.replace(
        tzinfo=timezone.utc)))
    return meetings


# --------------------------------------------------------------------------- #
# Writes (optional post-back)
# --------------------------------------------------------------------------- #
def add_comment(token: str, task_id: Any, text: str) -> bool:
    """Post a comment on a task. Best-effort: returns False on failure."""
    for path in (f"/tm/tasks/{task_id}/comments", f"/tm/task-comments"):
        try:
            body = {"text": text}
            if path.endswith("task-comments"):
                body["taskId"] = task_id
            _request("POST", path, token, body=body)
            return True
        except WeeekError:
            continue
    return False


def _find_custom_field_id(task: dict, field_name: str):
    """Id of a task's custom field by (case-insensitive, then partial) name."""
    fields = task.get("customFields") or []
    name = field_name.strip().lower()
    for cf in fields:
        if isinstance(cf, dict) and str(cf.get("name", "")).strip().lower() == name:
            return cf.get("id")
    for cf in fields:  # partial match fallback
        if isinstance(cf, dict) and name in str(cf.get("name", "")).lower():
            return cf.get("id")
    return None


def set_custom_field(token: str, task_id: Any, field_name: str, value: str) -> dict:
    """Write `value` into the task's custom field named `field_name` (e.g. a link
    field «Видео встречи»). Best-effort: tries the known Weeek write shapes and
    returns {ok, ...} with the API error for diagnosis if all fail."""
    try:
        task = get_task(token, task_id)
    except WeeekError as e:
        return {"ok": False, "error": f"Не прочитал задачу: {e}"}
    fid = _find_custom_field_id(task, field_name)
    if fid is None:
        return {"ok": False, "error": f"Кастом-поле «{field_name}» не найдено в задаче."}
    # Verified against the live Weeek API: update the task, passing customFields
    # as a {fieldId: value} MAP, and the value as a plain STRING. (The list shape
    # [{"id","value"}] returns 200 but is silently ignored; a non-string 422s.)
    try:
        _request("PUT", f"/tm/tasks/{task_id}", token,
                 body={"customFields": {str(fid): str(value)}})
        return {"ok": True, "field_id": fid}
    except WeeekError as e:
        return {"ok": False, "error": str(e), "field_id": fid}
