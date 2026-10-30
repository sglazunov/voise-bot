"""Индексатор архива протоколов (scripts/index_archive.py) и чтение Диска."""
from pathlib import Path

from app import db, docx_export, meeting_index as mi
from app.automation.clouds import yandex_disk
from scripts import index_archive as ia

PROTO = "01.09.2026, 11-30. - CRM - проектная встреча - протокол.docx"


def _make_docx(path: Path, draft: bool = False) -> Path:
    analysis = {"participants": [{"name": "Зоя Р", "role": "менеджер"}],
                "summary": "Про CRM.", "decisions": ["Снять заглушку"],
                "tasks": [{"task": "Найти тестировщика", "owner": "Зоя Р"}],
                "done_tasks": [], "minor_tasks": [], "detailed": [],
                "key_thoughts": [], "conclusions": []}
    if draft:
        analysis["_fallback"] = ["gemini: 429"]
    segs = [{"start": 0.0, "text": "Начинаем", "speaker": "Зоя Р"},
            {"start": 61.0, "text": "про ОД договорились", "speaker": "Иван"}]
    path.parent.mkdir(parents=True, exist_ok=True)
    docx_export.generate_report(path, PROTO.replace(" - протокол.docx", ".mp4"),
                                segs, analysis, duration=120.0, date_str="02.09.2026")
    return path


def _archive(tmp_path: Path) -> Path:
    root = tmp_path / "Телемост-записи"
    _make_docx(root / "CRM - проектная встреча" / PROTO)
    (root / "CRM - проектная встреча" / PROTO.replace(" - протокол.docx", ".mp4")).write_bytes(b"x")
    _make_docx(root / "Разовые встречи" / "03.09.2026, 10-00. - Встреча с юристом - протокол.docx")
    (root / "заметки.docx").write_bytes(b"x")                 # не протокол
    (root / "Встреча лидеров - протокол.docx").write_bytes(b"x")  # без даты
    return root


class TestCollect:
    def test_only_dated_protocols_from_root_and_series_folders(self, tmp_path):
        files, skipped = ia.collect(ia.Local(_archive(tmp_path)))
        assert [f["name"] for f in files] == [
            PROTO, "03.09.2026, 10-00. - Встреча с юристом - протокол.docx"]
        assert files[0]["day"] == "01.09.2026" and files[0]["key"]
        assert skipped == 3

    def test_only_filter(self, tmp_path):
        files, _ = ia.collect(ia.Local(_archive(tmp_path)), only="Встреча с юристом")
        assert len(files) == 1 and "юристом" in files[0]["name"]


class TestPlan:
    def _files(self):
        return [{"name": PROTO, "path": "/a/" + PROTO, "day": "01.09.2026", "key": "crm"},
                {"name": "x", "path": "/a/x.docx", "day": "02.09.2026", "key": "crm"}]

    def test_skips_job_docs_and_already_indexed(self, monkeypatch):
        files = self._files()
        done_id = mi.archive_doc_id("alice", "/a/x.docx")
        monkeypatch.setattr(db, "chunks_docs", lambda team: [
            {"doc_id": "job-1", "series_key": "crm", "meeting_date": "01.09.2026"},
            {"doc_id": done_id, "series_key": "crm", "meeting_date": "02.09.2026"}])
        items = ia.plan("alice", files)
        assert items[0]["skip"] == "есть куски из задачи распознавания"
        assert items[1]["skip"] == "уже в индексе"
        assert ia.plan("alice", files, force=True)[1]["skip"] == ""

    def test_fresh_archive_is_all_todo(self, monkeypatch):
        monkeypatch.setattr(db, "chunks_docs", lambda team: [])
        items = ia.plan("alice", self._files())
        assert all(not i["skip"] for i in items)
        assert items[0]["doc_id"].startswith(mi.ARCHIVE_PREFIX)


class TestIndexOne:
    def test_rows_go_to_db_with_team_and_doc_id(self, tmp_path, monkeypatch):
        root = _archive(tmp_path)
        saved = {}
        monkeypatch.setattr(db, "chunks_replace",
                            lambda doc_id, rows: saved.update({doc_id: rows}) or len(rows))
        files, _ = ia.collect(ia.Local(root), only="CRM - проектная встреча")
        item = dict(files[0], doc_id="archive:test", skip="")
        n = ia.index_one("alice", item, ia.Local(root), tmp_path)
        rows = saved["archive:test"]
        assert n == len(rows) > 0
        assert {r["team"] for r in rows} == {"alice"}
        assert {r["kind"] for r in rows} == {mi.KIND_PROTOCOL, mi.KIND_TRANSCRIPT}
        assert all(r["meeting_date"] == "01.09.2026" for r in rows)
        assert any("про ОД договорились" in r["text"] for r in rows)
        assert rows[0]["source"].endswith(PROTO)

    def test_draft_flag_from_document(self, tmp_path, monkeypatch):
        path = _make_docx(tmp_path / PROTO, draft=True)
        saved = {}
        monkeypatch.setattr(db, "chunks_replace",
                            lambda doc_id, rows: saved.update({doc_id: rows}) or len(rows))
        item = {"name": PROTO, "path": str(path), "day": "01.09.2026", "key": "crm",
                "doc_id": "archive:d", "skip": ""}
        ia.index_one("alice", item, ia.Local(tmp_path), tmp_path)
        assert all(r["is_draft"] for r in saved["archive:d"])

    def test_foreign_docx_rejected(self, tmp_path, monkeypatch):
        from docx import Document
        d = Document(); d.add_paragraph("чужой документ"); d.save(str(tmp_path / PROTO))
        monkeypatch.setattr(db, "chunks_replace", lambda *a: 0)
        item = {"name": PROTO, "path": str(tmp_path / PROTO), "day": "01.09.2026",
                "key": "crm", "doc_id": "archive:f", "skip": ""}
        try:
            ia.index_one("alice", item, ia.Local(tmp_path), tmp_path)
            raise AssertionError("чужой документ должен быть отвергнут")
        except ValueError as e:
            assert "не наш протокол" in str(e)


class TestMain:
    def test_needs_postgres(self, capsys):
        assert ia.main(["--user", "alice"]) == 2
        assert "Postgres" in capsys.readouterr().out


class TestYandexClient:
    def test_list_entries_returns_dirs_and_files(self, monkeypatch):
        def rj(method, url, **kw):
            assert kw["params"]["path"] == "disk:/Записи"
            return 200, {"_embedded": {"total": 2, "items": [
                {"name": "CRM", "path": "disk:/Записи/CRM", "type": "dir"},
                {"name": "a.docx", "path": "disk:/Записи/a.docx", "type": "file"}]}}
        monkeypatch.setattr(yandex_disk, "request_json", rj)
        out = yandex_disk.list_entries("disk:/Записи", {"token": "t"})
        assert [(o["name"], o["type"]) for o in out] == [("CRM", "dir"), ("a.docx", "file")]

    def test_download_two_steps(self, monkeypatch, tmp_path):
        calls = []
        def rj(method, url, **kw):
            calls.append(("json", url.split("/v1/disk")[-1], kw.get("params")))
            return 200, {"href": "https://downloader.disk.yandex.ru/x"}
        def rq(method, url, **kw):
            calls.append(("raw", url))
            return 200, b"PK-docx"
        monkeypatch.setattr(yandex_disk, "request_json", rj)
        monkeypatch.setattr(yandex_disk, "request", rq)
        dest = yandex_disk.download("disk:/Записи/a.docx", {"token": "w", "read_token": "r"},
                                    tmp_path / "a.docx")
        assert dest.read_bytes() == b"PK-docx"
        assert calls[0][1] == "/resources/download" and calls[0][2]["path"] == "disk:/Записи/a.docx"
        assert calls[1] == ("raw", "https://downloader.disk.yandex.ru/x")

    def test_download_404_is_named(self, monkeypatch, tmp_path):
        monkeypatch.setattr(yandex_disk, "request_json", lambda *a, **k: (404, {}))
        try:
            yandex_disk.download("disk:/нет.docx", {"token": "t"}, tmp_path / "x")
            raise AssertionError("ожидалась CloudError")
        except yandex_disk.CloudError as e:
            assert "нет файла" in str(e)
