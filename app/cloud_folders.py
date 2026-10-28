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
from pathlib import Path

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


def _other_occurrence(entry: dict, date: str, extra_days=None) -> bool:
    """Была ли у этого названия встреча в ДРУГОЙ день (протокол в истории
    серии, файл, уже положенный в «Разовые», или — при раскладке архива —
    другой файл того же названия в облаке)."""
    dates = {str(d) for d in (extra_days or ())}
    dates |= {str(h.get("date") or "") for h in (entry.get("history") or [])}
    dates |= {str(f.get("date") or "") for f in (entry.get("oneoff_files") or [])}
    dates |= {str(d) for d in (entry.get("seen_dates") or [])}
    dates.discard("")
    return bool(dates - {date})


def resolve(user: str, title: str | None, cfg: dict | None,
            date: str | None = None, extra_days=None) -> dict:
    """Куда положить файлы встречи `title`.

    {"sub": имя подпапки ('' — раскладка выключена), "kind": "series" |
    "oneoff" | "flat", "key": ключ серии, "date": день встречи}."""
    date = date or meeting_series.meeting_day(title)
    if not enabled(cfg):
        return {"sub": "", "kind": "flat", "key": "", "date": date}
    key, entry = meeting_series.find_exact(user, title)
    oneoff = {"sub": oneoff_name(cfg), "kind": "oneoff", "key": key, "date": date}
    if not key:
        return oneoff
    entry = entry or {}
    mode = entry.get("folder_mode") or "auto"
    if mode == "oneoff":
        return oneoff
    if mode == "series" or _has_signals(entry) or _other_occurrence(entry, date, extra_days):
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
    """Файл лёг в облако: запомнить день встречи у названия (вторая встреча в
    другой день делает его серией — и серией оно остаётся, даже когда
    протоколов нет), а если файл в «Разовых» — и его путь для переезда."""
    if place.get("kind") not in ("oneoff", "series") or not up.get("path"):
        return
    # «Загружено, но без ссылки» (uploaded) — файл тоже уже лежит в облаке.
    if not (up.get("ok") or up.get("uploaded")):
        return
    try:
        item = None
        # sub_failed — Google не создал подпапку, файл лёг в саму папку
        # записей: переезжать «к соседу» оттуда нельзя (ревью 28.10).
        if place["kind"] == "oneoff" and not up.get("sub_failed"):
            item = {"path": str(up["path"]), "backend": up.get("backend") or "",
                    "date": place.get("date") or "", "kind": kind,
                    "url": up.get("url") or ""}
        meeting_series.note_placement(user, title, place.get("date") or "", item)
    except Exception:  # noqa: BLE001 — раскладка не должна ронять доставку
        log.warning("Не запомнил выгрузку встречи «%s»", title, exc_info=True)


def adopt(user: str, title: str | None, place: dict, cfg: dict, logf=None) -> int:
    """Название стало серией — перевезти файлы прежних встреч из «Разовых» в
    папку серии. Возвращает, сколько файлов переехало. Ничего не роняет."""
    if place.get("kind") != "series":
        return 0
    try:
        _key, entry = meeting_series.find_exact(user, title)
        pending = list((entry or {}).get("oneoff_files") or [])
        if not pending:
            return 0
        from .automation import clouds
        moved, done, tries = 0, set(), {}
        for f in pending:
            res = clouds.move_to_sibling(f, place["sub"], cfg)
            pth = f.get("path")
            if res.get("ok"):
                moved += 1
                done.add(pth)
                if res.get("warning"):
                    # Переехал, но ссылка в Weeek могла сломаться — громко.
                    log.warning("Файл %s: %s", pth, res["warning"])
                    if logf:
                        logf(f"⚠ Облако: {Path(str(pth)).name} — {res['warning']}")
            elif res.get("retry") and int(f.get("tries") or 0) < 2:
                tries[pth] = int(f.get("tries") or 0) + 1
            else:
                done.add(pth)
                log.warning("Файл %s остался в «Разовых»: %s", pth, res.get("error"))
        meeting_series.update_oneoff_files(user, title, done, tries)
        if moved and logf:
            logf(f"Облако: «{place['sub']}» стала серией — {moved} файл(ов) прежних "
                 "встреч перенесено из разовых в её папку.")
        return moved
    except Exception:  # noqa: BLE001
        log.warning("Переезд файлов в папку серии «%s» сорвался", title, exc_info=True)
        return 0
