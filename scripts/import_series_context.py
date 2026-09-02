#!/usr/bin/env python3
"""Импорт карточек серий встреч из docs/series-seed.json в память команды.

Запуск на сервере (внутри контейнера, чтобы совпали DATA_DIR/DATABASE_URL):

    docker compose exec app python scripts/import_series_context.py --user sglazunov
    docker compose exec app python scripts/import_series_context.py --user sglazunov --file /data/my-series.json

`--user` — логин админа команды (карточки общие на команду). Существующие
карточки НЕ затираются: контекст дописывается только там, где он пуст, тип и
приоритет ставятся, если не были заданы. С `--force` — перезаписать контекст.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import meeting_series  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True, help="логин админа команды")
    ap.add_argument("--file", default=str(Path(__file__).resolve().parents[1] / "docs" / "series-seed.json"))
    ap.add_argument("--force", action="store_true", help="перезаписать непустой контекст")
    args = ap.parse_args()

    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    existing = meeting_series.load(args.user)
    n_new = n_upd = n_skip = 0
    for item in data.get("series") or []:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        key, cur = meeting_series.find(args.user, title)
        fields: dict = {"title": title}
        if cur is None or args.force or not (cur.get("context") or "").strip():
            fields["context"] = item.get("context", "")
        if cur is None or args.force or not cur.get("preset"):
            fields["preset"] = item.get("preset", "")
        if cur is None or args.force or (cur.get("priority") or "normal") == "normal":
            fields["priority"] = item.get("priority", "normal")
        if item.get("weeek_project_id") and (cur is None or not cur.get("weeek_project_id")):
            fields["weeek_project_id"] = item["weeek_project_id"]
        if cur is None:
            n_new += 1
        elif len(fields) > 1:
            n_upd += 1
        else:
            n_skip += 1
            continue
        meeting_series.upsert(args.user, key if cur is not None else title, fields)
        print(f"{'создана' if cur is None else 'обновлена'}: {title} → {key if cur else meeting_series.series_key(title)}")
    print(f"Готово: новых {n_new}, обновлено {n_upd}, без изменений {n_skip}; "
          f"всего серий у команды: {len(meeting_series.load(args.user))} (было {len(existing)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
