#!/usr/bin/env python3
"""Разложить АРХИВ облака по папкам встреч (docs/ТЗ-ПАПКИ-ВСТРЕЧ.md, §6).

Новые встречи раскладываются сами с момента обновления. Этот скрипт — разовый
перенос того, что уже лежит плоско в «Телемост-записи» и «Телемост-протоколы»
(и в «Разовых встречах»), по тем же правилам: у названия, которое встречается
в разные дни, — своя папка; встреча одного дня — в «Разовые встречи».

Запуск на сервере (внутри контейнера, под пользователем приложения):

    # 1. План — НИЧЕГО не двигает, только показывает, что куда поедет
    docker compose exec -u app app python scripts/sort_cloud_archive.py --user ЛОГИН
    # 2. Один файл на пробу — открыть его ссылку из Weeek и убедиться, что жива
    docker compose exec -u app app python scripts/sort_cloud_archive.py --user ЛОГИН --apply --limit 1
    # 3. Всё остальное
    docker compose exec -u app app python scripts/sort_cloud_archive.py --user ЛОГИН --apply

`--user` — логин владельца команды или её участника: архив всегда
раскладывается для КОМАНДЫ (настройки облака и карточки серий — общие).
`--only "Встреча с ОО"` — только одна серия.

⚠️ Ссылки на файлы уже стоят в задачах Weeek. Яндекс.Диск переносится через
`yandex_disk.move`: ссылка сверяется до и после, и если она сменилась, файл
возвращается на место, а скрипт ОСТАНАВЛИВАЕТСЯ — значит, Диск при переносе
ссылки не сохраняет, и переносить архив нельзя вовсе. Файлы с тем же именем в
папке назначения не затираются. Google Drive скрипт не поддерживает.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import cloud_folders, meeting_series  # noqa: E402

# Только файлы, которые кладёт сам бот: видео встречи и документы протокола.
_MEETING_FILE_RE = re.compile(r"\.(mp4|mkv|webm|docx|txt)$", re.IGNORECASE)
_PROTOCOL_RE = re.compile(r"[-–—]\s*(протокол|расшифровка)\.\w+$", re.IGNORECASE)


def _day_sort(day: str) -> tuple:
    d, m, y = (day.split(".") + ["0", "0", "0"])[:3]
    return (int(y or 0), int(m or 0), int(d or 0))


# --------------------------------------------------------------------------- #
# Облака: где лежит архив, как его прочитать и как переложить файл
# --------------------------------------------------------------------------- #
class Yandex:
    label = "Яндекс.Диск"

    def __init__(self, cfg: dict):
        from app.automation import clouds
        from app.automation.clouds import yandex_disk
        self.disk = yandex_disk
        self.bcfg = clouds._backend_cfg(cfg, "yandex_disk")
        # Все папки встреч — внутри папки записей, не по всему Диску.
        self.root = yandex_disk.meetings_root(self.bcfg)
        proto = (cfg.get("protocol_folder") or "").strip().rstrip("/")
        self.sources = [self.root]
        if proto.startswith("disk:/") and proto != self.root:
            self.sources.append(proto)
        self.sources.append(f"{self.root}/{cloud_folders.oneoff_name(cfg)}")

    def list(self, folder: str) -> list[dict]:
        return self.disk.list_folder(folder, self.bcfg)

    def dest(self, sub: str, name: str) -> str:
        return f"{self.root}/{sub}/{name}"

    def move(self, src: str, dst: str) -> dict:
        return self.disk.move(src, dst, self.bcfg)


class Local:
    label = "локальный диск"

    def __init__(self, cfg: dict, user: str = ""):
        # Папка по умолчанию у каждой команды своя (`data/recordings/<команда>`,
        # ревью 30.10): читать общий корень значило не видеть новых файлов и
        # с --apply раскладывать файлы ЧУЖИХ команд по плану этой.
        from app.automation import clouds
        from app.automation.clouds import local
        self.root = str(local.base_dir(clouds._backend_cfg(cfg, "local", user)))
        proto = (cfg.get("protocol_folder") or "").strip()
        proto = proto if Path(proto).is_absolute() else str(Path(self.root) / "protocols")
        self.sources = [self.root, proto,
                        str(Path(self.root) / cloud_folders.oneoff_name(cfg))]

    def list(self, folder: str) -> list[dict]:
        p = Path(folder)
        if not p.is_dir():
            return []
        return [{"name": f.name, "path": str(f)} for f in sorted(p.iterdir()) if f.is_file()]

    def dest(self, sub: str, name: str) -> str:
        return str(Path(self.root) / sub / name)

    def move(self, src: str, dst: str) -> dict:
        s, d = Path(src), Path(dst)
        if not s.exists():
            return {"ok": False, "error": "файла уже нет"}
        if d.exists():
            return {"ok": False, "error": "в папке назначения уже есть файл с таким именем"}
        d.parent.mkdir(parents=True, exist_ok=True)
        os.replace(s, d)
        return {"ok": True, "path": str(d), "url": d.as_uri()}


def backend_for(cfg: dict, user: str = ""):
    kind = cfg.get("cloud") or "local"
    if kind == "yandex_disk":
        return Yandex(cfg)
    if kind == "local":
        return Local(cfg, user)
    raise SystemExit(f"Облако «{kind}» скрипт не поддерживает (только Яндекс.Диск и "
                     "локальный диск).")


# --------------------------------------------------------------------------- #
# План и перенос
# --------------------------------------------------------------------------- #
def collect(cloud) -> tuple[list[dict], list[str]]:
    """Файлы встреч из всех папок-источников и то, что пропущено."""
    files, skipped, seen = [], [], set()
    for folder in cloud.sources:
        for it in cloud.list(folder):
            if it["path"] in seen:
                continue
            seen.add(it["path"])
            name = it["name"]
            day = meeting_series.date_prefix(name)
            key = meeting_series.series_key(name)
            if not (_MEETING_FILE_RE.search(name) and day and key):
                skipped.append(it["path"])
                continue
            files.append({"name": name, "path": it["path"], "day": day, "key": key,
                          "kind": "protocol" if _PROTOCOL_RE.search(name) else "video"})
    return files, skipped


def plan(user: str, cfg: dict, cloud, files: list[dict], only: str = "") -> list[dict]:
    """Группы по названию встречи: куда едет каждая и какие файлы двигать.
    Одна папка на группу — по самому свежему файлу, иначе «CRM с ОД» и
    «CRM с од» разъехались бы по двум папкам."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for f in files:
        groups[f["key"]].append(f)
    only_key = meeting_series.series_key(only) if only else ""
    out = []
    for key, items in groups.items():
        if only_key and key != only_key:
            continue
        days = {f["day"] for f in items}
        rep = max(items, key=lambda f: (_day_sort(f["day"]), f["name"]))
        place = cloud_folders.resolve(user, rep["name"], cfg, date=rep["day"],
                                      extra_days=days)
        moves = []
        for f in sorted(items, key=lambda f: (_day_sort(f["day"]), f["name"])):
            dst = cloud.dest(place["sub"], f["name"])
            if dst != f["path"]:
                moves.append(dict(f, dst=dst))
        out.append({"key": key, "title": meeting_series.display_title(rep["name"]),
                    "place": place, "days": len(days), "files": len(items),
                    "moves": moves})
    out.sort(key=lambda g: (g["place"]["kind"] != "series", g["place"]["sub"].lower(),
                            g["title"].lower()))
    return out


def print_plan(groups: list[dict]) -> int:
    total = 0
    for g in groups:
        where = g["place"]["sub"]
        kind = "серия" if g["place"]["kind"] == "series" else "разовая"
        n = len(g["moves"])
        total += n
        tail = f"переложить {n}" if n else "уже на месте"
        print(f"  «{g['title']}» → «{where}» ({kind}, дней: {g['days']}, "
              f"файлов: {g['files']}) — {tail}")
    return total


def apply(user: str, cloud, groups: list[dict], limit: int | None = None) -> dict:
    moved, failed, stopped = 0, [], ""
    left = limit if limit is not None else -1
    for g in groups:
        for m in g["moves"]:
            if left == 0:
                return {"moved": moved, "failed": failed, "stopped": stopped}
            left -= 1
            res = cloud.move(m["path"], m["dst"])
            if res.get("ok"):
                moved += 1
                print(f"  ✓ {m['name']} → {g['place']['sub']}")
                if res.get("warning"):
                    print(f"    ⚠ {res['warning']}")
                item = None
                if g["place"]["kind"] == "oneoff":
                    # Лежит в «Разовых» — переедет сам, когда название станет серией.
                    item = {"path": res.get("path") or m["dst"],
                            "backend": "yandex_disk" if isinstance(cloud, Yandex) else "local",
                            "date": m["day"], "kind": m["kind"], "url": res.get("url") or ""}
                meeting_series.note_placement(user, m["name"], m["day"], item)
                # Был в очереди переезда по старому пути — уже не нужно.
                meeting_series.update_oneoff_files(user, m["name"], {m["path"]})
            else:
                failed.append((m["path"], res.get("error") or "неизвестная ошибка"))
                print(f"  ✗ {m['name']}: {res.get('error')}")
            if res.get("link_changed"):
                stopped = ("Яндекс.Диск сменил публичную ссылку при переносе — ссылки "
                           "в Weeek сломались бы. Перенос архива остановлен.")
                return {"moved": moved, "failed": failed, "stopped": stopped}
    return {"moved": moved, "failed": failed, "stopped": stopped}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--user", required=True, help="логин владельца команды")
    ap.add_argument("--apply", action="store_true", help="переложить (без него — только план)")
    ap.add_argument("--limit", type=int, default=None, help="переложить не больше N файлов")
    ap.add_argument("--only", default="", help="только встреча с этим названием")
    args = ap.parse_args(argv)

    from app import security
    from app.automation import settings as auto_settings
    # Всё — по КОМАНДЕ, а не по логину из --user: настройки облака, папка
    # локального облака и карточки серий ключуются командой. С логином
    # участника скрипт читал чужую (пустую) папку `data/recordings/<логин>` и
    # писал размещения в карточки серий, которых нет (второй раунд ревью 30.10).
    team = security.team_of(args.user)
    if team != security.normalize_username(args.user):
        print(f"«{args.user}» — участник команды «{team}»: раскладываю архив команды.")
    cfg = auto_settings.load(team)
    if not cloud_folders.enabled(cfg):
        print("Раскладка по папкам выключена («Облако» → «Раскладка по встречам»). "
              "Включите её — иначе новые файлы снова лягут плоско.")
        return 2
    cloud = backend_for(cfg, team)
    print(f"Облако: {cloud.label}. Папка встреч: {cloud.root}")
    print("Читаю: " + ", ".join(cloud.sources))
    files, skipped = collect(cloud)
    groups = plan(team, cfg, cloud, files, args.only)
    print(f"Файлов встреч: {len(files)}, названий: {len(groups)}"
          + (f", пропущено (не файлы встреч): {len(skipped)}" if skipped else ""))
    total = print_plan(groups)
    if not args.apply:
        print(f"\nК переносу: {total}. Это план — ничего не перенесено. "
              "Запустите с --apply (на пробу — с --limit 1).")
        return 0
    if not total:
        print("Переносить нечего.")
        return 0
    print("\nПереношу…")
    res = apply(team, cloud, groups, args.limit)
    print(f"\nПеренесено: {res['moved']}, не удалось: {len(res['failed'])}.")
    if res["stopped"]:
        print("!! " + res["stopped"])
        return 1
    return 0 if not res["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
