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


def wipe_stale_links(task_id, cfg: dict, log, task: dict | None = None) -> dict:
    """Снять с задачи ссылки на видео и протокол ПРОШЛОГО проведения.

    Зовётся до встречи, а не в момент старта записи (так было раньше): к
    прошлым ссылкам чаще всего обращаются как раз в начале новой встречи.

    `task` — уже прочитанная задача (`Meeting.raw` из опроса). С ней проход по
    задаче с пустыми полями не стоит НИ ОДНОГО запроса к Weeek: раньше каждое
    поле перечитывало задачу заново, то есть четыре запроса на пустую работу.

    Возвращается отчёт `{ok, cleared[], errors[]}`, а не None: вызывающий
    должен знать, что очистка не удалась, — иначе человек и дальше видит в
    будущей задаче ссылки прошлой встречи, а мы считаем, что убрали их.
    """
    token = cfg.get("weeek_token")
    protected = cfg.get("weeek_protected_fields")
    cleared: list[str] = []
    errors: list[str] = []
    for opt, fname in (("weeek_set_video_field", "weeek_video_field"),
                       ("weeek_set_protocol_field", "weeek_protocol_field")):
        fld = (cfg.get(fname) or "").strip()
        # Поле, запись в которое выключена настройкой, не чистится тоже: раз мы
        # туда не пишем, то и лежит там не наше.
        if not (cfg.get(opt, True) and fld and token):
            continue
        try:
            if task is None:
                # Одно чтение на оба поля, а не по чтению на каждое.
                task = weeek.get_task(token, task_id)
            res = weeek.clear_custom_field(token, task_id, fld, task=task,
                                           protected=protected)
            if res.get("ok") and not res.get("skipped"):
                cleared.append(fld)
                log(f"Поле «{fld}»: снята ссылка прошлого проведения.")
                _LOG.info("Задача %s: поле «%s» очищено (было «%s»)",
                          task_id, fld, str(res.get("was"))[:120])
            elif not res.get("ok"):
                errors.append(f"{fld}: {res.get('error')}")
                _LOG.warning("Задача %s: поле «%s» не очищено — %s",
                             task_id, fld, res.get("error"))
        except Exception as e:  # noqa: BLE001 — сбой одного поля не рвёт проход
            errors.append(f"{fld}: {e}")
            _LOG.info("Не удалось очистить поле «%s» задачи %s",
                      fld, task_id, exc_info=True)
    return {"ok": not errors, "cleared": cleared, "errors": errors}


def write_weeek_field(token, task_id, field: str, value: str,
                       log, attempts: int = 3, protected=None) -> bool:
    """Write a link into a Weeek custom field, retrying — the link for THIS
    meeting must actually land, not silently stay last week's."""
    err = None
    for i in range(attempts):
        if i:
            time.sleep(10 * i)
        try:
            res = weeek.set_custom_field(token, task_id, field, value,
                                         protected=protected)
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": str(e)}
        if res.get("ok"):
            log(f"Поле «{field}» в Weeek: заполнено ✓")
            return True
        err = res.get("error")
        if res.get("protected"):
            break   # защищённое поле повторами не пробить — только шум в логе
    log(f"Поле «{field}» в Weeek: не удалось — {err}")
    return False


# -- recording delivery: the local file is only a staging copy ----------
def _needs_link(up: dict) -> bool:
    """Загружено в удалённое облако, но ссылки нет — людям файл недоступен."""
    return bool(up.get("ok")) and up.get("backend") != "local" and not up.get("url")


def upload_with_retry(out: str, cfg: dict, log, attempts: int = 3) -> dict:
    """Upload the recording, retrying a few times with a pause — one network
    hiccup must not leave a meeting's video stranded on the server.

    ⚠️ «Загружено, но без публичной ссылки» — это НЕ успех (17.09: файл лёг на
    Диск, публикация упала, а карточка писала «локально» и никто не повторял).
    Такой исход возвращается как ok=False с причиной и путём на Диске; на
    следующей попытке публикуется уже лежащий файл, а не заливается заново."""
    last: dict = {}
    for i in range(attempts):
        if i:
            log(f"Облако: повтор выгрузки {i + 1}/{attempts}…")
            time.sleep(20 * i)
        try:
            if last.get("uploaded") and last.get("path"):
                res = clouds.publish(last["path"], cfg)
                last = (dict(last, ok=True, url=res["url"], error=None) if res.get("ok")
                        else dict(last, error="файл загружен в облако, но публичная ссылка "
                                  f"не получена: {res.get('error') or last.get('error')}"))
            else:
                last = clouds.upload(out, Path(out).name, cfg)
        except Exception as e:  # noqa: BLE001 — an uploader bug isn't fatal
            last = {"ok": False, "error": str(e)}
        if _needs_link(last):
            last = dict(last, ok=False, uploaded=True,
                        error=last.get("public_note")
                        or "файл загружен в облако, но публичная ссылка не получена")
        if last.get("ok"):
            return last
        log(f"Облако (попытка {i + 1}/{attempts}): {last.get('error')}")
    return last or {"ok": False, "error": "облако недоступно"}


def publish_or_upload(out: str, cfg: dict, cloud_path: str | None, log) -> dict:
    """Поздняя дозагрузка: если файл уже на Диске — только опубликовать
    (секунды), иначе — обычная выгрузка одной попыткой."""
    if cloud_path:
        try:
            res = clouds.publish(cloud_path, cfg)
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": str(e)}
        if res.get("ok"):
            return {"ok": True, "backend": res.get("backend"), "url": res.get("url"),
                    "path": cloud_path}
        log(f"Облако: повторная публикация не удалась — {res.get('error')}")
    return upload_with_retry(out, cfg, log, attempts=1)


def delivered_elsewhere(up: dict, out: str) -> bool:
    """True when the upload put the file somewhere OTHER than the staging
    path — then the local copy is redundant and must be deleted."""
    if not up.get("ok"):
        return False
    if up.get("backend") != "local":
        # Без публичной ссылки удалённая копия людям недоступна — локальную
        # держим до тех пор, пока поздняя дозагрузка не добудет ссылку.
        return bool(up.get("url"))
    try:
        return Path(up.get("path") or "").resolve() != Path(out).resolve()
    except OSError:
        return False
