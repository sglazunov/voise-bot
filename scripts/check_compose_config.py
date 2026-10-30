#!/usr/bin/env python3
"""Проверить, что собранный compose-конфиг поднимет приложение на своей базе.

    docker compose config --format json | python3 scripts/check_compose_config.py

`docker compose config -q` ловит только синтаксис и незаданные `${…:?}`. Ошибку
P0-1 ревью 30.10 он пропускал: compose валиден, а приложение не входит в базу —
`DATABASE_URL` без хоста `db` (`…@:5432`) или с паролем `voise`, тогда как
сам Postgres создан с `POSTGRES_PASSWORD` из `.env`. Здесь сверяется, что
адрес базы у `app` ведёт в сервис `db` с теми же пользователем, паролем и
именем базы. Запускается в CI на `.env`, собранном из `.env.example`.
"""
from __future__ import annotations

import json
import sys
from urllib.parse import unquote, urlsplit


def problems(cfg: dict) -> list[str]:
    services = cfg.get("services") or {}
    app_env = (services.get("app") or {}).get("environment") or {}
    db_env = (services.get("db") or {}).get("environment") or {}
    url = app_env.get("DATABASE_URL") or ""
    if not url:
        return ["у app не задан DATABASE_URL"]
    u = urlsplit(url)
    want = {
        "хост": ("db", u.hostname),
        "пользователь": (db_env.get("POSTGRES_USER"), unquote(u.username or "")),
        "база": (db_env.get("POSTGRES_DB"), u.path.lstrip("/")),
    }
    out = [f"DATABASE_URL: {name} {got!r}, а у сервиса db — {exp!r}"
           for name, (exp, got) in want.items() if exp != got]
    # Сам пароль в лог не печатаем — только факт расхождения.
    if not db_env.get("POSTGRES_PASSWORD"):
        out.append("у db пустой POSTGRES_PASSWORD")
    elif unquote(u.password or "") != db_env["POSTGRES_PASSWORD"]:
        out.append("DATABASE_URL: пароль не совпадает с POSTGRES_PASSWORD сервиса db")
    return out


def main() -> int:
    errs = problems(json.load(sys.stdin))
    for e in errs:
        print(f"compose: {e}", file=sys.stderr)
    if not errs:
        print("compose: DATABASE_URL ведёт в сервис db с его учётными данными")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
