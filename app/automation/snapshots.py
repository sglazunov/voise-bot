"""Снапшоты состояния встреч: пережить перезапуск сервиса.

Вынесено из scheduler.py. Это чистое хранилище: снапшот собирается из
MeetingState и кладётся либо в Postgres, либо в файл команды — состояния
планировщика тут нет, поэтому и держать это внутри класса незачем.

Почему вообще снапшоты: перезапуск стирает состояние в памяти, и встреча,
которую предыдущий процесс уже записал, вернулась бы в список как «пропущена».
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .. import db, logs, security

log = logs.get("vtx.snapshots")


def path_for(user: str) -> Path:
    return security.user_dir(user) / "meetings.json"


def load(user: str) -> dict:
    try:
        if db.enabled():
            return db.meetings_load(user)
        return json.loads(path_for(user).read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


# Файловый режим пишет снапшоты «прочитать всё → изменить одну запись →
# записать всё». Это делают одновременно поток записи, поздняя выгрузка в
# облако и сохранение заметок из интерфейса — без лока они затирали правки
# друг друга. На Postgres лок не нужен: пишется ровно одна строка.
_snap_lock = threading.Lock()


def of_state(st) -> dict:
    return {"state": st.state, "detail": st.detail,
            "job_id": st.job_id, "cloud_url": st.cloud_url,
            "out_path": st.out_path,
            "live_notes": st.live_notes,
            "do_protocol": st.do_protocol,
            # Исход записи. Раньше в снапшот не попадал НИ ОДИН из трёх: после
            # перезапуска нельзя было узнать ни почему остановилась запись, ни
            # что она не уехала в облако. Поздняя дозагрузка при этом уже
            # работала — по полю, которого в снапшоте не было.
            "upload_error": st.upload_error,
            "stop_reason": st.stop_reason,
            "rec_bytes": int(getattr(st, "rec_bytes", 0) or 0),
            "join_delay_sec": getattr(st, "join_delay_sec", None),
            "saved_at": time.time()}


def save(st) -> None:
    if db.enabled():
        # Одна строка по ключу. Раньше здесь читались ВСЕ встречи команды и
        # записывались обратно целиком, причём meetings_save удаляет ключи,
        # которых нет в переданном словаре: два потока, сохранявшие разные
        # встречи, стирали работу друг друга.
        try:
            db.meeting_upsert(st.owner, st.key, of_state(st))
            # Обрезка до 200 последних: в файловом режиме она делается при
            # каждой записи, здесь — только на завершении встречи, чтобы не
            # гонять DELETE на каждое обновление статуса.
            if st.state in ("done", "error"):
                db.meetings_trim(st.owner)
        except Exception:   # noqa: BLE001 — снапшот не должен ронять запись
            log.warning("Снапшот встречи %s не сохранён", st.key, exc_info=True)
        return
    with _snap_lock:
        _save_to_file(st)


def _save_to_file(st) -> None:
    try:
        snaps = load(st.owner)
        snaps[st.key] = of_state(st)
        if len(snaps) > 200:  # keep the newest 200
            for k in sorted(snaps, key=lambda k: snaps[k].get("saved_at", 0))[:-200]:
                snaps.pop(k, None)
        p = path_for(st.owner)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(snaps, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:  # persistence is best-effort, never breaks the loop
        log.warning("Снапшот встречи %s не записан в файл", st.key, exc_info=True)
