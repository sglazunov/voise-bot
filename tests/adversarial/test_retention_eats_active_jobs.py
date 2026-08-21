"""Атака на ретеншн: очистка не смотрит на статус задачи.

`JobStore._purge_old()` (app/jobs.py:735) отбирает задачи по возрасту
`finished_at or created_at` и НЕ проверяет статус. Воркер распознавания один,
очередь общая, ретеншн по умолчанию 24 часа (VTX_RETENTION_HOURS) — задача,
простоявшая в очереди сутки, попадает под нож ВМЕСТЕ С ИСХОДНИКОМ.

Для записи встречи исходник — это сама запись (data/users/<команда>/recordings).
Если выгрузка в облако не прошла (её ретраит `_late_upload`, у которого срок
4 часа), встреча исчезает целиком: файла нет, задачи нет, повторить нечем.
"""
from __future__ import annotations

import time

import pytest

from app import config
from app.jobs import (STATUS_ANALYZING, STATUS_QUEUED, STATUS_RUNNING, STATUS_DONE, Job,
                      store)

# Окно ретеншна фиксируем сами. Оно берётся из VTX_RETENTION_HOURS, и на боевом
# сервере стоит своё значение: при окне больше 25 часов «старая» задача просто
# не доходила до очистки, и тест либо падал, либо проходил не по той причине.
RETENTION_HOURS = 24


@pytest.fixture(autouse=True)
def _fixed_retention(monkeypatch):
    monkeypatch.setattr(config, "RESULT_RETENTION_HOURS", RETENTION_HOURS)


def _make_job(status: str, age_hours: float = RETENTION_HOURS + 1) -> Job:
    src = config.UPLOAD_DIR / f"meeting-{status}.mp4"
    src.write_bytes(b"\x00" * 1024)          # «запись встречи»
    job = Job(id=f"adv{status[:6]}01", filename=src.name, audio_path=str(src),
              language="ru", diarize=False, owner="alice", status=status,
              created_at=time.time() - age_hours * 3600)
    store._jobs[job.id] = job
    return job


class TestRetentionVsActiveJobs:
    def test_queued_job_keeps_its_source(self):
        """Задача сутки ждала свободного воркера (три встречи подряд — обычное
        дело). Ретеншн не имеет права трогать то, что ещё не обработано."""
        job = _make_job(STATUS_QUEUED, age_hours=RETENTION_HOURS + 1)
        src = config.UPLOAD_DIR / job.filename

        store._purge_old()

        assert store.get(job.id) is not None, "задача из очереди удалена ретеншном"
        assert src.exists(), "ретеншн стёр исходник задачи, которая ещё в очереди"

    def test_running_job_keeps_its_source(self):
        """Ещё хуже: воркер прямо сейчас читает этот файл."""
        job = _make_job(STATUS_RUNNING, age_hours=RETENTION_HOURS + 1)
        src = config.UPLOAD_DIR / job.filename

        store._purge_old()

        assert store.get(job.id) is not None, "идущая задача удалена ретеншном"
        assert src.exists(), "ретеншн стёр файл, который читает воркер"

    def test_purged_job_is_not_resurrected_in_the_database(self, monkeypatch):
        """Второй виток той же гонки, но уже на Postgres.

        `_save(job)` пишет ОДНУ задачу (`db.job_upsert`). Ссылку на `Job`
        держат не только воркер, но и поток доставки протокола и «Пересобрать»:
        любой из них своим следующим `_set(...)` заново ВСТАВЛЯЕТ строку задачи,
        которой в памяти уже нет, а файлы которой стёрты. После перезапуска
        `jobs_load()` поднимет её в список: карточка «Готово», а скачивание
        любого формата — 404.

        Сценарий взят на ЗАВЕРШЁННОЙ задаче: незавершённые ретеншн больше не
        трогает (см. тесты выше), но завершённую он удаляет штатно — а поток
        доставки в этот момент вполне может быть ещё жив.
        """
        from app import db

        rows: dict[str, dict] = {}
        monkeypatch.setattr(db, "enabled", lambda: True)
        monkeypatch.setattr(db, "job_upsert", lambda d: rows.__setitem__(d["id"], dict(d)))
        monkeypatch.setattr(db, "jobs_save", lambda lst: (
            rows.clear() or [rows.__setitem__(d["id"], dict(d)) for d in lst]))
        monkeypatch.setattr(db, "search_delete", lambda jid: None)
        monkeypatch.setattr(db, "search_save", lambda *a, **k: None)
        monkeypatch.setattr(db, "stats_add", lambda r: None)

        job = _make_job(STATUS_DONE, age_hours=RETENTION_HOURS + 1)
        job.finished_at = time.time() - (RETENTION_HOURS + 1) * 3600
        job.id = "advzombie1"
        store._jobs = {job.id: job}
        store._save(job)
        assert job.id in rows

        store._purge_old()
        assert job.id not in rows and job.id not in store._jobs   # чистка отработала

        # …а поток доставки всё ещё жив и «дописывает» свою задачу.
        store._set(job, delivery_error="")

        assert job.id not in rows, (
            "удалённая ретеншном задача воскресла в базе без единого файла — "
            "после перезапуска она вернётся в список как «Готово»")

    def test_analyzing_job_keeps_its_transcript(self):
        """Распознавание позади, идёт сборка протокола: расшифровка на диске —
        единственное, из чего протокол ещё можно собрать."""
        job = _make_job(STATUS_ANALYZING, age_hours=RETENTION_HOURS + 1)
        txt = store.result_path(job.id, "txt")
        txt.write_text("[00:01] разговор был\n", encoding="utf-8")

        store._purge_old()

        assert txt.exists(), "ретеншн стёр расшифровку задачи на этапе анализа"
        assert store.get(job.id) is not None
