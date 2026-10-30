"""Эмбеддинги памяти встреч (app/embeddings.py, векторная часть app/db.py):
провайдеры зовут нужный адрес с нужными заголовками и телом, разбирают ответ,
режут длинный текст, молчат об ошибке (пустой результат, не исключение);
pgvector при неудаче выключается, а не роняет процесс; SQL векторных запросов
фильтрует по команде. Живого Postgres и HTTP нет — всё подменено."""
import logging

import pytest

from app import config, db, embeddings as emb, meeting_index as mi


@pytest.fixture(autouse=True)
def _reset_vector_state(monkeypatch):
    monkeypatch.setattr(db, "_vector_state", {"tried": False, "ready": False, "dim": 0})


@pytest.fixture
def http(monkeypatch):
    """Подмена llm_base._http_post_json в модуле эмбеддингов: пишет вызовы,
    отвечает тем, что положили в `reply` (исключение — поднимается)."""
    calls = []
    box = {"reply": None}

    def fake(url, payload, headers, timeout=180, max_retries=3):
        calls.append({"url": url, "payload": payload, "headers": headers,
                      "max_retries": max_retries})
        if isinstance(box["reply"], Exception):
            raise box["reply"]
        return box["reply"](payload) if callable(box["reply"]) else box["reply"]
    monkeypatch.setattr(emb, "_http_post_json", fake)
    box["calls"] = calls
    return box


# --------------------------------------------------------------------------- #
# Провайдеры
# --------------------------------------------------------------------------- #
class TestYandex:
    def test_doc_and_query_models_headers_and_parse(self, http):
        http["reply"] = {"embedding": [0.1] * 256}
        p = emb.YandexEmbeddings("KEY", "b1gfolder")
        assert p.dim() == 256
        out = p.embed_texts(["решили снять заглушку"], emb.KIND_DOC)
        assert out == [[0.1] * 256]
        c = http["calls"][0]
        assert c["url"] == "https://llm.api.cloud.yandex.net/foundationModels/v1/textEmbedding"
        assert c["headers"] == {"Authorization": "Api-Key KEY", "x-folder-id": "b1gfolder"}
        assert c["payload"] == {"modelUri": "emb://b1gfolder/text-search-doc/latest",
                                "text": "решили снять заглушку"}
        p.embed_texts(["что решили?"], emb.KIND_QUERY)
        assert http["calls"][1]["payload"]["modelUri"] == "emb://b1gfolder/text-search-query/latest"
        # Ключ не должен попасть в адрес — только в заголовок.
        assert "KEY" not in c["url"]

    def test_one_request_per_text_and_empty_skipped(self, http):
        http["reply"] = {"embedding": [0.5] * 256}
        p = emb.YandexEmbeddings("k", "f")
        out = p.embed_texts(["a", "", "   ", "b"])
        assert len(http["calls"]) == 2
        assert [bool(v) for v in out] == [True, False, False, True]

    def test_long_text_clipped(self, http):
        http["reply"] = {"embedding": [0.5] * 256}
        emb.YandexEmbeddings("k", "f").embed_texts(["я" * 20_000])
        assert len(http["calls"][0]["payload"]["text"]) == emb.MAX_CHARS

    def test_error_gives_empty_and_stops_batch(self, http, caplog):
        http["reply"] = RuntimeError("HTTP 401 от …: ключ")
        p = emb.YandexEmbeddings("k", "f")
        with caplog.at_level(logging.WARNING, logger="vtx.embeddings"):
            out = p.embed_texts(["a", "b", "c"])
        assert out == [[], [], []]
        assert len(http["calls"]) == 1          # после ошибки остальные не шлются
        assert "не посчитан" in caplog.text

    def test_wrong_dimension_dropped(self, http, caplog):
        http["reply"] = {"embedding": [0.5] * 10}
        with caplog.at_level(logging.WARNING, logger="vtx.embeddings"):
            out = emb.YandexEmbeddings("k", "f").embed_texts(["a"])
        assert out == [[]] and "размерность" in caplog.text

    def test_garbage_answer_is_empty(self, http):
        http["reply"] = {"result": "oops"}
        assert emb.YandexEmbeddings("k", "f").embed_texts(["a"]) == [[]]


class TestGemini:
    def test_url_header_task_type_and_dimension(self, http):
        http["reply"] = {"embedding": {"values": [0.2] * 768}}
        p = emb.GeminiEmbeddings("AIza-key")
        assert p.model == "gemini-embedding-001" and p.dim() == 768
        out = p.embed_texts(["текст куска"])
        assert out == [[0.2] * 768]
        c = http["calls"][0]
        assert c["url"] == ("https://generativelanguage.googleapis.com/v1beta/models/"
                            "gemini-embedding-001:embedContent")
        assert c["headers"] == {"x-goog-api-key": "AIza-key"}
        assert c["payload"] == {"content": {"parts": [{"text": "текст куска"}]},
                                "taskType": "RETRIEVAL_DOCUMENT",
                                "outputDimensionality": 768}
        p.embed_texts(["вопрос"], emb.KIND_QUERY)
        assert http["calls"][1]["payload"]["taskType"] == "RETRIEVAL_QUERY"

    def test_custom_model_and_dim(self, http):
        http["reply"] = {"embedding": {"values": [0.2] * 256}}
        p = emb.GeminiEmbeddings("k", model="text-embedding-005", dim=256)
        assert p.embed_texts(["a"]) == [[0.2] * 256]
        c = http["calls"][0]
        assert "text-embedding-005:embedContent" in c["url"]
        assert c["payload"]["outputDimensionality"] == 256

    def test_error_is_silent(self, http):
        http["reply"] = RuntimeError("HTTP 429")
        assert emb.GeminiEmbeddings("k").embed_texts(["a"]) == [[]]


class TestOllama:
    def test_batch_request_and_learned_dim(self, http, monkeypatch):
        monkeypatch.setattr(config, "OLLAMA_URL", "http://ollama:11434/")
        http["reply"] = lambda payload: {"embeddings": [[0.1, 0.2, 0.3] for _ in payload["input"]]}
        p = emb.OllamaEmbeddings()
        assert p.model == "nomic-embed-text" and p.dim() == 0
        out = p.embed_texts(["a", "", "b"])
        assert out == [[0.1, 0.2, 0.3], [], [0.1, 0.2, 0.3]]
        assert p.dim() == 3                     # размерность — из первого ответа
        c = http["calls"][0]
        assert c["url"] == "http://ollama:11434/api/embed"
        assert c["payload"] == {"model": "nomic-embed-text", "input": ["a", "b"]}
        assert len(http["calls"]) == 1          # одна пачка — один запрос

    def test_all_empty_no_request(self, http):
        assert emb.OllamaEmbeddings().embed_texts(["", " "]) == [[], []]
        assert http["calls"] == []

    def test_count_mismatch_and_error_are_empty(self, http, caplog):
        http["reply"] = {"embeddings": [[0.1]]}
        with caplog.at_level(logging.WARNING, logger="vtx.embeddings"):
            assert emb.OllamaEmbeddings().embed_texts(["a", "b"]) == [[], []]
        assert "ожидалось 2" in caplog.text
        http["reply"] = RuntimeError("Не удалось подключиться")
        assert emb.OllamaEmbeddings().embed_texts(["a"]) == [[]]


# --------------------------------------------------------------------------- #
# Выбор провайдера и ключа
# --------------------------------------------------------------------------- #
class TestProviderFor:
    def test_disabled_by_default(self):
        assert config.EMBED_PROVIDER == ""
        assert not emb.enabled()
        assert emb.provider_for("alice") is None
        assert emb.embed_texts("alice", ["a", "b"]) == [[], []]
        assert emb.embed_query("alice", "что решили?") is None

    def test_team_key_wins_over_env(self, monkeypatch):
        monkeypatch.setattr(config, "EMBED_PROVIDER", "yandex")
        monkeypatch.setattr(config, "YANDEX_API_KEY", "env-key")
        monkeypatch.setattr(config, "YANDEX_FOLDER_ID", "env-folder")
        from app import user_creds
        monkeypatch.setattr(user_creds, "load",
                            lambda team: {"yandex": [{"key": "team-key", "extra": "team-folder"}]})
        p = emb.provider_for("alice")
        assert isinstance(p, emb.YandexEmbeddings)
        assert (p.api_key, p.folder) == ("team-key", "team-folder")

    def test_env_fallback_and_missing_key(self, monkeypatch, caplog):
        monkeypatch.setattr(config, "EMBED_PROVIDER", "gemini")
        monkeypatch.setattr(config, "EMBED_MODEL", "gemini-embedding-001")
        monkeypatch.setattr(config, "GEMINI_API_KEY", "env-gemini")
        p = emb.provider_for(None)
        assert isinstance(p, emb.GeminiEmbeddings) and p.api_key == "env-gemini"
        monkeypatch.setattr(config, "GEMINI_API_KEY", "")
        with caplog.at_level(logging.WARNING, logger="vtx.embeddings"):
            assert emb.provider_for(None) is None
        assert "ключа Gemini нет" in caplog.text

    def test_ollama_needs_no_key(self, monkeypatch):
        monkeypatch.setattr(config, "EMBED_PROVIDER", "ollama")
        monkeypatch.setattr(config, "EMBED_MODEL", "bge-m3")
        monkeypatch.setattr(config, "EMBED_DIM", 1024)
        p = emb.provider_for("alice")
        assert isinstance(p, emb.OllamaEmbeddings)
        assert p.model == "bge-m3" and p.dim() == 1024

    def test_embed_query_uses_query_kind(self, monkeypatch, http):
        monkeypatch.setattr(config, "EMBED_PROVIDER", "yandex")
        monkeypatch.setattr(config, "YANDEX_API_KEY", "k")
        monkeypatch.setattr(config, "YANDEX_FOLDER_ID", "f")
        http["reply"] = {"embedding": [0.3] * 256}
        assert emb.embed_query("alice", "что решили?") == [0.3] * 256
        assert "text-search-query" in http["calls"][0]["payload"]["modelUri"]


# --------------------------------------------------------------------------- #
# pgvector в db.py
# --------------------------------------------------------------------------- #
class _Cur:
    def __init__(self, log, one=None, many=None):
        self.log, self.one, self.many = log, one, many

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))

    def executemany(self, sql, rows):
        self.log.append((" ".join(sql.split()), list(rows)))

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many or []


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def fake_db(monkeypatch):
    """Подмена соединения: SQL копится в списке, ответ задаётся заранее."""
    box = {"log": [], "one": None, "many": None}
    monkeypatch.setattr(db, "enabled", lambda: True)
    monkeypatch.setattr(db, "_conn", lambda: _Conn())
    monkeypatch.setattr(db, "_cur", lambda conn: _Cur(box["log"], box["one"], box["many"]))
    return box


class TestVectorReady:
    def test_disabled_db_or_zero_dim(self):
        assert db.vector_ready(256) is False     # DATABASE_URL в тестах снят
        assert db.vector_ready(0) is False

    def test_failure_returns_false_once_and_does_not_raise(self, monkeypatch, caplog):
        monkeypatch.setattr(db, "enabled", lambda: True)
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise RuntimeError('extension "vector" is not available')
        monkeypatch.setattr(db, "_conn", boom)
        with caplog.at_level(logging.WARNING, logger="vtx.db"):
            assert db.vector_ready(256) is False
            assert db.vector_ready(256) is False
        assert calls["n"] == 1                   # второй раз в базу не ходим
        assert "pgvector недоступен" in caplog.text

    def test_success_creates_extension_column_index(self, fake_db):
        fake_db["one"] = {"dim": 256}
        assert db.vector_ready(256) is True
        sqls = [s for s, _ in fake_db["log"]]
        assert sqls[0] == "CREATE EXTENSION IF NOT EXISTS vector"
        assert "ADD COLUMN IF NOT EXISTS embedding vector(256)" in sqls[1]
        assert any("USING hnsw (embedding vector_cosine_ops)" in s for s in sqls)
        # Повторный вызов — из памяти; другая размерность — False.
        n = len(fake_db["log"])
        assert db.vector_ready(256) is True and len(fake_db["log"]) == n
        assert db.vector_ready(768) is False

    def test_existing_column_with_other_dim_disables(self, fake_db, caplog):
        fake_db["one"] = {"dim": 256}
        with caplog.at_level(logging.WARNING, logger="vtx.db"):
            assert db.vector_ready(768) is False
        assert "создана на 256" in caplog.text
        assert not any("hnsw" in s for s, _ in fake_db["log"])


class TestVectorSql:
    def test_literal(self):
        assert db._vec_literal([0.1, 2, -3.5]) == "[0.1,2.0,-3.5]"

    def test_set_embeddings_skips_empty(self, fake_db):
        assert db.chunks_set_embeddings([("a", [0.1, 0.2]), ("b", []), ("c", [1, 2])]) == 2
        sql, rows = fake_db["log"][0]
        assert sql == "UPDATE meeting_chunks SET embedding=%s::vector WHERE id=%s"
        assert rows == [("[0.1,0.2]", "a"), ("[1.0,2.0]", "c")]
        assert db.chunks_set_embeddings([("b", [])]) == 0

    def test_vector_search_is_team_scoped(self, fake_db):
        fake_db["many"] = [{"id": "x", "dist": 0.1}]
        rows = db.chunks_vector_search("alice", [0.1, 0.2], limit=5, kinds=("protocol",))
        assert rows == [{"id": "x", "dist": 0.1}]
        sql, params = fake_db["log"][0]
        assert "WHERE team=%s AND embedding IS NOT NULL" in sql
        assert "embedding <=> %s::vector AS dist" in sql
        assert sql.endswith("ORDER BY embedding <=> %s::vector LIMIT %s")
        assert "kind = ANY(%s)" in sql
        assert params == ["[0.1,0.2]", "alice", ["protocol"], "[0.1,0.2]", 5]

    def test_without_embedding_and_stats(self, fake_db):
        fake_db["many"] = [{"id": "a", "text": "t"}]
        assert db.chunks_without_embedding("alice", 10) == [{"id": "a", "text": "t"}]
        sql, params = fake_db["log"][0]
        assert "WHERE team=%s AND embedding IS NULL" in sql and params == ("alice", 10)
        fake_db["one"] = {"chunks": 9, "embedded": 4}
        assert db.chunks_embedding_stats("alice") == {"chunks": 9, "embedded": 4}
        assert "WHERE team=%s" in fake_db["log"][1][0]


# --------------------------------------------------------------------------- #
# embed_doc / store_doc — best-effort
# --------------------------------------------------------------------------- #
def _rows(n, doc="job1"):
    return [{"id": f"{doc}:t{i}", "doc_id": doc, "text": f"кусок {i}"} for i in range(n)]


class TestEmbedDoc:
    def test_disabled_does_nothing(self, monkeypatch):
        monkeypatch.setattr(db, "vector_ready", lambda d: pytest.fail("не должно звать базу"))
        assert mi.embed_doc("alice", _rows(3)) == 0

    def test_writes_vectors_in_batches(self, monkeypatch):
        monkeypatch.setattr(emb, "enabled", lambda: True)
        monkeypatch.setattr(emb, "BATCH", 2)
        sent, written, ready = [], [], []
        monkeypatch.setattr(emb, "embed_texts",
                            lambda team, texts, kind: sent.append((team, texts, kind))
                            or [[0.1, 0.2] for _ in texts])
        monkeypatch.setattr(db, "vector_ready", lambda d: ready.append(d) or True)
        monkeypatch.setattr(db, "chunks_set_embeddings",
                            lambda pairs: written.extend(pairs) or len(pairs))
        assert mi.embed_doc("alice", _rows(5)) == 5
        assert [len(t) for _, t, _ in sent] == [2, 2, 1]
        assert all(team == "alice" and kind == emb.KIND_DOC for team, _, kind in sent)
        assert ready == [2, 2, 2] and [p[0] for p in written] == [r["id"] for r in _rows(5)]

    def test_provider_failure_stops_quietly(self, monkeypatch, caplog):
        monkeypatch.setattr(emb, "enabled", lambda: True)
        monkeypatch.setattr(emb, "embed_texts", lambda team, texts, kind: [[] for _ in texts])
        monkeypatch.setattr(db, "chunks_set_embeddings",
                            lambda pairs: pytest.fail("нечего писать"))
        with caplog.at_level(logging.WARNING, logger="vtx.meeting_index"):
            assert mi.embed_doc("alice", _rows(3)) == 0
        assert "не посчитаны" in caplog.text

    def test_no_pgvector_or_db_error_do_not_raise(self, monkeypatch):
        monkeypatch.setattr(emb, "enabled", lambda: True)
        monkeypatch.setattr(emb, "embed_texts", lambda team, texts, kind: [[0.1] for _ in texts])
        monkeypatch.setattr(db, "vector_ready", lambda d: False)
        assert mi.embed_doc("alice", _rows(2)) == 0

        def boom(pairs):
            raise RuntimeError("connection lost")
        monkeypatch.setattr(db, "vector_ready", lambda d: True)
        monkeypatch.setattr(db, "chunks_set_embeddings", boom)
        assert mi.embed_doc("alice", _rows(2)) == 0

    def test_backfill_script_paginates_and_stops_on_dead_provider(self, monkeypatch):
        """scripts/embed_backfill.py: берёт куски без вектора пачками, куски с
        осечкой не зацикливают прогон, мёртвый провайдер останавливает его."""
        from scripts import embed_backfill as bf
        pending = {f"c{i}": f"текст {i}" for i in range(5)}
        written = []

        def without(team, limit):
            assert team == "alice"
            return [{"id": k, "text": v} for k, v in list(pending.items())[:limit]]

        def set_emb(pairs):
            for i, _ in pairs:
                pending.pop(i)
                written.append(i)
            return len(pairs)
        monkeypatch.setattr(db, "chunks_without_embedding", without)
        monkeypatch.setattr(db, "chunks_set_embeddings", set_emb)

        class _P:
            name, model = "fake", "m"

            def embed_texts(self, texts, kind):
                # «текст 1» провайдер не осилил — остаётся без вектора.
                return [[] if t == "текст 1" else [0.1] for t in texts]
        lines = []
        done, skipped = bf.backfill("alice", _P(), limit=None, batch=2, out=lines.append)
        assert (done, skipped) == (4, 1) and written == ["c0", "c2", "c3", "c4"]
        assert pending == {"c1": "текст 1"}

        class _Dead:
            name, model = "fake", "m"

            def embed_texts(self, texts, kind):
                return [[] for _ in texts]
        pending.update({"c7": "x", "c8": "y"})
        done, skipped = bf.backfill("alice", _Dead(), limit=None, batch=2, out=lines.append)
        assert done == 0 and any("не отвечает" in ln for ln in lines)

    def test_store_doc_replaces_then_embeds(self, monkeypatch):
        order = []
        monkeypatch.setattr(db, "chunks_replace",
                            lambda doc_id, rows: order.append(("replace", doc_id)) or len(rows))
        monkeypatch.setattr(mi, "embed_doc",
                            lambda team, rows: order.append(("embed", team)) or 0)
        assert mi.store_doc("job1", "alice", _rows(2)) == 2
        assert order == [("replace", "job1"), ("embed", "alice")]
