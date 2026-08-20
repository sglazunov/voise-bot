"""Атака: «Пересобрать» на задаче, которую прямо сейчас обрабатывает воркер.

`JobStore.retry` (app/jobs.py:640) статус проверяет: задачу в
`queued/running/paused/analyzing` она честно отказывается трогать.
`JobStore.reanalyze` (app/jobs.py:344) — НЕ проверяет ничего, кроме наличия
файла расшифровки. А расшифровка на диске остаётся и от прошлого прогона:
`retry()` результаты не удаляет.

Что из этого выходит:
  * два прогона ИИ по одной задаче одновременно — при том, что `_reanalyze_lock`
    заведён именно ради «two LLM runs can't overlap»; воркера он не видит;
  * `_do_reanalyze` в конце ставит `status=done` — карточка показывает
    «готово», пока распознавание ещё идёт;
  * `_deliver_protocol` отрабатывает ДВАЖДЫ: две выгрузки протокола в облако и
    два комментария/записи поля в задаче Weeek;
  * какой из двух протоколов останется в `job.analysis` — решает случай.
"""
from __future__ import annotations

import time

import pytest

from app import analyze as analyze_mod
from app.jobs import (STATUS_ANALYZING, STATUS_RUNNING, Job, store)


def _job(status: str, jid: str) -> Job:
    job = Job(id=jid, filename="Планёрка.mp4", audio_path="/data/uploads/x.mp4",
              language="ru", diarize=False, owner="alice", analyze=True,
              status=status, created_at=time.time())
    store._jobs[job.id] = job
    store.result_path(job.id, "txt").write_text(
        "[00:01] прошлый прогон оставил расшифровку\n", encoding="utf-8")
    return job


def _wait_lock_free(timeout: float = 5.0) -> None:
    """Замок пересборки — общий на весь процесс. Ждём, пока поток предыдущего
    теста его отпустит, иначе следующий тест «пройдёт» по чужой причине."""
    end = time.time() + timeout
    while time.time() < end:
        if store._reanalyze_lock.acquire(blocking=False):
            store._reanalyze_lock.release()
            return
        time.sleep(0.05)
    raise AssertionError("замок пересборки не освободился")


@pytest.fixture(autouse=True)
def _stub_llm(monkeypatch):
    """Никакого движка: считаем только количество запусков сборки протокола."""
    runs: list[float] = []

    def fake(*a, **kw):
        runs.append(time.time())
        time.sleep(0.2)
        return {"summary": "протокол", "detailed": []}

    monkeypatch.setattr(analyze_mod, "analyze_transcript", fake)
    _wait_lock_free()
    yield runs
    _wait_lock_free()


def test_reanalyze_refuses_a_job_the_worker_is_analysing(_stub_llm):
    job = _job(STATUS_ANALYZING, "advdouble01")
    with pytest.raises(ValueError):
        store.reanalyze(job.id)
    time.sleep(0.5)
    assert not _stub_llm, "запущен второй прогон ИИ поверх работающего воркера"


def test_reanalyze_refuses_a_job_still_being_transcribed(_stub_llm):
    job = _job(STATUS_RUNNING, "advdouble02")
    with pytest.raises(ValueError):
        store.reanalyze(job.id)
    time.sleep(0.5)
    assert job.status == STATUS_RUNNING, (
        f"статус задачи подменён на «{job.status}», пока идёт распознавание")
