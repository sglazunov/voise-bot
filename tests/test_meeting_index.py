"""Куски встреч (app/meeting_index.py): нарезка расшифровки и протокола,
строки для таблицы, разбор архивного .docx нашего же экспорта."""
from pathlib import Path

from app import docx_export, meeting_index as mi


def _transcript(n: int = 100) -> str:
    return "\n".join(f"[00:{i // 60:02d}:{i % 60:02d}] реплика номер {i}" for i in range(n))


class TestChunkTranscript:
    def test_windows_overlap_and_carry_first_timecode(self):
        chunks = mi.chunk_transcript(_transcript(100))
        assert [c["idx"] for c in chunks] == list(range(len(chunks)))
        assert chunks[0]["ts"] == "00:00:00"
        assert chunks[1]["ts"] == "00:00:20"          # шаг — половина окна
        assert "реплика номер 39" in chunks[0]["text"]  # окно 40 строк
        assert "реплика номер 40" not in chunks[0]["text"]
        # Хвост не теряется и не дублируется лишними пустыми окнами.
        assert "реплика номер 99" in chunks[-1]["text"]
        assert len(chunks) == 4

    def test_empty_and_short(self):
        assert mi.chunk_transcript("") == []
        assert mi.chunk_transcript("\n\n") == []
        one = mi.chunk_transcript("[12:34] одна реплика")
        assert len(one) == 1 and one[0]["ts"] == "12:34"


class TestProtocolChunks:
    def test_sections_in_order(self):
        a = {"summary": "Про сайт.",
             "decisions": ["Снять заглушку"],
             "tasks": [{"task": "Сделать форму", "owner": "Зоя Р", "due": "пятница"}],
             "minor_tasks": [{"task": "Поправить шрифт"}],
             "done_tasks": [{"task": "Собрать макет", "owner": "Наталья П"}],
             "statuses": [{"item": "баг с ролями", "status": "в работе", "note": "в спринте"}],
             "detailed": [{"topic": "Дизайн", "details": "Обсудили сетку."}]}
        chunks = mi.protocol_chunks(a)
        texts = [c["text"] for c in chunks]
        assert texts[0].startswith("О чём шёл разговор: Про сайт.")
        assert "Решения" in texts[1] and "Снять заглушку" in texts[1]
        assert "Сделать форму — Зоя Р (срок: пятница)" in texts[2]
        assert "Поправить шрифт" in texts[2]
        assert "Сделано" in texts[3] and "Наталья П" in texts[3]
        assert "баг с ролями: в работе (в спринте)" in texts[4]
        assert texts[5] == "Тема: Дизайн\nОбсудили сетку."
        assert [c["idx"] for c in chunks] == list(range(6))

    def test_empty_analysis(self):
        assert mi.protocol_chunks(None) == []
        assert mi.protocol_chunks({}) == []


class TestDocRows:
    def test_rows_carry_identity_and_unique_ids(self):
        rows = mi.doc_rows("job1", "alice", title="Встреча лидеров",
                           series_key="встреча лидеров", meeting_date="21.10.2026",
                           meeting_ts=1_700_000_000.0,
                           transcript=_transcript(50),
                           analysis={"summary": "итог", "decisions": ["раз"]},
                           is_draft=True, source="job:job1")
        ids = [r["id"] for r in rows]
        assert len(ids) == len(set(ids))
        kinds = {r["kind"] for r in rows}
        assert kinds == {mi.KIND_PROTOCOL, mi.KIND_TRANSCRIPT}
        for r in rows:
            assert r["team"] == "alice" and r["doc_id"] == "job1"
            assert r["is_draft"] is True and r["meeting_date"] == "21.10.2026"
        proto = [r for r in rows if r["kind"] == mi.KIND_PROTOCOL]
        assert proto[0]["id"] == "job1:p0"

    def test_meeting_ts_from_date(self):
        ts = mi.meeting_ts_of("21.10.2026")
        assert ts and abs(ts - mi.meeting_ts_of("22.10.2026")) == 86400
        assert mi.meeting_ts_of("мусор", fallback=5.0) == 5.0

    def test_archive_doc_id_is_stable_per_team_and_path(self):
        a = mi.archive_doc_id("alice", "disk:/Телемост-записи/x.docx")
        assert a == mi.archive_doc_id("alice", "disk:/Телемост-записи/x.docx")
        assert a != mi.archive_doc_id("bob", "disk:/Телемост-записи/x.docx")
        assert a.startswith(mi.ARCHIVE_PREFIX)


class TestParseDocx:
    def _make(self, tmp_path: Path, analysis: dict) -> Path:
        out = tmp_path / "01.09.2026, 11-30. - CRM - проектная встреча - протокол.docx"
        segments = [{"start": 0.0, "text": "Начинаем встречу", "speaker": "Зоя Р"},
                    {"start": 170.0, "text": "по ОД договорились на пятницу", "speaker": "Иван"},
                    {"start": 3700.0, "text": "на этом всё", "speaker": None}]
        docx_export.generate_report(out, "01.09.2026, 11-30. - CRM - проектная встреча.mp4",
                                    segments, analysis, duration=3720.0,
                                    date_str="02.09.2026")
        return out

    def _analysis(self, **extra) -> dict:
        a = {"participants": [{"name": "Зоя Р", "role": "менеджер"},
                              {"name": "Иван", "role": "разработчик"}],
             "summary": "Встреча про CRM и дизайн.",
             "decisions": ["Исключить разработчиков из дизайн-сессий"],
             "tasks": [{"task": "Найти тестировщика", "owner": "Зоя Р", "due": "сегодня"},
                       {"task": "Без хозяина", "owner": ""}],
             "done_tasks": [{"task": "Собрать макет", "owner": "Наталья П"}],
             "detailed": [{"topic": "Дизайн CRM", "details": "Пересмотрели формат сессий."}],
             "key_thoughts": [], "conclusions": [], "minor_tasks": []}
        a.update(extra)
        return a

    def test_parses_sections_and_transcript(self, tmp_path):
        path = self._make(tmp_path, self._analysis())
        p = mi.parse_protocol_docx(path)
        assert p["ours"] is True and p["is_draft"] is False
        assert p["title"].startswith("01.09.2026, 11-30. - CRM")
        assert p["date"] == "02.09.2026"
        assert any("Зоя Р" in x for x in p["participants"])
        assert p["summary"] == "Встреча про CRM и дизайн."
        assert p["decisions"] == ["Исключить разработчиков из дизайн-сессий"]
        tasks = {t["task"]: t for t in p["tasks"]}
        assert tasks["Найти тестировщика"]["owner"] == "Зоя Р"
        assert tasks["Найти тестировщика"]["due"] == "сегодня"
        assert "Без хозяина" in tasks                  # из блока «назначьте исполнителя»
        assert p["done"] == [{"task": "Собрать макет", "owner": "Наталья П", "due": ""}]
        assert p["topics"] == [{"topic": "Дизайн CRM", "details": "Пересмотрели формат сессий."}]
        lines = p["transcript"].splitlines()
        assert lines[0] == "[00:00] Зоя Р: Начинаем встречу"
        assert lines[1] == "[02:50] Иван: по ОД договорились на пятницу"
        assert lines[2] == "[01:01:40] на этом всё"

    def test_draft_mark_is_read_from_heading(self, tmp_path):
        path = self._make(tmp_path, self._analysis(_fallback=["gemini: 429"]))
        p = mi.parse_protocol_docx(path)
        assert p["is_draft"] is True

    def test_parsed_feeds_protocol_chunks(self, tmp_path):
        path = self._make(tmp_path, self._analysis())
        p = mi.parse_protocol_docx(path)
        chunks = mi.protocol_chunks(mi.analysis_from_parsed(p))
        joined = "\n".join(c["text"] for c in chunks)
        assert "Найти тестировщика — Зоя Р (срок: сегодня)" in joined
        assert "Тема: Дизайн CRM" in joined
        rows = mi.doc_rows("archive:x", "alice", title=p["title"], series_key="crm",
                           meeting_date="01.09.2026", meeting_ts=None,
                           transcript=p["transcript"],
                           analysis=mi.analysis_from_parsed(p), is_draft=p["is_draft"])
        assert any(r["kind"] == mi.KIND_TRANSCRIPT and r["ts"] == "00:00" for r in rows)


class TestIndexJobFileMode:
    def test_without_database_nothing_happens(self):
        from app.jobs import store
        job = store.create(filename="m.mp4", audio_path="/tmp/m.mp4",
                           language="ru", diarize=False, owner="alice")
        assert mi.index_job(store, job) == 0
