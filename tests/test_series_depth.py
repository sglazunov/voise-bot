"""Глубокая память серии: кроме прошлой встречи в промпт идут краткие итоги
нескольких встреч до неё — из истории карточки и из памяти встреч."""
from app import db, meeting_index, meeting_series as ms


def _analysis(summary: str, decision: str) -> dict:
    return {"summary": summary, "decisions": [decision],
            "tasks": [{"task": "Задача " + decision[:10], "owner": "Алсу"}],
            "minor_tasks": [], "done_tasks": [], "participants": []}


def _remember(date: str, n: int) -> None:
    ms.remember("alice", f"{date}, 09-00. - Гранты - внутренняя.mp4",
                _analysis(f"Итог встречи {n}.", f"Решение {n}"), job_id=f"j{n}")


def test_history_keeps_summary_and_decisions():
    _remember("24.08.2026", 1)
    h = ms.load("alice")["гранты внутренняя"]["history"][-1]
    assert h["summary"] == "Итог встречи 1." and h["decided"] == ["Решение 1"]
    assert h["open"] == ["Задача Решение 1 — Алсу"]


def test_block_lists_earlier_meetings_newest_first_without_last():
    for i, d in enumerate(["24.08.2026", "31.08.2026", "07.09.2026", "14.09.2026", "21.09.2026"], 1):
        _remember(d, i)
    blk = ms.block_for("alice", "28.09.2026, 09-00. - Гранты - внутренняя.mp4")
    assert ms.LAST_HEADER + " (21.09.2026)" in blk
    assert ms.EARLIER_HEADER in blk
    earlier = blk.split(ms.EARLIER_HEADER)[1]
    assert "Итог встречи 5." not in earlier            # прошлая — в своём блоке
    lines = [ln for ln in earlier.splitlines() if ln.startswith("• ")]
    assert [ln[2:12] for ln in lines] == ["14.09.2026", "07.09.2026", "31.08.2026"]
    assert "24.08.2026" not in earlier                  # не больше трёх
    assert "Решили: Решение 4." in lines[0]


def test_single_past_meeting_has_no_earlier_block():
    _remember("24.08.2026", 1)
    blk = ms.block_for("alice", "31.08.2026, 09-00. - Гранты - внутренняя.mp4")
    assert ms.LAST_HEADER in blk and ms.EARLIER_HEADER not in blk


def test_missing_meetings_come_from_meeting_memory(monkeypatch):
    _remember("14.09.2026", 4)
    _remember("21.09.2026", 5)
    monkeypatch.setattr(meeting_index, "enabled", lambda: True)
    calls = {}

    def recent(team, key, limit=3, kind="protocol", exclude_doc=None, before_ts=None):
        calls.update(team=team, key=key, exclude_doc=exclude_doc)
        return [{"doc_id": "archive:a", "meeting_date": "07.09.2026", "kind": kind, "idx": 0,
                 "text": "О чём шёл разговор: Из архива про гранты."},
                {"doc_id": "archive:a", "meeting_date": "07.09.2026", "kind": kind, "idx": 1,
                 "text": "Решения и договорённости:\n- Подать заявку\n- Второе"},
                # Та же дата, что у записи истории, — не дублируется.
                {"doc_id": "job-x", "meeting_date": "14.09.2026", "kind": kind, "idx": 0,
                 "text": "О чём шёл разговор: дубль"}]
    monkeypatch.setattr(db, "chunks_series_recent", recent)
    blk = ms.block_for("alice", "28.09.2026, 09-00. - Гранты - внутренняя.mp4")
    earlier = blk.split(ms.EARLIER_HEADER)[1]
    lines = [ln for ln in earlier.splitlines() if ln.startswith("• ")]
    assert [ln[2:12] for ln in lines] == ["14.09.2026", "07.09.2026"]
    assert "Из архива про гранты. Решили: Подать заявку; Второе." in lines[1]
    assert "дубль" not in earlier
    assert calls == {"team": "alice", "key": "гранты внутренняя", "exclude_doc": "j5"}


def test_memory_failure_does_not_break_block(monkeypatch):
    _remember("14.09.2026", 4)
    _remember("21.09.2026", 5)
    monkeypatch.setattr(meeting_index, "enabled", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("база недоступна")
    monkeypatch.setattr(db, "chunks_series_recent", boom)
    blk = ms.block_for("alice", "28.09.2026, 09-00. - Гранты - внутренняя.mp4")
    assert ms.LAST_HEADER in blk and "14.09.2026" in blk
