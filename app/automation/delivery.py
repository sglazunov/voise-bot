"""Доставка результатов встречи: выгрузка записи и запись полей в Weeek.

Вынесено из scheduler.py. Ни одна из этих функций не трогает состояние
планировщика — им нужны только токен, идентификатор задачи и настройки, поэтому
методами класса они были зря.

Общая мысль всех трёх: сеть подводит, а результат встречи потерять нельзя.
Поэтому и выгрузка, и запись поля идут с повторами, а локальный файл считается
лишним только после того, как облако подтвердило приём.
"""
from __future__ import annotations

import time
from pathlib import Path

from .. import logs
from . import clouds, weeek

# Модульный логгер назван _LOG, а не log, ОСОЗНАННО: в этом модуле `log` —
# локальная функция журнала карточки встречи, и она перекрывала логгер.
# Обработчик ошибки, звавший log.warning, падал с AttributeError изнутри
# except — и уносил управление мимо спасательного кода.
_LOG = logs.get("vtx.delivery")


def wipe_stale_links(task_id, cfg: dict, log) -> None:
    """Blank the video/protocol link fields of the task when recording
    starts — a recurring task inherits LAST week's links otherwise."""
    token = cfg.get("weeek_token")
    for opt, fname in (("weeek_set_video_field", "weeek_video_field"),
                       ("weeek_set_protocol_field", "weeek_protocol_field")):
        fld = (cfg.get(fname) or "").strip()
        if not (cfg.get(opt, True) and fld and token):
            continue
        try:
            res = weeek.set_custom_field(token, task_id, fld, "")
            if res.get("ok"):
                log(f"Поле «{fld}»: очищено от прошлой встречи.")
        except Exception:  # cosmetic step — never blocks the recording
            _LOG.info("Не удалось очистить поле «%s» задачи %s",
                     fld, task_id, exc_info=True)


def write_weeek_field(token, task_id, field: str, value: str,
                       log, attempts: int = 3) -> bool:
    """Write a link into a Weeek custom field, retrying — the link for THIS
    meeting must actually land, not silently stay last week's."""
    err = None
    for i in range(attempts):
        if i:
            time.sleep(10 * i)
        try:
            res = weeek.set_custom_field(token, task_id, field, value)
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": str(e)}
        if res.get("ok"):
            log(f"Поле «{field}» в Weeek: заполнено ✓")
            return True
        err = res.get("error")
    log(f"Поле «{field}» в Weeek: не удалось — {err}")
    return False


# -- recording delivery: the local file is only a staging copy ----------
def upload_with_retry(out: str, cfg: dict, log, attempts: int = 3) -> dict:
    """Upload the recording, retrying a few times with a pause — one network
    hiccup must not leave a meeting's video stranded on the server."""
    last: dict = {}
    for i in range(attempts):
        if i:
            log(f"Облако: повтор выгрузки {i + 1}/{attempts}…")
            time.sleep(20 * i)
        try:
            last = clouds.upload(out, Path(out).name, cfg)
        except Exception as e:  # noqa: BLE001 — an uploader bug isn't fatal
            last = {"ok": False, "error": str(e)}
        if last.get("ok"):
            return last
        log(f"Облако (попытка {i + 1}/{attempts}): {last.get('error')}")
    return last or {"ok": False, "error": "облако недоступно"}


def delivered_elsewhere(up: dict, out: str) -> bool:
    """True when the upload put the file somewhere OTHER than the staging
    path — then the local copy is redundant and must be deleted."""
    if not up.get("ok"):
        return False
    if up.get("backend") != "local":
        return True
    try:
        return Path(up.get("path") or "").resolve() != Path(out).resolve()
    except OSError:
        return False
