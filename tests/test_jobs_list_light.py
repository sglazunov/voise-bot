"""Список встреч лёгкий: протоколы в нём не ездят (жалоба 15.10 «страница
грузится очень долго» — 236 задач давали 6 МБ на каждый опрос раз в 4 с)."""
from __future__ import annotations

import json

from app import llm
from app.jobs import store
from conftest import register


def _fat_job(owner):
    job = store.create("Планёрка.mp4", "/data/uploads/x.mp4", "ru", False, owner=owner)
    job.analysis = {"summary": "я" * 50_000, "tasks": [{"task": "т" * 1000}] * 50}
    job.weeek_tasks = [{"fp": str(i), "text": "ж" * 2000} for i in range(20)]
    job.status = "done"
    return job


def test_список_без_протоколов(client):
    register(client, "alice", phone="+7 999 000-00-01")
    job = _fat_job("alice")
    rows = client.get("/api/jobs").json()
    row = next(r for r in rows if r["id"] == job.id)
    assert "analysis" not in row and "weeek_tasks" not in row
    assert "audio_path" not in row
    assert row["has_analysis"] is True
    # Всё, чем пользуется список и календарь на странице «Распознавание».
    for k in ("id", "filename", "status", "progress", "created_at"):
        assert k in row
    assert len(json.dumps(row, ensure_ascii=False)) < 2000


def test_карточка_по_id_полная(client):
    register(client, "alice", phone="+7 999 000-00-01")
    job = _fat_job("alice")
    d = client.get(f"/api/jobs/{job.id}").json()
    assert d["analysis"]["summary"].startswith("я")
    assert len(d["weeek_tasks"]) == 20


def test_зонд_ollama_кэшируется(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise OSError("connection refused")

    monkeypatch.setattr(llm.urllib.request, "urlopen", boom)
    monkeypatch.setattr(llm, "_ollama_cache", {})
    assert llm.list_ollama_models() == []
    llm.list_ollama_models(); llm.ollama_status(); llm.ollama_status()
    # По одному зонду на вид, неудача тоже кэшируется.
    assert len(calls) == 2


def test_partial_отдаёт_только_новые_фрагменты(client):
    """Опрос раз в 2 с не тянет расшифровку с начала: since — сколько уже есть."""
    register(client, "alice", phone="+7 999 000-00-01")
    job = _fat_job("alice")
    job.status = "running"
    store._partial[job.id] = [{"start": i, "end": i + 1, "text": f"ф{i}"} for i in range(5)]
    p = client.get(f"/api/jobs/{job.id}/partial").json()
    assert p["from"] == 0 and p["total"] == 5 and len(p["segments"]) == 5
    p = client.get(f"/api/jobs/{job.id}/partial?since=3").json()
    assert p["from"] == 3 and [s["text"] for s in p["segments"]] == ["ф3", "ф4"]
    p = client.get(f"/api/jobs/{job.id}/partial?since=5").json()
    assert p["from"] == 5 and p["segments"] == []
    # Задачу начали заново — фрагментов меньше, чем у клиента: отдаём всё.
    store._partial[job.id] = store._partial[job.id][:2]
    p = client.get(f"/api/jobs/{job.id}/partial?since=5").json()
    assert p["from"] == 0 and len(p["segments"]) == 2
    assert "analysis" not in json.dumps(p["segments"])
