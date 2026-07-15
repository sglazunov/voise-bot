#!/usr/bin/env python3
"""One-shot migration of the file-based state under DATA_DIR into PostgreSQL.

Run it ONCE, with DATABASE_URL pointing at the target DB (and VTX_DATA_DIR at the
existing data dir). It reads the raw JSON files and writes them into the DB via
app.db (secrets stay encrypted exactly as on disk — the master key is unchanged).
Idempotent: every write is an upsert/replace, so re-running is safe.

    DATABASE_URL=postgresql://voise:voise@db:5432/voise \
    VTX_DATA_DIR=/data python -m scripts.migrate_to_postgres

Media files (uploads/results/recordings) are NOT touched — they stay on disk.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app import config, db


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main() -> int:
    if not config.DATABASE_URL:
        print("DATABASE_URL не задан — миграция отменена.", file=sys.stderr)
        return 2
    data = config.DATA_DIR
    print(f"Источник: {data}")
    print(f"Цель:     {config.DATABASE_URL.split('@')[-1]}")
    db.init_schema()

    # --- global files ---
    users = _read(data / "users.json")
    if isinstance(users, dict):
        db.users_save(users)
        print(f"  users:     {len(users)}")

    sessions = _read(data / "sessions.json")
    if isinstance(sessions, dict):
        db.sessions_save(sessions)
        print(f"  sessions:  {len(sessions)}")

    recovery = _read(data / "recovery.json")
    if isinstance(recovery, dict):
        db.recovery_save(recovery)
        print(f"  recovery:  {len(recovery)}")

    jobs = _read(data / "jobs.json")
    if isinstance(jobs, list):
        # keep only known columns; fill missing so INSERT has every field
        rows = [{c: j.get(c) for c in db.JOB_COLS} for j in jobs if isinstance(j, dict)]
        db.jobs_save(rows)
        print(f"  jobs:      {len(rows)}")

    # --- per-user files ---
    users_dir = data / "users"
    n_settings = n_creds = n_ctx = n_meet = 0
    if users_dir.is_dir():
        for udir in sorted(p for p in users_dir.iterdir() if p.is_dir()):
            user = udir.name
            s = _read(udir / "automation.json")
            if isinstance(s, dict):
                db.settings_save(user, s); n_settings += 1
            c = _read(udir / "llm_keys.json")
            if isinstance(c, dict):
                db.creds_save(user, c); n_creds += 1
            ctx = _read(udir / "ai_context.json")
            if isinstance(ctx, dict):
                db.aicontext_save(user, ctx); n_ctx += 1
            m = _read(udir / "meetings.json")
            if isinstance(m, dict):
                db.meetings_save(user, m); n_meet += 1
    print(f"  settings:  {n_settings}")
    print(f"  llm_keys:  {n_creds}")
    print(f"  ai_context:{n_ctx}")
    print(f"  meetings:  {n_meet}")
    print("Готово.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
