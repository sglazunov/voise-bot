"""Weeek time parsing + task pagination.

Regression cover for two real bugs:
  * a single-time meeting near midnight (00:25) landed on the WRONG day because
    Weeek returns `date` (UTC date) and `time` (local time) in mismatched zones —
    parse_start must prefer the full-UTC `dueDateTime`;
  * meetings past the first 100 tasks (e.g. an older task rescheduled into the
    future) were never fetched — list_tasks must paginate by `offset`.
"""
from datetime import timezone
from zoneinfo import ZoneInfo

from app.automation import weeek

MSK = ZoneInfo("Europe/Moscow")


class TestParseStart:
    def test_single_time_near_midnight_uses_duedatetime(self):
        # 00:25 MSK on 16.07 == 21:25 UTC on 15.07. Weeek's date/time are in
        # mismatched zones; dueDateTime is the source of truth.
        task = {"dueDateTime": "2026-07-15T21:25:00Z", "date": "15.07.2026", "time": "00:25"}
        start = weeek.parse_start(task, MSK)
        assert start == weeek._parse_dt("2026-07-15T21:25:00Z")
        assert start.astimezone(MSK).strftime("%d.%m %H:%M") == "16.07 00:25"

    def test_range_meeting_uses_start_not_end(self):
        task = {"startDateTime": "2026-07-22T07:00:00Z",
                "dueDateTime": "2026-07-22T08:00:00Z", "timeStart": "07:00"}
        start = weeek.parse_start(task, MSK)
        assert start.astimezone(MSK).strftime("%d.%m %H:%M") == "22.07 10:00"  # 07:00 UTC

    def test_date_time_fallback_when_no_datetime(self):
        # No dueDateTime/startDateTime -> fall back to day + local time.
        task = {"date": "22.07.2026", "time": "16:00"}
        start = weeek.parse_start(task, MSK)
        assert start.astimezone(MSK).strftime("%d.%m %H:%M") == "22.07 16:00"

    def test_no_time_returns_none(self):
        assert weeek.parse_start({"title": "x"}, MSK) is None


class TestPagination:
    def test_list_tasks_collects_pages_by_offset(self, monkeypatch):
        # Pages are fetched CONCURRENTLY (up to max_tasks/100 of them), so
        # offsets past the data come back empty — that's the real API's shape.
        pages = {
            0: {"tasks": [{"id": i} for i in range(100)], "hasMore": True},
            100: {"tasks": [{"id": i} for i in range(100, 150)], "hasMore": False},
        }
        seen_offsets = []

        def fake_request(method, path, token, params=None, **kw):
            off = (params or {}).get("offset", 0)
            seen_offsets.append(off)
            return pages.get(off, {"tasks": [], "hasMore": False})

        monkeypatch.setattr(weeek, "_request", fake_request)
        tasks = weeek.list_tasks("tok")
        assert len(tasks) == 150                     # both pages collected, in order
        assert [t["id"] for t in tasks] == list(range(150))
        assert {0, 100} <= set(seen_offsets)         # paginated by offset

    def test_list_tasks_stops_at_cap(self, monkeypatch):
        def fake_request(method, path, token, params=None, **kw):
            return {"tasks": [{"id": 1} for _ in range(100)], "hasMore": True}

        monkeypatch.setattr(weeek, "_request", fake_request)
        tasks = weeek.list_tasks("tok", max_tasks=250)
        assert len(tasks) == 300  # 3 pages of 100, then len >= max_tasks stops the loop


class TestDateOnly:
    def test_date_without_time_is_no_time_not_midnight(self):
        # Задачу-встречу создали «на сегодня» без времени: полночь — фикция,
        # из-за неё встреча молча становилась «пропущенной» и пряталась.
        assert weeek.parse_start({"date": "20.07.2026"}, MSK) is None

    def test_date_with_embedded_time_still_parses(self):
        # Поле date иногда несёт полный datetime — он остаётся рабочим.
        start = weeek.parse_start({"date": "2026-07-20T10:30"}, MSK)
        assert start is not None
