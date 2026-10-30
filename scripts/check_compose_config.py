#!/usr/bin/env python3
"""Проверить, что собранный compose-конфиг поднимет приложение на своей базе.

    docker compose config --format json | python3 scripts/check_compose_config.py [--fresh]
    docker compose config --format json | python3 scripts/check_compose_config.py --alter-role-sql
    docker compose config --format json | python3 scripts/check_compose_config.py --db-mode

`docker compose config -q` ловит только синтаксис и незаданные `${…:?}`. Ошибку
P0-1 ревью 30.10 он пропускал: compose валиден, а приложение не входит в базу —
`DATABASE_URL` без хоста `db` (`…@:5432`) или с паролем `voise`, тогда как
сам Postgres создан с `POSTGRES_PASSWORD` из `.env`. Здесь сверяется, что
адрес базы у `app` ведёт в сервис `db` с теми же пользователем, паролем и
именем базы. Адрес разбирается так же, как его разбирает libpq (см.
`libpq_uri`), а не `urllib`: пароль `p@ss` urllib считает верным, а libpq
видит хост `ss@db`.

Пароль. Postgres берёт `POSTGRES_PASSWORD` ТОЛЬКО при создании тома `pgdata`,
дальше это просто строка в окружении. На проде том создан 28.07 с паролем
`voise`, и приложение входит лишь благодаря строке `DATABASE_URL=…voise:voise…`
в `.env`. Поэтому явный `DATABASE_URL` с паролем, отличным от
`POSTGRES_PASSWORD`, — по умолчанию ПРЕДУПРЕЖДЕНИЕ (строку на такой
установке удалять нельзя, см. README «Пароль базы на старой установке»), а с
`--fresh` (чистая установка, CI) — ошибка. Настоящий вход проверяет
`scripts/update.sh` — пробным подключением из контейнера app.

Внешний Postgres (`VTX_EXTERNAL_DB=1` в `.env` вместе с `DATABASE_URL` на
чужой хост): сверка с сервисом `db` пропускается — явный отказ от неё, а не
молчаливый; адрес должен разбираться, вести НЕ в `db` и нести пароль сам
(`PGPASSWORD` у app — пароль локального `db`, libpq взял бы его). `--db-mode`
печатает `external` или `compose` — так `scripts/update.sh` решает, у какой
базы спрашивать о занятости и делать ли REINDEX.

`$` в значениях. `compose config` печатает его экранированным (`$$`) — вывод
должен снова читаться compose, — а в контейнер уходит одиночный `$`. Поэтому
окружение сервисов читается через `service_env`, который возвращает `$`.

Ещё проверяется профиль `https`, если он включён: файл Caddyfile, который
монтирует caddy, существует, а команда caddy без `DOMAIN` выходит с ошибкой,
а с `DOMAIN` запускает caddy (команда прогоняется через `sh` с заглушкой
вместо caddy).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _pct_decode(s: str, what: str) -> str:
    """Процентное декодирование, как `conninfo_uri_decode` в libpq: битая
    последовательность — ошибка, а не буквальный «%»."""
    out = bytearray()
    i = 0
    raw = s.encode("utf-8")
    while i < len(raw):
        c = raw[i]
        if c == ord("%"):
            hx = raw[i + 1:i + 3].decode("ascii", "replace")
            if len(hx) != 2 or any(ch not in "0123456789abcdefABCDEF" for ch in hx):
                raise ValueError(f"{what}: неверная %-последовательность")
            if hx == "00":
                raise ValueError(f"{what}: %00 запрещён")
            out.append(int(hx, 16))
            i += 3
        else:
            out.append(c)
            i += 1
    return out.decode("utf-8", "replace")


def libpq_uri(url: str) -> dict:
    """Разобрать `postgresql://…` по правилам libpq (`conninfo_uri_parse_options`
    в fe-connect.c). Главные отличия от `urllib.parse.urlsplit`:
      • учётные данные — до ПЕРВОГО «@» (urllib берёт последний), причём «@»
        ищется только до первого «/»;
      • «#» — обычный символ (у urllib — начало фрагмента), «?» вне
        учётных данных начинает параметры;
      • параметры `?user=…&password=…&host=…&dbname=…` перекрывают части адреса.
    Возвращает словарь только с заданными частями."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            rest = url[len(prefix):]
            break
    else:
        raise ValueError("адрес должен начинаться с postgresql:// или postgres://")
    out: dict = {}
    i = 0
    while i < len(rest) and rest[i] not in "@/":
        i += 1
    if i < len(rest) and rest[i] == "@":
        cred = rest[:i]
        user, sep, password = cred.partition(":")
        if user:
            out["user"] = _pct_decode(user, "пользователь")
        if sep:
            out["password"] = _pct_decode(password, "пароль")
        rest = rest[i + 1:]
    # Хост (или [IPv6]) и порт — до «/» или «?».
    i = 0
    if rest.startswith("["):
        j = rest.find("]")
        if j < 0:
            raise ValueError("незакрытая «[» в адресе хоста")
        host, i = rest[1:j], j + 1
    else:
        while i < len(rest) and rest[i] not in ":/?":
            i += 1
        host = rest[:i]
    if host:
        out["host"] = _pct_decode(host, "хост")
    if i < len(rest) and rest[i] == ":":
        j = i + 1
        while j < len(rest) and rest[j] not in "/?":
            j += 1
        if rest[i + 1:j]:
            out["port"] = _pct_decode(rest[i + 1:j], "порт")
        i = j
    if i < len(rest) and rest[i] == "/":
        j = rest.find("?", i)
        j = len(rest) if j < 0 else j
        if rest[i + 1:j]:
            out["dbname"] = _pct_decode(rest[i + 1:j], "база")
        i = j
    if i < len(rest) and rest[i] == "?":
        for pair in rest[i + 1:].split("&"):
            if not pair:
                continue
            k, eq, v = pair.partition("=")
            if not eq:
                raise ValueError(f"параметр «{k}» без значения")
            out[_pct_decode(k, "параметр")] = _pct_decode(v, "параметр")
    return out


def service_env(cfg: dict, name: str) -> dict:
    """Окружение сервиса таким, каким его увидит контейнер: `compose config`
    экранирует `$` как `$$`, в контейнер уходит одиночный `$`."""
    env = ((cfg.get("services") or {}).get(name) or {}).get("environment") or {}
    return {k: v.replace("$$", "$") if isinstance(v, str) else v for k, v in env.items()}


def is_external(app_env: dict) -> bool:
    """Явный отказ от сверки с сервисом db: VTX_EXTERNAL_DB=1 в .env."""
    return str(app_env.get("VTX_EXTERNAL_DB") or "").strip().lower() in ("1", "true", "yes")


def _effective(url: str, env: dict) -> dict:
    """С чем libpq реально подключится: части адреса, а чего в адресе нет —
    из переменных PG* окружения app (docker-compose передаёт PGPASSWORD, чтобы
    не вставлять пароль в адрес без экранирования)."""
    p = libpq_uri(url)
    for key, var in (("host", "PGHOST"), ("port", "PGPORT"), ("user", "PGUSER"),
                     ("password", "PGPASSWORD"), ("dbname", "PGDATABASE")):
        if key not in p and env.get(var):
            p[key] = env[var]
    p.setdefault("port", "5432")
    if "dbname" not in p and "user" in p:
        p["dbname"] = p["user"]          # умолчание libpq: база = пользователь
    return p


def problems(cfg: dict, fresh: bool = False) -> tuple[list[str], list[str]]:
    """(ошибки, предупреждения)."""
    services = cfg.get("services") or {}
    app_env = service_env(cfg, "app")
    db_env = service_env(cfg, "db")
    errs: list[str] = []
    warns: list[str] = []
    url = app_env.get("DATABASE_URL") or ""
    if not url:
        return ["у app не задан DATABASE_URL"], warns
    try:
        eff = _effective(url, app_env)
    except ValueError as e:
        return [f"DATABASE_URL не разбирается libpq: {e}"], warns
    if is_external(app_env):
        own = libpq_uri(url)
        if eff.get("host") in (None, "", "db"):
            errs.append("VTX_EXTERNAL_DB=1, а DATABASE_URL ведёт в сервис db (или без "
                        "хоста) — уберите VTX_EXTERNAL_DB или укажите внешний хост")
        elif not own.get("password"):
            errs.append("VTX_EXTERNAL_DB=1: пароля нет в самом DATABASE_URL — libpq "
                        "взял бы PGPASSWORD, а это пароль ЛОКАЛЬНОГО db")
        else:
            warns.append(f"внешний Postgres (VTX_EXTERNAL_DB=1, хост {eff['host']!r}): "
                         "сверка с сервисом db пропущена; вход проверит update.sh")
        return errs + _caddy_problems(services.get("caddy")), warns
    want = {
        "хост": ("db", eff.get("host")),
        "порт": ("5432", eff.get("port")),
        "пользователь": (db_env.get("POSTGRES_USER"), eff.get("user")),
        "база": (db_env.get("POSTGRES_DB"), eff.get("dbname")),
    }
    errs += [f"DATABASE_URL: {name} {got!r}, а у сервиса db — {exp!r}"
             for name, (exp, got) in want.items() if exp != got]
    # Сам пароль в лог не печатаем — только факт расхождения.
    pg_pw = db_env.get("POSTGRES_PASSWORD")
    if not pg_pw:
        errs.append("у db пустой POSTGRES_PASSWORD")
    elif eff.get("password") is None:
        errs.append("DATABASE_URL: пароля нет ни в адресе, ни в PGPASSWORD у app")
    elif eff["password"] != pg_pw:
        msg = ("DATABASE_URL: пароль не совпадает с POSTGRES_PASSWORD сервиса db")
        if fresh:
            errs.append(msg)
        else:
            warns.append(
                msg + ". На старой установке (том pgdata создан со старым "
                "паролем, например voise) это и есть рабочий пароль — строку "
                "DATABASE_URL в .env НЕ удаляйте, пока не смените пароль роли "
                "(README, «Пароль базы на старой установке»). На чистой "
                "установке — удалите строку DATABASE_URL из .env.")
    errs += _caddy_problems(services.get("caddy"))
    return errs, warns


def _caddy_problems(caddy: dict | None) -> list[str]:
    if not caddy:
        return []          # профиль https выключен — caddy в конфиге нет
    out: list[str] = []
    for v in caddy.get("volumes") or []:
        if str(v.get("target", "")).endswith("/Caddyfile"):
            if not Path(v.get("source", "")).is_file():
                out.append(f"caddy: монтируемый Caddyfile не найден: {v.get('source')!r}")
            break
    else:
        out.append("caddy: Caddyfile не монтируется в /etc/caddy/Caddyfile")
    cmd = caddy.get("command") or []
    if len(cmd) != 3 or cmd[:2] != ["sh", "-c"]:
        return out + [f"caddy: ожидалась команда sh -c '…', а не {cmd!r}"]
    sh = shutil.which("sh")
    if not sh:
        print("compose: нет sh — команду caddy не прогоняю", file=sys.stderr)
        return out
    # `compose config` оставляет «$$» экранированным (вывод должен снова
    # читаться compose), в контейнер же уходит одиночный «$».
    script = cmd[2].replace("$$", "$")
    if subprocess.run([sh, "-n", "-c", script], capture_output=True).returncode:
        return out + ["caddy: синтаксическая ошибка в команде sh -c"]
    # Прогон с заглушкой caddy: без DOMAIN — выход с ошибкой и без запуска
    # caddy, с DOMAIN — запуск caddy run с Caddyfile.
    with tempfile.TemporaryDirectory() as tmp:
        stub = Path(tmp) / "caddy"
        stub.write_text('#!/bin/sh\necho "STUB-CADDY $*"\n', encoding="utf-8")
        stub.chmod(0o755)
        env = {**os.environ, "PATH": tmp + os.pathsep + os.environ.get("PATH", "")}
        for domain, must_run in (("", False), ("ci.example.com", True)):
            r = subprocess.run([sh, "-c", script], capture_output=True, text=True,
                               env={**env, "DOMAIN": domain}, timeout=30)
            ran = "STUB-CADDY run" in r.stdout and "/etc/caddy/Caddyfile" in r.stdout
            if must_run and (r.returncode or not ran):
                out.append("caddy: с заданным DOMAIN команда не запускает caddy run")
            if not must_run and (r.returncode == 0 or ran):
                out.append("caddy: без DOMAIN команда не останавливается с ошибкой")
    return out


def alter_role_sql(cfg: dict) -> str:
    """SQL смены пароля роли на POSTGRES_PASSWORD из .env — для старой
    установки (README, «Пароль базы на старой установке»). Печатается в
    stdout и идёт в psql через конвейер: в командной строке и истории
    оболочки пароля нет. Кавычки удваиваются; при standard_conforming_strings
    (умолчание Postgres) обратная косая внутри '…' — обычный символ, а psql
    не подставляет переменные внутри кавычек."""
    db_env = service_env(cfg, "db")
    user, pw = db_env.get("POSTGRES_USER"), db_env.get("POSTGRES_PASSWORD")
    if not user or not pw:
        raise ValueError("у сервиса db нет POSTGRES_USER или POSTGRES_PASSWORD")
    ident = '"' + user.replace('"', '""') + '"'
    lit = "'" + pw.replace("'", "''") + "'"
    return f"ALTER ROLE {ident} WITH PASSWORD {lit};\n"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cfg = json.load(sys.stdin)
    if "--alter-role-sql" in argv:
        try:
            sys.stdout.write(alter_role_sql(cfg))
        except ValueError as e:
            print(f"compose: {e}", file=sys.stderr)
            return 1
        return 0
    if "--db-mode" in argv:
        print("external" if is_external(service_env(cfg, "app")) else "compose")
        return 0
    fresh = "--fresh" in argv
    errs, warns = problems(cfg, fresh=fresh)
    for w in warns:
        print(f"compose: ПРЕДУПРЕЖДЕНИЕ: {w}", file=sys.stderr)
    for e in errs:
        print(f"compose: {e}", file=sys.stderr)
    if not errs:
        print("compose: DATABASE_URL ведёт в сервис db с его учётными данными"
              + (" (пароль — см. предупреждение)" if warns else ""))
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
