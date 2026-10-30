"""scripts/update.sh под шимами docker и git (ревью 30.10, §1 и §7 «Деплой»).

До правки скрипт не проверялся ничем, даже `bash -n`. Найдено:
  • любая локальная правка отслеживаемого файла — три `git pull` впустую
    (три запроса к SourceCraft), «Репозиторий недоступен», сборка СТАРОГО
    кода и рапорт «совпадает с git»;
  • упавшая база или psql с ошибкой = «задач в работе: 0»;
  • «config не проходит — НЕ перезапускаю» недостижимо: compose build падал
    раньше под `set -e`.
Плюс новое: пробный вход в базу с новыми настройками (старая установка без
строки DATABASE_URL не войдёт) и REINDEX после смены musl → glibc.

Скрипт копируется во временную папку (он делает `cd` в родителя своей папки),
в PATH первыми — шимы `docker`, `git`, `python3`. Каждый вызов шимы пишут в
calls.log. Вход в базу проверяет НАСТОЯЩИЙ код из update.sh: шим docker
запускает его настоящим Python с поддельным модулем psycopg, который сверяет
пароль с «паролем роли» FAKE_ROLE_PW.
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


def _find_bash() -> str | None:
    if os.name == "nt":
        # Не bash.exe из System32 (это WSL): берём bash из Git for Windows.
        git = shutil.which("git")
        if git:      # …\Git\cmd\git.exe, …\Git\ucrt64\bin\git.exe и т.п.
            for base in Path(git).resolve().parents:
                for cand in (base / "usr" / "bin" / "bash.exe", base / "bin" / "bash.exe"):
                    if cand.exists():
                        return str(cand)
        return None
    return shutil.which("bash")


BASH = _find_bash()
pytestmark = pytest.mark.skipif(not BASH, reason="нет bash")

DOCKER_SHIM = r'''#!/usr/bin/env bash
# Шим docker для tests/test_update_sh.py: поведение — из переменных окружения.
echo "docker $*" >> "$STATE/calls.log"
if [ "$1" = image ]; then exit 0; fi
[ "$1" = compose ] || { echo "шим docker: неизвестно $*" >&2; exit 99; }
shift
case "$1" in
  config)
    if [ "${2:-}" = -q ]; then [ -z "${CONFIG_FAIL:-}" ]; exit; fi
    cat "$STATE/config.json"; exit 0 ;;
  build)
    [ -n "${BUILD_FAIL:-}" ] && { echo "pip: ошибка" >&2; exit 1; }
    touch "$STATE/built"; exit 0 ;;
  up) exit 0 ;;
  ps) echo "linux-voise   Up"; exit 0 ;;
  exec)
    svc="$3"; shift 3
    if [ "$svc" = db ]; then
      sql="$(cat)"
      echo "SQL: $sql" >> "$STATE/calls.log"
      [ -n "${DB_DOWN:-}" ] && { echo 'service "db" is not running' >&2; exit 1; }
      case "$sql" in
        *"count(*)"*)
          if [ -f "$STATE/built" ] && [ -n "${BUSY_AFTER_BUILD:-}" ]; then echo 1
          else echo "${BUSY_JOBS:-0}"; fi ;;
        *datcollversion*) echo "${COLL:-ok}" ;;
        *REINDEX*) [ -z "${REINDEX_FAIL:-}" ] || exit 1 ;;
      esac
      exit 0
    fi
    # app
    case "$1" in
      sh) echo "${REC_N:-0}"; exit 0 ;;
      python)
        [ -n "${APP_DOWN:-}" ] && { echo 'service "app" is not running' >&2; exit 1; }
        shift; exec "$REAL_PY" "$@" ;;
      ps) echo "docker-init"; exit 0 ;;
      printenv)
        if [ -n "${STALE_IMAGE:-}" ]; then echo old0000; else echo "$VTX_BUILD"; fi
        exit 0 ;;
    esac ;;
esac
echo "шим docker: неизвестно compose $*" >&2
exit 99
'''

GIT_SHIM = r'''#!/usr/bin/env bash
echo "git $*" >> "$STATE/calls.log"
case "$1" in
  status)
    if [ "${2:-}" = -sb ]; then echo "## main...origin/main [ahead 1, behind 2]"; exit 0; fi
    [ -n "${GIT_DIRTY:-}" ] && printf ' M %s\n' "$GIT_DIRTY"
    exit 0 ;;
  fetch) [ -z "${FETCH_FAIL:-}" ] ;;
  merge) [ -z "${MERGE_FAIL:-}" ] || { echo "fatal: Not possible to fast-forward" >&2; exit 128; } ;;
  rev-parse) echo abc1234 ;;
  *) echo "шим git: неизвестно $*" >&2; exit 99 ;;
esac
'''

PYTHON3_SHIM = r'''#!/usr/bin/env bash
echo "python3 $*" >> "$STATE/calls.log"
exec "$REAL_PY" "$@"
'''

# Поддельный psycopg: «роль» знает только пароль FAKE_ROLE_PW; пароль берётся,
# как у libpq, из адреса, а если в адресе его нет — из PGPASSWORD.
FAKE_PSYCOPG = r'''
import os, re


class OperationalError(Exception):
    pass


class _Conn:
    def close(self):
        pass


def connect(url, connect_timeout=None):
    if os.environ.get("FAKE_DB_DOWN"):
        raise OperationalError('connection to server at "db" failed: Connection refused')
    m = re.match(r"postgresql://[^:@/]*:([^@/]*)@", url)
    pw = m.group(1) if m else os.environ.get("PGPASSWORD")
    if pw is None:
        raise OperationalError("fe_sendauth: no password supplied")
    if pw != os.environ["FAKE_ROLE_PW"]:
        raise OperationalError('FATAL:  password authentication failed for user "voise"')
    return _Conn()
'''


def _app_cfg(url, pgpassword="0123456789abcdef", pw="0123456789abcdef"):
    return {"services": {
        "app": {"environment": {"DATABASE_URL": url, "PGPASSWORD": pgpassword}},
        "db": {"environment": {"POSTGRES_USER": "voise", "POSTGRES_PASSWORD": pw,
                               "POSTGRES_DB": "voise"}},
    }}


FRESH_CFG = _app_cfg("postgresql://voise@db:5432/voise")
PROD_KEPT = _app_cfg("postgresql://voise:voise@db:5432/voise")     # старая строка на месте
PROD_REMOVED = _app_cfg("postgresql://voise@db:5432/voise")        # строку удалили


class Run:
    def __init__(self, proc, state):
        self.rc = proc.returncode
        self.out = proc.stdout + proc.stderr
        self.calls = (state / "calls.log").read_text(encoding="utf-8") \
            if (state / "calls.log").exists() else ""

    def called(self, needle):
        return needle in self.calls

    def count(self, needle):
        return self.calls.count(needle)


@pytest.fixture
def run_update(tmp_path):
    proj = tmp_path / "proj"
    (proj / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "update.sh", proj / "scripts" / "update.sh")
    shutil.copy(ROOT / "scripts" / "check_compose_config.py",
                proj / "scripts" / "check_compose_config.py")
    shims = tmp_path / "shims"
    shims.mkdir()
    for name, body in (("docker", DOCKER_SHIM), ("git", GIT_SHIM), ("python3", PYTHON3_SHIM)):
        f = shims / name
        f.write_text(body, encoding="utf-8", newline="\n")
        f.chmod(0o755)
    fake = tmp_path / "fakepy"
    fake.mkdir()
    (fake / "psycopg.py").write_text(FAKE_PSYCOPG, encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()

    def run(*args, cfg=FRESH_CFG, role_pw="0123456789abcdef", **flags):
        (state / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        (state / "calls.log").unlink(missing_ok=True)
        (state / "built").unlink(missing_ok=True)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("PG", "DATABASE_URL", "VTX_"))}
        env.update({
            "PATH": str(shims) + os.pathsep + env.get("PATH", ""),
            "STATE": str(state),
            "REAL_PY": sys.executable.replace("\\", "/"),
            "PYTHONPATH": str(fake),
            "PYTHONUTF8": "1",
            "FAKE_ROLE_PW": role_pw,
            "VTX_UPDATE_DETACHED": "1",
            "VTX_UPDATE_RETRY_SEC": "0",
        })
        env.update({k: str(v) for k, v in flags.items()})
        proc = subprocess.run([BASH, str(proj / "scripts" / "update.sh"), *args],
                              cwd=proj, env=env, capture_output=True, text=True,
                              encoding="utf-8", timeout=120)
        return Run(proc, state)

    return run


def test_синтаксис_bash():
    r = subprocess.run([BASH, "-n", str(ROOT / "scripts" / "update.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_свободный_сервис__полный_проход(run_update):
    r = run_update()
    assert r.rc == 0, r.out
    assert "совпадает с git" in r.out
    calls = r.calls
    # Порядок: config до сборки, база до приложения.
    assert calls.index("compose config -q") < calls.index("compose build app")
    assert calls.index("compose build app") < calls.index("compose up -d --wait db")
    assert calls.index("compose up -d --wait db") < calls.index("compose up -d app")
    assert r.count("git fetch") == 1 and r.called("git merge --ff-only @{u}")


def test_неизвестный_параметр(run_update):
    r = run_update("--frce")
    assert r.rc == 2 and not r.called("compose build")


class TestГрязноеДерево:
    def test_останавливается_до_сети_и_сборки(self, run_update):
        r = run_update(GIT_DIRTY="docker-compose.yml")
        assert r.rc == 1, r.out
        assert "docker-compose.yml" in r.out and "--allow-dirty" in r.out
        assert not r.called("git fetch") and not r.called("compose build")

    def test_allow_dirty_собирает_с_правками(self, run_update):
        r = run_update("--allow-dirty", GIT_DIRTY="docker-compose.yml")
        assert r.rc == 0, r.out
        assert r.called("compose build app") and r.called("compose up -d app")

    def test_скачанное_не_применяется__стоп_без_повторов(self, run_update):
        r = run_update("--allow-dirty", GIT_DIRTY="app/main.py", MERGE_FAIL=1)
        assert r.rc == 1, r.out
        assert r.count("git fetch") == 1, "повторять имеет смысл только сеть"
        assert "НЕ применяются" in r.out
        assert "недоступен" not in r.out
        assert not r.called("compose build")


def test_сеть_недоступна__текущий_код_и_честный_итог(run_update):
    r = run_update(FETCH_FAIL=1)
    assert r.rc == 0, r.out
    assert r.count("git fetch") == 3
    assert "недоступен" in r.out and "ДО обновления" in r.out
    assert "совпадает с git" not in r.out


class TestЗанятость:
    def test_база_недоступна__это_не_ноль_задач(self, run_update):
        r = run_update(DB_DOWN=1)
        assert r.rc == 1, r.out
        assert "НЕ МОГУ ПРОВЕРИТЬ" in r.out
        assert not r.called("compose build")

    def test_база_недоступна__force_проходит(self, run_update):
        r = run_update("--force", DB_DOWN=1, COLL="ok")
        # Пробный вход в базу тоже не прошёл бы (база лежит) — это не отказ
        # по паролю, скрипт продолжает.
        assert r.rc == 0, r.out
        assert r.called("compose up -d app")

    def test_задачи_в_работе(self, run_update):
        r = run_update(BUSY_JOBS=2)
        assert r.rc == 1 and "задач в работе: 2" in r.out
        assert not r.called("compose build")

    def test_идёт_запись(self, run_update):
        r = run_update(REC_N=1)
        assert r.rc == 1 and "идёт запись" in r.out

    def test_занятость_после_сборки__не_перезапускает(self, run_update):
        r = run_update(BUSY_AFTER_BUILD=1)
        assert r.rc == 1, r.out
        assert r.called("compose build app")
        assert not r.called("compose up -d app") and not r.called("compose up -d --wait db")
        assert "За время сборки" in r.out

    def test_force_игнорирует_занятость(self, run_update):
        r = run_update("--force", BUSY_JOBS=3, BUSY_AFTER_BUILD=1)
        assert r.rc == 0, r.out
        assert r.called("compose up -d app")
        assert "SQL: select count(*)" not in r.calls


def test_сломанный_config__сообщение_достижимо_и_сборки_нет(run_update):
    r = run_update(CONFIG_FAIL=1)
    assert r.rc == 1
    assert "НЕ перезапускаю" in r.out
    assert not r.called("compose build")


def test_упавшая_сборка__контейнер_не_тронут(run_update):
    r = run_update(BUILD_FAIL=1)
    assert r.rc == 1 and "Сборка упала" in r.out
    assert not r.called("compose up")


class TestВходВБазу:
    def test_старая_установка_со_строкой__входит(self, run_update):
        r = run_update(cfg=PROD_KEPT, role_pw="voise")
        assert r.rc == 0, r.out
        assert "Вход в базу с новыми настройками проверен" in r.out
        assert "ПРЕДУПРЕЖДЕНИЕ" in r.out          # check_compose_config: строку не удалять

    def test_старая_установка_строку_удалили__стоп(self, run_update):
        r = run_update(cfg=PROD_REMOVED, role_pw="voise")
        assert r.rc == 1, r.out
        assert "НЕ ВОЙДЁТ" in r.out and "Пароль базы на старой установке" in r.out
        assert not r.called("compose build")

    def test_пароль_роли_сменён__входит_без_строки(self, run_update):
        r = run_update(cfg=PROD_REMOVED, role_pw="0123456789abcdef")
        assert r.rc == 0, r.out
        assert "Вход в базу с новыми настройками проверен" in r.out

    def test_база_не_отвечает__не_отказ_по_паролю(self, run_update):
        r = run_update(FAKE_DB_DOWN=1)
        assert r.rc == 0, r.out
        assert "проверить не удалось" in r.out

    def test_контейнер_app_не_запущен__продолжает(self, run_update):
        r = run_update(APP_DOWN=1)
        assert r.rc == 0, r.out
        assert "проверить не удалось" in r.out

    def test_адрес_базы_не_сходится__стоп(self, run_update):
        r = run_update(cfg=_app_cfg("postgresql://voise@:5432/voise"))
        assert r.rc == 1 and "не сходится" in r.out
        assert not r.called("compose build")


class TestСменаОбразаБазы:
    def test_reindex_до_запуска_приложения(self, run_update):
        r = run_update(COLL="reindex")
        assert r.rc == 0, r.out
        calls = r.calls
        assert "REINDEX DATABASE" in calls and "REFRESH COLLATION VERSION" in calls
        assert calls.index("REINDEX DATABASE") < calls.index("compose up -d app")

    def test_без_смены__reindex_нет(self, run_update):
        r = run_update(COLL="ok")
        assert r.rc == 0 and "REINDEX" not in r.calls

    def test_reindex_упал__приложение_не_перезапущено(self, run_update):
        r = run_update(COLL="reindex", REINDEX_FAIL=1)
        assert r.rc == 1 and not r.called("compose up -d app")


def test_образ_не_обновился__предупреждает(run_update):
    r = run_update(STALE_IMAGE=1)
    assert "образ НЕ обновился" in r.out
