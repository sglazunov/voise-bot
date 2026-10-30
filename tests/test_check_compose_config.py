"""scripts/check_compose_config.py — сверка адреса базы и профиля https
(ревью 30.10, §1 и §7 «Деплой»).

До правки: адрес разбирался urllib, а не как в libpq (пароль `p@ss` признавался
верным, хотя libpq видит хост `ss@db`; `#` urllib считал фрагментом), прод-
подобный `.env` со старой строкой `DATABASE_URL=…voise:voise…` объявлялся
ошибкой (а это единственная рабочая строка на томе, созданном с паролем
voise), а caddy не проверялся вовсе. Своих тестов у скрипта не было.

Вторая половина — настоящий `docker compose config` на нескольких вариантах
`.env` (пропускается, если docker compose нет; в GitHub Actions он есть).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts import check_compose_config as ccc  # noqa: E402

CADDY_SCRIPT = ('test -n "$$DOMAIN" || { echo "caddy: укажите DOMAIN в .env"; exit 1; }; '
                "exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile")


def _cfg(url, pw="s3cret", user="voise", db="voise", pgpassword=None, caddy=None):
    app_env = {"DATABASE_URL": url}
    if pgpassword is not None:
        app_env["PGPASSWORD"] = pgpassword
    services = {
        "app": {"environment": app_env},
        "db": {"environment": {"POSTGRES_USER": user, "POSTGRES_PASSWORD": pw,
                               "POSTGRES_DB": db}},
    }
    if caddy is not None:
        services["caddy"] = caddy
    return {"services": services}


class TestLibpqРазбор:
    def test_учётные_данные_до_первой_собаки(self):
        p = ccc.libpq_uri("postgresql://voise:p@ss@db:5432/voise")
        assert p["password"] == "p" and p["host"] == "ss@db"

    def test_решётка_часть_пароля_а_не_фрагмент(self):
        p = ccc.libpq_uri("postgresql://voise:pa#ss@db:5432/voise")
        assert p == {"user": "voise", "password": "pa#ss", "host": "db",
                     "port": "5432", "dbname": "voise"}

    def test_процентное_экранирование(self):
        p = ccc.libpq_uri("postgresql://voise:p%40s%23s%2F@db/voise")
        assert p["password"] == "p@s#s/"

    def test_косая_в_пароле_ломает_учётные_данные(self):
        # libpq ищет «@» только до первого «/»: учётных данных нет.
        p = ccc.libpq_uri("postgresql://voise:pa/ss@db:5432/voise")
        assert "password" not in p and p["host"] == "voise"

    def test_параметры_перекрывают_адрес(self):
        p = ccc.libpq_uri("postgresql://voise:x@db/voise?password=y&host=db2")
        assert p["password"] == "y" and p["host"] == "db2"

    def test_битая_процентная_последовательность(self):
        with pytest.raises(ValueError):
            ccc.libpq_uri("postgresql://voise:100%@db/voise")

    def test_не_postgres_схема(self):
        with pytest.raises(ValueError):
            ccc.libpq_uri("mysql://u:p@db/x")


class TestСверка:
    def test_собака_в_пароле_не_признаётся_верной(self):
        errs, _ = ccc.problems(_cfg("postgresql://voise:p@ss@db:5432/voise", pw="p@ss"))
        assert any("хост" in e for e in errs), errs

    def test_решётка_в_пароле_верна(self):
        errs, warns = ccc.problems(_cfg("postgresql://voise:pa#ss@db:5432/voise", pw="pa#ss"),
                                   fresh=True)
        assert errs == [] and warns == []

    def test_пароль_из_pgpassword(self):
        cfg = _cfg("postgresql://voise@db:5432/voise", pw="p@ss#w/rd%", pgpassword="p@ss#w/rd%")
        assert ccc.problems(cfg, fresh=True) == ([], [])

    def test_пароля_нет_нигде(self):
        errs, _ = ccc.problems(_cfg("postgresql://voise@db:5432/voise"))
        assert any("пароля нет" in e for e in errs)

    def test_прод_со_старой_строкой__предупреждение_а_не_ошибка(self):
        cfg = _cfg("postgresql://voise:voise@db:5432/voise", pw="random-hex",
                   pgpassword="random-hex")
        errs, warns = ccc.problems(cfg)
        assert errs == []
        assert len(warns) == 1 and "НЕ удаляйте" in warns[0]

    def test_прод_со_старой_строкой__ошибка_на_чистой_установке(self):
        cfg = _cfg("postgresql://voise:voise@db:5432/voise", pw="random-hex")
        errs, _ = ccc.problems(cfg, fresh=True)
        assert any("пароль не совпадает" in e for e in errs)

    def test_пароль_не_печатается(self, capsys, monkeypatch):
        cfg = _cfg("postgresql://voise:voise@db:5432/voise", pw="TOPSECRET")
        monkeypatch.setattr(sys, "stdin", _Stdin(json.dumps(cfg)))
        assert ccc.main(["--fresh"]) == 1
        out = capsys.readouterr()
        assert "TOPSECRET" not in out.out + out.err and "voise:voise" not in out.err

    def test_зашитый_пользователь(self):
        errs, _ = ccc.problems(_cfg("postgresql://voise@db:5432/vtx", user="vtx", db="vtx",
                                    pgpassword="s3cret"))
        assert any("пользователь" in e for e in errs)

    def test_нет_хоста(self):
        errs, _ = ccc.problems(_cfg("postgresql://voise:s3cret@:5432/voise"))
        assert any("хост" in e for e in errs)


class _Stdin:
    def __init__(self, text):
        self._t = text

    def read(self, *a):
        return self._t


class TestСменаПароляРоли:
    def test_кавычки_удваиваются(self):
        sql = ccc.alter_role_sql(_cfg("x", pw="it's\\ok", user='we"ird'))
        assert sql == "ALTER ROLE \"we\"\"ird\" WITH PASSWORD 'it''s\\ok';\n"

    def test_без_пароля_ошибка(self):
        with pytest.raises(ValueError):
            ccc.alter_role_sql(_cfg("x", pw=""))


needs_sh = pytest.mark.skipif(not shutil.which("sh"), reason="нет sh")


def _caddy(script=CADDY_SCRIPT, caddyfile=ROOT / "Caddyfile"):
    return {"command": ["sh", "-c", script],
            "volumes": [{"type": "bind", "source": str(caddyfile),
                         "target": "/etc/caddy/Caddyfile", "read_only": True}]}


@needs_sh
class TestCaddy:
    def _errs(self, caddy):
        errs, _ = ccc.problems(_cfg("postgresql://voise@db:5432/voise", pgpassword="s3cret",
                                    caddy=caddy))
        return errs

    def test_команда_как_в_compose(self):
        assert self._errs(_caddy()) == []

    def test_перевёрнутая_проверка_домена(self):
        errs = self._errs(_caddy(CADDY_SCRIPT.replace("test -n", "test -z")))
        assert any("без DOMAIN" in e for e in errs)

    def test_домен_подставлен_при_сборке_конфига(self):
        # «$DOMAIN» вместо «$$DOMAIN» compose подставит из .env — проверка в
        # контейнере тогда сравнивает константу.
        errs = self._errs(_caddy(CADDY_SCRIPT.replace("$$DOMAIN", "ci.example.com")))
        assert any("без DOMAIN" in e for e in errs)

    def test_сломанный_путь_caddyfile(self, tmp_path):
        errs = self._errs(_caddy(caddyfile=tmp_path / "Caddyfile"))
        assert any("Caddyfile не найден" in e for e in errs)


# ---- настоящий docker compose config на вариантах .env ---------------------

def _compose_ok() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True,
                              timeout=30).returncode == 0
    except Exception:
        return False


needs_compose = pytest.mark.skipif(not _compose_ok(), reason="нет docker compose")

PROD_LINE = "DATABASE_URL=postgresql://voise:voise@db:5432/voise"


def _compose_json(tmp_path, env_lines):
    """`docker compose config --format json` проекта в tmp_path с .env из
    .env.example плюс env_lines (последние значения перекрывают)."""
    shutil.copy(ROOT / "docker-compose.yml", tmp_path / "docker-compose.yml")
    shutil.copy(ROOT / "Caddyfile", tmp_path / "Caddyfile")
    base = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    keys = {ln.split("=", 1)[0] for ln in env_lines}
    base = [ln for ln in base if ln.split("=", 1)[0] not in keys]
    (tmp_path / ".env").write_text("\n".join(base + list(env_lines)) + "\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("POSTGRES_", "DATABASE_URL", "DOMAIN", "COMPOSE_", "PG"))}
    r = subprocess.run(["docker", "compose", "config", "--format", "json"], cwd=tmp_path,
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@needs_compose
class TestНастоящийCompose:
    def test_чистая_установка(self, tmp_path):
        cfg = _compose_json(tmp_path, ["POSTGRES_PASSWORD=0123456789abcdef"])
        assert ccc.problems(cfg, fresh=True) == ([], [])
        assert "caddy" not in cfg["services"]

    def test_прод_со_старой_строкой(self, tmp_path):
        cfg = _compose_json(tmp_path, ["POSTGRES_PASSWORD=0123456789abcdef", PROD_LINE])
        errs, warns = ccc.problems(cfg)
        assert errs == [] and warns
        assert ccc.problems(cfg, fresh=True)[0]

    def test_пароль_со_спецсимволами_и_свой_пользователь(self, tmp_path):
        cfg = _compose_json(tmp_path, ["POSTGRES_PASSWORD='p@ss#w/rd%x?y'",
                                       "POSTGRES_USER=vtxuser", "POSTGRES_DB=vtxdb"])
        assert cfg["services"]["db"]["environment"]["POSTGRES_PASSWORD"] == "p@ss#w/rd%x?y"
        assert ccc.problems(cfg, fresh=True) == ([], [])

    @needs_sh
    def test_домен_и_профиль_https_в_env(self, tmp_path):
        cfg = _compose_json(tmp_path, ["POSTGRES_PASSWORD=0123456789abcdef",
                                       "DOMAIN=ci.example.com", "COMPOSE_PROFILES=https"])
        assert "caddy" in cfg["services"]
        assert ccc.problems(cfg, fresh=True) == ([], [])
