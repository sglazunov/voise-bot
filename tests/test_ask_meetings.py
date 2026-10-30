"""Вопрос по ВСЕМ встречам команды (analyze.ask_meetings + POST /api/ask):
куски берутся из памяти встреч, модель получает их с подписями «встреча —
дата — [таймкод]», пустой результат отдаётся без вызова модели."""
import pytest

from app import analyze, db, meeting_index
from tests.conftest import login, register


def _row(doc, idx, text, kind="transcript", title="Встреча лидеров",
         date="21.10.2026", ts="00:12:00", draft=False):
    return {"id": f"{doc}:{kind[0]}{idx}", "doc_id": doc, "kind": kind, "idx": idx,
            "title": title, "meeting_date": date, "meeting_ts": 1.0, "ts": ts,
            "text": text, "is_draft": draft, "series_key": "x", "source": "",
            "rank": 1.0}


class _Fake:
    name = "fake"

    def __init__(self, captured):
        self.captured = captured

    def complete(self, prompt, max_tokens=2000, force_json=True, **kw):
        self.captured["prompt"] = prompt
        self.captured["calls"] = self.captured.get("calls", 0) + 1
        return "Решили снять заглушку (Встреча лидеров, 21.10.2026, [00:12:00])."


@pytest.fixture
def pg(monkeypatch):
    """Память встреч «включена» (только она — остальная база в файловом
    режиме, иначе вход пользователя полез бы в настоящий Postgres)."""
    store = {"rows": [], "queries": []}
    monkeypatch.setattr(meeting_index, "enabled", lambda: True)

    def search(team, q, limit=24, series_key=None, kinds=None):
        store["queries"].append((team, q))
        return list(store["rows"])
    monkeypatch.setattr(db, "chunks_search", search)
    monkeypatch.setattr(db, "chunks_count", lambda team: {"docs": 2, "chunks": 9,
                                                           "first_ts": 1.0, "last_ts": 2.0})
    return store


def test_fts_queries_strict_then_or():
    qs = analyze._fts_queries("что решили по бюджету сайта?")
    assert qs[0] == "что решили по бюджету сайта?"
    assert qs[1] == "бюджету or сайта"


def test_dedup_drops_overlapping_neighbour_and_caps_docs():
    rows = [_row("a", 3, "x"), _row("a", 4, "y"), _row("a", 6, "z"),
            _row("a", 0, "p", kind="protocol")]
    rows += [_row(f"d{i}", 0, "t") for i in range(10)]
    out = analyze._dedup_chunks(rows)
    ids = [(r["doc_id"], r["kind"], r["idx"]) for r in out]
    assert ("a", "transcript", 4) not in ids and ("a", "transcript", 6) in ids
    assert ("a", "protocol", 0) in ids
    assert len({r["doc_id"] for r in out}) == analyze._ASK_ALL_MAX_DOCS


def test_answer_cites_meetings(pg, monkeypatch):
    captured = {}
    monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: _Fake(captured))
    pg["rows"] = [_row("job1", 2, "[00:12:00] Зоя Р: заглушку снимаем"),
                  _row("archive:1", 0, "Решения:\n- снять заглушку", kind="protocol",
                       title="CRM", date="14.10.2026", ts="", draft=True)]
    res = analyze.ask_meetings("alice", "что решили по заглушке?")
    assert "Решили снять заглушку" in res["answer"]
    assert res["found"] == 2 and len(res["sources"]) == 2
    p = captured["prompt"]
    assert "=== Встреча лидеров — 21.10.2026 — [00:12:00] ===" in p
    assert "=== CRM — 14.10.2026 — черновик протокола ===" in p
    assert "не обсуждалось" in p
    assert pg["queries"][0] == ("alice", "что решили по заглушке?")


def test_second_query_with_or_when_strict_finds_nothing(pg, monkeypatch):
    captured = {}
    monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: _Fake(captured))
    calls = {"n": 0}

    def search(team, q, limit=24, series_key=None, kinds=None):
        calls["n"] += 1
        pg["queries"].append(q)
        return [] if calls["n"] == 1 else [_row("job1", 0, "бюджет сайта — 200")]
    monkeypatch.setattr(db, "chunks_search", search)
    res = analyze.ask_meetings("alice", "что решили по бюджету сайта?")
    assert res["found"] == 1
    assert pg["queries"] == ["что решили по бюджету сайта?", "бюджету or сайта"]


def test_nothing_found_does_not_call_model(pg, monkeypatch):
    captured = {}
    monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: _Fake(captured))
    res = analyze.ask_meetings("alice", "что по кибербезопасности?")
    assert res["found"] == 0 and res["sources"] == []
    assert "ничего не нашлось" in res["answer"]
    assert captured.get("calls", 0) == 0


def test_question_of_stopwords_asks_to_clarify(pg, monkeypatch):
    captured = {}
    monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: _Fake(captured))
    res = analyze.ask_meetings("alice", "ну и что?")
    assert "Уточните" in res["answer"] and captured.get("calls", 0) == 0


def test_without_postgres_raises():
    with pytest.raises(ValueError):
        analyze.ask_meetings("alice", "что решили по заглушке?")


class TestApi:
    def test_requires_login_and_postgres(self, client):
        assert client.post("/api/ask", json={"question": "что решили?"}).status_code in (401, 403)
        register(client); login(client)
        r = client.post("/api/ask", json={"question": "что решили по заглушке?"})
        assert r.status_code == 409
        assert client.get("/api/ask/status").json() == {"enabled": False, "docs": 0, "chunks": 0}

    def test_answer_is_team_scoped_and_logged(self, client, pg, monkeypatch):
        captured = {}
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: _Fake(captured))
        pg["rows"] = [_row("job1", 2, "[00:12:00] заглушку снимаем")]
        register(client); login(client)
        r = client.post("/api/ask", json={"question": "что решили по заглушке?"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] and body["sources"][0]["doc_id"] == "job1"
        assert pg["queries"][0][0] == "alice"          # команда = логин админа
        assert client.post("/api/ask", json={"question": "хм"}).status_code == 400
        st = client.get("/api/ask/status").json()
        assert st["enabled"] and st["docs"] == 2
