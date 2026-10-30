#!/usr/bin/env python3
"""Проиндексировать АРХИВ протоколов команды в память встреч (meeting_chunks).

Зачем. Поиск и «вопрос по всем встречам» работают по таблице кусков
(app/meeting_index.py), а она наполняется только с момента обновления —
новыми задачами распознавания. Всё, что было до этого, лежит готовыми
протоколами .docx в облаке (папка записей, по папкам серий и в «Разовых
встречах»): расшифровка с таймкодами внутри. Этот скрипт читает их один раз
и кладёт в таблицу; повторный прогон уже проиндексированное пропускает.

Запуск на сервере (внутри контейнера, под пользователем приложения):

    # 1. План — ничего не пишет, показывает, что будет проиндексировано
    docker compose exec -u app app python scripts/index_archive.py --user ЛОГИН
    # 2. Один файл на пробу
    docker compose exec -u app app python scripts/index_archive.py --user ЛОГИН --apply --limit 1
    # 3. Всё остальное
    docker compose exec -u app app python scripts/index_archive.py --user ЛОГИН --apply

`--user` — логин владельца команды: куски получают ключ ЭТОЙ команды, и
другим командам они не видны. Облако берётся из настроек команды (Яндекс.Диск
или локальный диск); `--dir ПУТЬ` читает папку с диска напрямую (копия
Яндекс.Диска на машине разработчика). `--only "Встреча с ОО"` — одна серия,
`--force` — переиндексировать уже лежащее.

Что пропускается: файлы не наших протоколов (не «… - протокол.docx»), без
даты в начале имени, и встречи, у которых в таблице уже есть куски из ЗАДАЧИ
распознавания (та же серия и день) — задача полнее архива.
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, db, meeting_index, meeting_series, security  # noqa: E402

_PROTOCOL_RE = re.compile(r"[-–—]\s*протокол\.docx$", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Откуда читать
# --------------------------------------------------------------------------- #
class Yandex:
    label = "Яндекс.Диск"

    def __init__(self, cfg: dict):
        from app.automation import clouds
        from app.automation.clouds import yandex_disk
        self.disk = yandex_disk
        self.bcfg = clouds._backend_cfg(cfg, "yandex_disk")
        self.root = yandex_disk.meetings_root(self.bcfg)
        proto = (cfg.get("protocol_folder") or "").strip().rstrip("/")
        self.sources = [self.root]
        if proto.startswith("disk:/") and proto != self.root:
            self.sources.append(proto)

    def walk(self):
        """Файлы корня, папок протоколов и ОДНОГО уровня подпапок (серии)."""
        seen: set[str] = set()
        for src in self.sources:
            for it in self.disk.list_entries(src, self.bcfg):
                if it["type"] == "dir":
                    for f in self.disk.list_entries(it["path"], self.bcfg):
                        if f["type"] == "file" and f["path"] not in seen:
                            seen.add(f["path"])
                            yield f
                elif it["path"] not in seen:
                    seen.add(it["path"])
                    yield it

    def fetch(self, path: str, tmp: Path) -> Path:
        return self.disk.download(path, self.bcfg, tmp / Path(path).name)


class Local:
    label = "локальный диск"

    def __init__(self, root: str | Path):
        self.root = str(root)
        self.sources = [self.root]

    def walk(self):
        base = Path(self.root)
        if not base.is_dir():
            return
        for p in sorted(base.iterdir()):
            if p.is_file():
                yield {"name": p.name, "path": str(p), "type": "file"}
            elif p.is_dir():
                for f in sorted(p.iterdir()):
                    if f.is_file():
                        yield {"name": f.name, "path": str(f), "type": "file"}

    def fetch(self, path: str, tmp: Path) -> Path:
        return Path(path)


def backend_for(cfg: dict, local_dir: str = ""):
    if local_dir:
        return Local(local_dir)
    kind = cfg.get("cloud") or "local"
    if kind == "yandex_disk":
        return Yandex(cfg)
    if kind == "local":
        raw = (cfg.get("local_dir") or "").strip()
        return Local(Path(raw) if raw else (config.DATA_DIR / "recordings"))
    raise SystemExit(f"Облако «{kind}» скрипт не поддерживает (Яндекс.Диск, локальный "
                     "диск или --dir).")


# --------------------------------------------------------------------------- #
# План
# --------------------------------------------------------------------------- #
def collect(cloud, only: str = "") -> tuple[list[dict], int]:
    """Протоколы архива: [{name, path, day, key}] и сколько файлов пропущено."""
    only_key = meeting_series.series_key(only) if only else ""
    files, skipped = [], 0
    for it in cloud.walk():
        name = it["name"]
        day = meeting_series.date_prefix(name)
        key = meeting_series.series_key(name)
        if not (_PROTOCOL_RE.search(name) and day and key):
            skipped += 1
            continue
        if only_key and key != only_key:
            continue
        files.append({"name": name, "path": it["path"], "day": day, "key": key})
    files.sort(key=lambda f: (meeting_index.meeting_ts_of(f["day"]) or 0, f["name"]))
    return files, skipped


def plan(team: str, files: list[dict], force: bool = False) -> list[dict]:
    """Что индексировать: каждому файлу — doc_id и причина пропуска, если есть."""
    existing = db.chunks_docs(team)
    by_doc = {d["doc_id"]: d for d in existing}
    from_jobs = {(d.get("series_key") or "", d.get("meeting_date") or "")
                 for d in existing if not str(d["doc_id"]).startswith(meeting_index.ARCHIVE_PREFIX)}
    out = []
    for f in files:
        doc_id = meeting_index.archive_doc_id(team, f["path"])
        skip = ""
        if (f["key"], f["day"]) in from_jobs:
            skip = "есть куски из задачи распознавания"
        elif doc_id in by_doc and not force:
            skip = "уже в индексе"
        out.append(dict(f, doc_id=doc_id, skip=skip))
    return out


def index_one(team: str, item: dict, cloud, tmp: Path) -> int:
    local = cloud.fetch(item["path"], tmp)
    parsed = meeting_index.parse_protocol_docx(local)
    if not parsed.get("ours"):
        raise ValueError("не наш протокол (нет заголовка «Протокол встречи»)")
    date = item["day"]
    rows = meeting_index.doc_rows(
        item["doc_id"], team,
        title=parsed.get("title") or meeting_series.display_title(item["name"]),
        series_key=item["key"], meeting_date=date,
        meeting_ts=meeting_index.meeting_ts_of(date),
        transcript=parsed.get("transcript") or "",
        analysis=meeting_index.analysis_from_parsed(parsed),
        is_draft=bool(parsed.get("is_draft")), source=item["path"])
    db.chunks_replace(item["doc_id"], rows)
    return len(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--user", required=True, help="логин владельца команды")
    ap.add_argument("--apply", action="store_true", help="записать (без него — только план)")
    ap.add_argument("--limit", type=int, default=None, help="проиндексировать не больше N файлов")
    ap.add_argument("--only", default="", help="только встреча с этим названием")
    ap.add_argument("--dir", default="", help="читать папку с диска вместо облака команды")
    ap.add_argument("--force", action="store_true", help="переиндексировать уже лежащее")
    args = ap.parse_args(argv)

    if not db.enabled():
        print("Нужен Postgres (DATABASE_URL): память встреч живёт только в базе.")
        return 2
    team = security.team_of(args.user)
    from app.automation import settings as auto_settings
    cfg = auto_settings.load(args.user)
    cloud = backend_for(cfg, args.dir)
    print(f"Команда: {team}. Источник: {cloud.label}: " + ", ".join(cloud.sources))
    files, skipped = collect(cloud, args.only)
    items = plan(team, files, args.force)
    todo = [i for i in items if not i["skip"]]
    print(f"Протоколов в архиве: {len(files)}, к индексации: {len(todo)}, "
          f"уже есть/пропущено: {len(items) - len(todo)}"
          + (f", не протоколы: {skipped}" if skipped else ""))
    for i in items:
        mark = "  · " if i["skip"] else "  + "
        print(f"{mark}{i['name']}" + (f" — {i['skip']}" if i["skip"] else ""))
    if not args.apply:
        print("\nЭто план — ничего не записано. Запустите с --apply (на пробу — с --limit 1).")
        return 0
    if not todo:
        print("Индексировать нечего.")
        return 0
    done, failed = 0, []
    with tempfile.TemporaryDirectory(prefix="vtx-archive-") as tmp:
        for i in todo[: args.limit] if args.limit is not None else todo:
            try:
                n = index_one(team, i, cloud, Path(tmp))
                done += 1
                print(f"  ✓ {i['name']}: кусков {n}")
            except Exception as e:  # noqa: BLE001 — один битый файл не останавливает архив
                failed.append((i["name"], str(e)))
                print(f"  ✗ {i['name']}: {e}")
    print(f"\nПроиндексировано: {done}, не удалось: {len(failed)}.")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
