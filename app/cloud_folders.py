"""Папка встречи в облаке: видео и протокол одной серии — в одной подпапке.

Требования — docs/ТЗ-ПАПКИ-ВСТРЕЧ.md. Коротко:

* у повторяющейся встречи (серии) своя папка с её названием: «ОО»,
  «Виртуальный помощник», «CRM - проектная встреча»; внутри и видео, и
  протокол каждой встречи серии — имена файлов уже начинаются с даты;
* встреча, которой пока не было раньше, — в общей папке «Разовые встречи»;
* как только у названия появилась вторая встреча, оно становится серией:
  заводится папка, и файлы первой встречи переезжают туда из «Разовых»
  (только если ссылка на файл после переезда остаётся прежней — иначе файл
  возвращается на место: ссылки уже стоят в Weeek);
* человек решает сам на карточке серии («Контекст для ИИ» → «Повторяющиеся
  встречи»): «Авто» / «Своя папка» (можно задать имя) / «Разовые встречи».

Корень — папка записей выбранного облака («Папка для записи»). Отдельная
«Папка для протокола» используется только при выключенной раскладке.
"""
from __future__ import annotations

import re
import time

from . import logs, meeting_series

log = logs.get("vtx.folders")

ONEOFF_DEFAULT = "Разовые встречи"
_MAX_NAME = 80
# Символы, которые не переживут ни Диск, ни Windows-клиент синхронизации.
_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_name(name: str | None) -> str:
    """Имя папки, пригодное для любого облака: без слэшей и служебных знаков,
    без точек и пробелов по краям, не длиннее 80 символов."""
    s = _BAD.sub(" ", str(name or ""))
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:_MAX_NAME].rstrip(" .")


def enabled(cfg: dict | None) -> bool:
    return bool((cfg or {}).get("folders_by_series", True))


def oneoff_name(cfg: dict | None) -> str:
    return safe_name((cfg or {}).get("oneoff_folder")) or ONEOFF_DEFAULT


def _has_signals(entry: dict) -> bool:
    """Карточку серии завёл или настроил человек — значит, это серия."""
    return bool((entry.get("context") or "").strip()
                or (entry.get("preset") or "").strip()
                or (entry.get("priority") or "normal") != "normal"
                or entry.get("weeek_project_id")
                or (entry.get("folder") or "").strip())


def _other_occurrence(entry: dict, date: str) -> bool:
    """Была ли у этого названия встреча в ДРУГОЙ день (протокол в истории
    серии или файл, уже положенный в «Разовые»)."""
    dates = {str(h.get("date") or "") for h in (entry.get("history") or [])}
    dates |= {str(f.get("date") or "") for f in (entry.get("oneoff_files") or [])}
    dates.discard("")
    return bool(dates - {date})


def resolve(user: str, title: str | None, cfg: dict | None,
            date: str | None = None) -> dict:
    """Куда положить файлы встречи `title`.

    {"sub": имя подпапки ('' — раскладка выключена), "kind": "series" |
    "oneoff" | "flat", "key": ключ серии, "date": день встречи}."""
    date = date or meeting_series.date_from_title(title) or time.strftime("%d.%m.%Y")
    if not enabled(cfg):
        return {"sub": "", "kind": "flat", "key": "", "date": date}
    key, entry = meeting_series.find(user, title)
    oneoff = {"sub": oneoff_name(cfg), "kind": "oneoff", "key": key, "date": date}
    if not key:
        return oneoff
    entry = entry or {}
    mode = entry.get("folder_mode") or "auto"
    if mode == "oneoff":
        return oneoff
    if mode == "series" or _has_signals(entry) or _other_occurrence(entry, date):
        # Название карточки — если человек его писал; автоматическое название,
        # совпадающее с ключом (всё строчными), хуже названия самой встречи.
        card_title = entry.get("title") or ""
        if card_title == key:
            card_title = ""
        name = safe_name(entry.get("folder")) or safe_name(card_title) \
            or safe_name(meeting_series.display_title(title))
        if not name or name.lower() == oneoff["sub"].lower():
            return oneoff
        return {"sub": name, "kind": "series", "key": key, "date": date}
    return oneoff


def placed(user: str, title: str | None, place: dict, up: dict, kind: str) -> None:
    """Файл лёг в облако. Если в «Разовые» — запомнить путь: когда у названия
    появится вторая встреча, файл переедет в папку серии."""
    # «Загружено, но без ссылки» (uploaded) — файл тоже уже лежит в «Разовых».
    if (place.get("kind") != "oneoff" or not up.get("path")
            or not (up.get("ok") or up.get("uploaded"))):
        return
    try:
        meeting_series.note_oneoff_file(user, title, {
            "path": str(up["path"]), "backend": up.get("backend") or "",
            "date": place.get("date") or "", "kind": kind,
            "url": up.get("url") or ""})
    except Exception:  # noqa: BLE001 — раскладка не должна ронять доставку
        log.warning("Не запомнил файл разовой встречи «%s»", title, exc_info=True)


def adopt(user: str, title: str | None, place: dict, cfg: dict, logf=None) -> int:
    """Название стало серией — перевезти файлы прежних встреч из «Разовых» в
    папку серии. Возвращает, сколько файлов переехало. Ничего не роняет."""
    if place.get("kind") != "series":
        return 0
    try:
        _key, entry = meeting_series.find(user, title)
        pending = list((entry or {}).get("oneoff_files") or [])
        if not pending:
            return 0
        from .automation import clouds
        moved, keep = 0, []
        for f in pending:
            res = clouds.move_to_sibling(f, place["sub"], cfg)
            if res.get("ok"):
                moved += 1
            elif res.get("retry") and int(f.get("tries") or 0) < 2:
                keep.append(dict(f, tries=int(f.get("tries") or 0) + 1))
            else:
                log.warning("Файл %s остался в «Разовых»: %s", f.get("path"),
                            res.get("error"))
        meeting_series.set_oneoff_files(user, title, keep)
        if moved and logf:
            logf(f"Облако: «{place['sub']}» стала серией — {moved} файл(ов) прежних "
                 "встреч перенесено из разовых в её папку.")
        return moved
    except Exception:  # noqa: BLE001
        log.warning("Переезд файлов в папку серии «%s» сорвался", title, exc_info=True)
        return 0
