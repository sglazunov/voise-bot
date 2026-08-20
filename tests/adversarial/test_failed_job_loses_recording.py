"""Атака: сбой распознавания стирает исходник, и «Повторить» уже невозможно.

`JobStore._process` (app/jobs.py:1123) в блоке `finally` безусловно зовёт
`_delete_source(job)`, если у задачи стоит `delete_audio_when_done`. Флаг ставит
планировщик (`scheduler.py:554`) записям встреч, которые ушли в облако, — и
ставит его ДО обработки. `finally` срабатывает и на ветке `except`, то есть у
УПАВШЕЙ задачи запись удаляется тоже.

Ровно эту ошибку уже чинили в `scheduler._late_upload` (см. CLAUDE.md: «удалял
локальную запись у любой задачи вне queued/running/paused/analyzing… Теперь
удаляется только у done»), но в самом воркере она осталась.

Итог для владельца: карточка предлагает «Повторить», кнопка отвечает «Исходный
файл больше недоступен — загрузите его заново», а загружать нечего.
"""
from __future__ import annotations

import time

import pytest

from app import config, jobs
from app.jobs import STATUS_ERROR, Job, store


def _job_with_flag(tmp_name: str = "meeting.mp4") -> Job:
    src = config.UPLOAD_DIR / tmp_name
    src.write_bytes(b"\x00" * 4096)
    job = Job(id="advkeep0001", filename=tmp_name, audio_path=str(src),
              language="ru", diarize=False, owner="alice",
              delete_audio_when_done=True, created_at=time.time())
    store._jobs[job.id] = job
    return job


class TestSourceSurvivesFailure:
    def test_source_is_kept_when_recognition_fails(self, monkeypatch):
        job = _job_with_flag()
        src = config.UPLOAD_DIR / job.filename

        def boom(*a, **kw):
            raise RuntimeError("движок распознавания упал на середине")

        monkeypatch.setattr(jobs, "transcribe_file", boom)
        store._process(job)

        assert job.status == STATUS_ERROR      # предпосылка сценария
        assert src.exists(), (
            "исходник удалён у УПАВШЕЙ задачи — повторить обработку нечем")

    def test_retry_is_possible_after_a_failure(self, monkeypatch):
        job = _job_with_flag("meeting2.mp4")

        def boom(*a, **kw):
            raise RuntimeError("движок распознавания упал на середине")

        monkeypatch.setattr(jobs, "transcribe_file", boom)
        store._process(job)

        # Кнопка «Повторить» на карточке встречи.
        store.retry(job.id)   # currently: ValueError «Исходный файл больше недоступен»
