"""Гибридный поиск по памяти встреч (analyze.retrieve_meetings): без
эмбеддингов — полнотекст ровно как раньше; с ними — списки полнотекста и
вектора сливаются Reciprocal Rank Fusion, дедуп и лимит прежние. Вектор
вопроса не посчитался или база без pgvector — снова один полнотекст."""
import pytest

from app import analyze, db, embeddings as emb


def _row(doc, idx, kind="transcript", text="x"):
    return {"id": f"{doc}:{kind[0]}{idx}", "doc_id": doc, "kind": kind, "idx": idx,
            "title": doc, "meeting_date": "21.10.2026", "meeting_ts": 1.0, "ts": "",
            "text": text, "is_draft": False, "series_key": "s", "source": ""}


class TestRrf:
    def test_both_lists_beat_one_and_order_is_kept(self):
        a, b, c, d = (_row(x, 0) for x in "abcd")
        merged = analyze._rrf_merge([[a, b, c], [c, d]])
        ids = [r["id"] for r in merged]
        # c есть в обоих списках (2-е и 1-е места) — выше всех; дубля нет.
        assert ids[0] == "c:t0" and ids.count("c:t0") == 1
        assert ids == ["c:t0", "a:t0", "b:t0", "d:t0"]

    def test_ties_keep_first_list_order_and_rows_are_first_seen(self):
        a1 = dict(_row("a", 0), rank=1.0)
        a2 = dict(_row("a", 0), dist=0.1)
        b = _row("b", 0)
        merged = analyze._rrf_merge([[a1, b], [b, a2]])
        assert [r["id"] for r in merged] == ["a:t0", "b:t0"]
        assert "rank" in merged[0] and "dist" not in merged[0]

    def test_empty_lists(self):
        assert analyze._rrf_merge([[], []]) == []
        assert [r["id"] for r in analyze._rrf_merge([[], [_row("z", 1)]])] == ["z:t1"]


@pytest.fixture
def searches(monkeypatch):
    """Полнотекст и вектор подменены; `fts`/`vec` — что отдать, `calls` — что
    спрашивали."""
    box = {"fts": [], "vec": [], "calls": [], "ready": True, "query_vec": [0.1, 0.2]}

    def fts(team, q, limit=24, series_key=None, kinds=None):
        box["calls"].append(("fts", team, q, limit))
        return list(box["fts"])

    def vec(team, v, limit=24, kinds=None):
        box["calls"].append(("vec", team, v, limit))
        return list(box["vec"])
    monkeypatch.setattr(db, "chunks_search", fts)
    monkeypatch.setattr(db, "chunks_vector_search", vec)
    monkeypatch.setattr(db, "vector_ready", lambda d: box["ready"])
    monkeypatch.setattr(emb, "embed_query",
                        lambda team, q: box["calls"].append(("embed", team, q))
                        or box["query_vec"])
    return box


class TestRetrieveWithoutEmbeddings:
    def test_fts_only_exactly_as_before(self, searches):
        searches["fts"] = [_row("a", 0), _row("b", 0)]
        rows = analyze.retrieve_meetings("alice", "что решили по бюджету сайта?")
        assert [r["id"] for r in rows] == ["a:t0", "b:t0"]
        kinds = [c[0] for c in searches["calls"]]
        assert "vec" not in kinds and "embed" not in kinds

    def test_nothing_found_is_empty(self, searches):
        assert analyze.retrieve_meetings("alice", "что по бюджету кибербезопасности?") == []
        # Два запроса полнотекста (строгий и через or), вектора нет.
        assert [c[0] for c in searches["calls"]] == ["fts", "fts"]


@pytest.fixture
def hybrid(monkeypatch, searches):
    monkeypatch.setattr(emb, "enabled", lambda: True)
    return searches


class TestRetrieveHybrid:
    def test_merges_fts_and_vector(self, hybrid):
        hybrid["fts"] = [_row("a", 0), _row("b", 0), _row("c", 0)]
        hybrid["vec"] = [_row("c", 0), _row("d", 0)]
        rows = analyze.retrieve_meetings("alice", "что решили по бюджету сайта?")
        assert [r["id"] for r in rows] == ["c:t0", "a:t0", "b:t0", "d:t0"]
        embed = [c for c in hybrid["calls"] if c[0] == "embed"]
        assert embed == [("embed", "alice", "что решили по бюджету сайта?")]
        vec = [c for c in hybrid["calls"] if c[0] == "vec"]
        assert vec == [("vec", "alice", [0.1, 0.2], analyze._ASK_ALL_LIMIT)]

    def test_vector_alone_when_fts_is_empty(self, hybrid):
        hybrid["vec"] = [_row("d", 0), _row("e", 2)]
        rows = analyze.retrieve_meetings("alice", "как дела с деньгами за лендинг?")
        assert [r["id"] for r in rows] == ["d:t0", "e:t2"]

    def test_dedup_and_limit_apply_to_merged(self, hybrid):
        # Соседние окна одной расшифровки — второе лишнее; потолок кусков — limit.
        hybrid["fts"] = [_row("a", 3), _row("a", 4)]
        hybrid["vec"] = [_row(f"d{i}", 0) for i in range(30)]
        rows = analyze.retrieve_meetings("alice", "что решили по бюджету сайта?", limit=5)
        ids = [r["id"] for r in rows]
        assert "a:t4" not in ids and "a:t3" in ids and len(rows) <= 5

    def test_query_vector_missing_falls_back_to_fts(self, hybrid):
        hybrid["fts"] = [_row("a", 0)]
        hybrid["vec"] = [_row("z", 0)]
        hybrid["query_vec"] = None
        rows = analyze.retrieve_meetings("alice", "что решили по бюджету сайта?")
        assert [r["id"] for r in rows] == ["a:t0"]
        assert not [c for c in hybrid["calls"] if c[0] == "vec"]

    def test_no_pgvector_falls_back_to_fts(self, hybrid):
        hybrid["fts"] = [_row("a", 0)]
        hybrid["vec"] = [_row("z", 0)]
        hybrid["ready"] = False
        rows = analyze.retrieve_meetings("alice", "что решили по бюджету сайта?")
        assert [r["id"] for r in rows] == ["a:t0"]
        assert not [c for c in hybrid["calls"] if c[0] == "vec"]

    def test_vector_search_error_does_not_break_answer(self, hybrid, monkeypatch):
        hybrid["fts"] = [_row("a", 0)]

        def boom(team, v, limit=24, kinds=None):
            raise RuntimeError("operator does not exist: vector <=> vector")
        monkeypatch.setattr(db, "chunks_vector_search", boom)
        rows = analyze.retrieve_meetings("alice", "что решили по бюджету сайта?")
        assert [r["id"] for r in rows] == ["a:t0"]

    def test_ask_meetings_uses_hybrid_rows(self, hybrid, monkeypatch):
        from app import meeting_index
        monkeypatch.setattr(meeting_index, "enabled", lambda: True)
        captured = {}

        class _Fake:
            name = "fake"

            def complete(self, prompt, max_tokens=2000, force_json=True, **kw):
                captured["prompt"] = prompt
                return "Ответ (d, 21.10.2026)."
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: _Fake())
        hybrid["vec"] = [_row("d", 0, text="деньги за лендинг — 200 тысяч")]
        res = analyze.ask_meetings("alice", "как дела с деньгами за лендинг?")
        assert res["found"] == 1 and res["sources"][0]["doc_id"] == "d"
        assert "деньги за лендинг" in captured["prompt"]
