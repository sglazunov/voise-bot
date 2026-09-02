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

import http.client
import json
import re
import time
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

    # Weeek sometimes truncates a big response mid-read (http.client.IncompleteRead
    # on a chunked/keep-alive connection) — which silently broke the scheduler poll
    # so the bot never joined. Force a non-keep-alive connection and retry the
    # whole request a few times on transient network/read errors.
    payload = None
    last_err: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/json")
        req.add_header("Accept-Encoding", "identity")
        req.add_header("Connection", "close")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read().decode("utf-8")
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in (401, 403):
                raise WeeekError(
                    f"Weeek отклонил токен ({e.code}). Проверьте токен в настройках "
                    f"воркспейса. {detail}") from e
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                last_err = e
                # 429: Weeek говорит, сколько ждать — слушаемся (в разумных
                # пределах), иначе три быстрых повтора только продлевают бан.
                wait = 0.7 * (attempt + 1)
                if e.code == 429:
                    try:
                        wait = min(30.0, max(wait, float(e.headers.get("Retry-After") or 0)))
                    except (TypeError, ValueError):
                        pass
                time.sleep(wait)
                continue
            raise WeeekError(f"Weeek API {e.code}: {detail}") from e
        except (http.client.IncompleteRead, urllib.error.URLError,
                TimeoutError, OSError) as e:
            last_err = e
            time.sleep(0.7 * (attempt + 1))
            continue
    if payload is None:
        raise WeeekError(f"Weeek: сеть нестабильна, ответ не получен ({last_err}).")

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
               extra_params: dict | None = None, max_tasks: int = 500) -> list[dict]:
    """Return raw task dicts. `project_id` narrows to one project if given.

    Weeek caps a page at 100 tasks, so we paginate by `offset` (up to
    `max_tasks`) — otherwise meetings past the first 100 (e.g. an older task
    RESCHEDULED into the future) are never fetched. The pages are fetched
    CONCURRENTLY: sequentially, 5 pages of a ~3 s API took ~15 s (and made the
    «Обновить» button feel dead); in parallel it's ~one round-trip."""
    per = 100
    n_pages = max(1, (int(max_tasks) + per - 1) // per)

    def fetch(offset: int) -> list[dict]:
        params: dict = {"perPage": per, "offset": offset}
        if project_id is not None:
            params["projectId"] = project_id
        if extra_params:
            params.update(extra_params)
        out = _request("GET", "/tm/tasks", token, params=params)
        if isinstance(out, dict):
            return out.get("tasks") or []
        return out if isinstance(out, list) else []

    first = fetch(0)
    if n_pages == 1 or len(first) < per:
        # Короткая первая страница = задач меньше сотни; остальные четыре
        # запроса на каждом опросе были бы впустую (и кормили 429).
        return first
    from concurrent.futures import ThreadPoolExecutor
    offsets = [i * per for i in range(1, n_pages)]
    with ThreadPoolExecutor(max_workers=min(len(offsets), 6)) as ex:
        pages = list(ex.map(fetch, offsets))
    collected: list[dict] = list(first)
    for pg in pages:                 # keep order; stop at the first short page
        collected.extend(pg)
        if len(pg) < per:
            break
    return collected


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
    """Meeting START time from a task, tz-aware in UTC.

    IMPORTANT: for meetings with a time range Weeek exposes both `startDateTime`
    (the START, UTC) and `dueDateTime` (the END, UTC). We must take the START, so
    the bot joins at the beginning — not `dueDateTime`, which would be the end.
    Single-time meetings only carry `date` + `time`.

    `local_tz` is the workspace timezone used for naive (no-offset) values.
    """
    # 1) Explicit START datetime (range meetings), full UTC timestamp. For a
    #    time RANGE Weeek exposes both startDateTime (START) and dueDateTime
    #    (END) — we must take the START. NEVER dueDateTime in this branch.
    for f in ("startDateTime", "dateTime", "datetime"):
        dt = _parse_dt(task.get(f), local_tz)
        if dt:
            return dt.astimezone(timezone.utc)
    # 2) Single-time meetings (no startDateTime): dueDateTime is the exact meeting
    #    time as a full UTC timestamp. PREFER it over the separate `date`+`time`
    #    fields — Weeek returns those in MISMATCHED zones (`date` follows UTC while
    #    `time` is the workspace-local time), so combining them is wrong, most
    #    visibly near midnight (a 00:25 meeting parsed to the previous day).
    dt = _parse_dt(task.get("dueDateTime"), local_tz)
    if dt:
        return dt.astimezone(timezone.utc)
    # 3) Last resort: a day + a local start time (only when no dueDateTime exists).
    day = next((task.get(f) for f in
                ("dateStart", "date", "startDate", "dueDate", "day") if task.get(f)), None)
    stime = next((task.get(f) for f in
                  ("timeStart", "startTime", "time") if task.get(f)), None)
    if day:
        if not stime and ":" not in str(day):
            # A DATE without any time: parsing it would fabricate «полночь»,
            # which is always in the past — the meeting would silently become
            # «пропущена». Treat it as «без времени» instead: the card stays
            # visible with a manual «Подключиться», and the bot never
            # auto-joins at a made-up 00:00.
            return None
        combined = f"{day}T{stime}" if stime else str(day)
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


def _as_bool(v: Any) -> bool | None:
    """Interpret a checkbox custom-field value as True/False, or None if unclear."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on", "да", "вкл", "checked", "y"):
            return True
        if s in ("0", "false", "no", "off", "нет", "выкл", "", "n"):
            return False
        return None
    if isinstance(v, dict):
        for k in ("value", "checked", "enabled", "state", "isChecked"):
            if k in v:
                return _as_bool(v[k])
    return None


def custom_field_bool(task: dict, field_name: str) -> bool | None:
    """Value of a checkbox/toggle custom field by (case-insensitive, then partial)
    name — e.g. «Запись встречи». Returns True/False, or None if the field is
    absent or its value can't be read as a boolean."""
    name = (field_name or "").strip().lower()
    if not name:
        return None
    fields = task.get("customFields") or []
    cf = next((c for c in fields if isinstance(c, dict)
               and str(c.get("name", "")).strip().lower() == name), None)
    if cf is None:  # partial-name fallback
        cf = next((c for c in fields if isinstance(c, dict)
                   and name in str(c.get("name", "")).lower()), None)
    if cf is None:
        return None
    return _as_bool(cf.get("value"))


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
