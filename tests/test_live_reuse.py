"""Живая расшифровка как основа итога + второй воркер очереди (2026-10-02).

Было: по ходу встречи куски распознавались каждые 5 минут только для окна
«живая расшифровка», а после встречи задача распознавала файл с нуля — час на
часовую встречу. Теперь куски копятся в `<запись>.live.json`, задача берёт их
и дораспознаёт только хвост. Второй воркер — на 8 ядрах / 12 ГБ две встречи
подряд считаются параллельно, у каждого воркера свой экземпляр модели.
"""
import json
import time
from pathlib import Path

import pytest

from app import config, jobs, transcribe
from app.automation.scheduler import Scheduler
from app.transcribe import Segment, trim_chunk_edge


def _seg(a, b, text="x"):
    return Segment(start=a, end=b, text=text)


class TestКрайКуска:
    def test_сегмент_у_края_отбрасывается_и_покрытие_отступает(self):
        segs = [_seg(0, 10, "a"), _seg(10, 20, "b"), _seg(20, 29.5, "обрыв")]
        kept, until = trim_chunk_edge(segs, chunk_end=30.0)
        assert [s.text for s in kept] == ["a", "b"]
        assert until == 20.0, "следующий кусок начнётся с оборванного сегмента"

    def test_далеко_от_края_покрыто_до_конца(self):
        segs = [_seg(0, 10, "a"), _seg(10, 20, "b")]
        kept, until = trim_chunk_edge(segs, chunk_end=30.0)
        assert len(kept) == 2 and until == 30.0

    def test_единственный_сегмент_у_края_не_теряется(self):
        """Один длинный сегмент до края: отбросить — значит потерять всё и
        вечно топтаться на месте; оставляем и идём дальше."""
        segs = [_seg(0, 29.8, "монолог")]
        kept, until = trim_chunk_edge(segs, chunk_end=30.0)
        assert len(kept) == 1 and until == 30.0


class TestФайлЖивойРасшифровки:
    def test_load_live(self, tmp_path):
        assert jobs.load_live("") is None
        assert jobs.load_live(str(tmp_path / "нет.json")) is None
        bad = tmp_path / "bad.json"; bad.write_text("{", encoding="utf-8")
        assert jobs.load_live(str(bad)) is None
        empty = tmp_path / "e.json"; empty.write_text('{"until": 5, "segments": []}')
        assert jobs.load_live(str(empty)) is None
        ok = tmp_path / "ok.json"
        ok.write_text(json.dumps({"until": 120.0, "segments": [
            {"start": 1.0, "end": 2.0, "text": "привет"}]}), encoding="utf-8")
        assert jobs.load_live(str(ok))["until"] == 120.0

    def test_путь_живой_расшифровки_рядом_с_записью(self):
        assert Scheduler.live_path_for("/d/rec.mp4") == "/d/rec.mp4.live.json"


class TestЗадачаДораспознаётХвост:
    @pytest.fixture
    def rec(self, tmp_path, monkeypatch):
        media = tmp_path / "встреча.mp4"
        media.write_bytes(b"\x00" * 1024)
        live = tmp_path / "встреча.mp4.live.json"
        live.write_text(json.dumps({"until": 120.0, "segments": [
            {"start": 1.0, "end": 4.0, "text": "начало встречи", "avg_logprob": -0.2},
            {"start": 60.0, "end": 64.0, "text": "середина"}]}, ensure_ascii=False),
            encoding="utf-8")
        calls = []

        def fake_transcribe(path, language=None, on_segment=None, on_start=None,
                            initial_prompt=None, model_name=None, nonblocking=False,
                            offset_sec=0.0):
            calls.append({"path": path, "offset": offset_sec})
            if on_start:
                on_start()
            seg = Segment(start=125.0 + offset_sec - 120.0, end=130.0 + offset_sec - 120.0,
                          text="хвост")
            # как настоящий transcribe_file: времена уже со сдвигом
            seg = Segment(start=offset_sec + 5.0, end=offset_sec + 10.0, text="хвост")
            if on_segment:
                on_segment(seg, 180.0)
            return [seg], {"language": "ru", "duration": 180.0, "model": "medium"}

        monkeypatch.setattr(jobs, "transcribe_file", fake_transcribe)
        monkeypatch.setattr(jobs, "_media_duration", lambda p: 180.0)
        tail = tmp_path / "встреча.mp4.tail.wav"
        tail.write_bytes(b"RIFF")
        monkeypatch.setattr(jobs, "_cut_wav", lambda p, start: str(tail))
        return media, live, calls

    def test_хвост_со_сдвигом_и_склейка(self, rec):
        media, live, calls = rec
        job = jobs.store.create(filename=media.name, audio_path=str(media),
                                language="ru", diarize=False, owner="alice",
                                live_path=str(live))
        jobs.store._queue.get(timeout=5)         # не отдаём задачу воркеру
        jobs.store._process(job)
        assert job.status == jobs.STATUS_DONE, job.error
        assert calls and calls[0]["offset"] == 120.0
        assert calls[0]["path"].endswith(".tail.wav"), "распознаётся только хвост"
        txt = jobs.store.result_path(job.id, "txt").read_text(encoding="utf-8")
        assert "начало встречи" in txt and "середина" in txt and "хвост" in txt
        assert txt.index("середина") < txt.index("хвост")
        meta = json.loads(jobs.store.result_path(job.id, "json").read_text(encoding="utf-8"))
        assert meta.get("meta", meta).get("live_reused_sec") == 120.0

    def test_без_файла_живой_расшифровки_полный_прогон(self, rec):
        media, live, calls = rec
        job = jobs.store.create(filename=media.name, audio_path=str(media),
                                language="ru", diarize=False, owner="alice")
        jobs.store._queue.get(timeout=5)
        jobs.store._process(job)
        assert job.status == jobs.STATUS_DONE, job.error
        assert calls[0]["offset"] == 0.0 and calls[0]["path"] == str(media)

    def test_покрытие_не_сходится_с_файлом_полный_прогон(self, rec, monkeypatch):
        media, live, calls = rec
        monkeypatch.setattr(jobs, "_media_duration", lambda p: 60.0)   # файл короче «покрытия»
        job = jobs.store.create(filename=media.name, audio_path=str(media),
                                language="ru", diarize=False, owner="alice",
                                live_path=str(live))
        jobs.store._queue.get(timeout=5)
        jobs.store._process(job)
        assert job.status == jobs.STATUS_DONE, job.error
        assert calls[0]["path"] == str(media) and calls[0]["offset"] == 0.0

    def test_хвост_не_вырезался_полный_прогон(self, rec, monkeypatch):
        media, live, calls = rec
        monkeypatch.setattr(jobs, "_cut_wav", lambda p, start: None)
        job = jobs.store.create(filename=media.name, audio_path=str(media),
                                language="ru", diarize=False, owner="alice",
                                live_path=str(live))
        jobs.store._queue.get(timeout=5)
        jobs.store._process(job)
        assert job.status == jobs.STATUS_DONE, job.error
        assert calls[-1]["path"] == str(media), "откат на полную расшифровку"


class TestВоркерыИПулМоделей:
    def test_число_воркеров_из_конфига(self):
        assert jobs.store.workers == max(1, config.JOB_WORKERS)
        assert transcribe._POOL_SIZE == max(1, config.JOB_WORKERS)
        assert len(transcribe._SLOTS) == transcribe._POOL_SIZE

    def test_второй_слот_с_половиной_потоков(self):
        s0 = transcribe._Slot(0); s1 = transcribe._Slot(1)
        assert s0.threads == config.CPU_THREADS
        assert s1.threads == max(2, config.CPU_THREADS // 2)

    def test_авто_выбор_по_железу(self, monkeypatch):
        monkeypatch.setattr(config, "_mem_total_gb", lambda: 12.0)
        monkeypatch.setattr(config.os, "cpu_count", lambda: 8)
        assert config._auto_workers() == 2
        monkeypatch.setattr(config, "_mem_total_gb", lambda: 7.7)
        assert config._auto_workers() == 1
        monkeypatch.setattr(config, "_mem_total_gb", lambda: 12.0)
        monkeypatch.setattr(config.os, "cpu_count", lambda: 4)
        assert config._auto_workers() == 1

    def test_слоты_выдаются_и_возвращаются(self):
        got = [transcribe._acquire_slot(blocking=False) for _ in range(transcribe._POOL_SIZE)]
        assert all(got) and len({g.index for g in got}) == len(got)
        assert transcribe._acquire_slot(blocking=False) is None
        for g in got:
            transcribe._release_slot(g)
        again = transcribe._acquire_slot(blocking=False)
        assert again is not None
        transcribe._release_slot(again)
