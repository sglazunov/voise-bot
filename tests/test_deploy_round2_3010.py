"""Деплой и CI, второй раунд ревью 30.10 (docs/АДВЕРСАРИАЛЬНОЕ-РЕВЮ-2026-10-30-2.md §1).

Здесь то, что не про сам update.sh (его шимы — tests/test_update_sh.py) и не
про разбор адреса (tests/test_check_compose_config.py):
  • `_load` переписывал «Прервано» из ЛЮБОГО процесса, импортировавшего
    app.jobs: scripts/compare_models.py («ничего не сохраняет») превращал
    живые задачи сервиса в error/done, update.sh видел 0 занятых;
  • README: пути в обход скрипта (`up -d app` тянул пересоздание db без
    REINDEX), «любые символы» в пароле (а `$` compose подставляет), бэкапы с
    секретами в корне репозитория и в контексте сборки;
  • возврат старого совета «не задавайте DATABASE_URL» (мутант выживал);
  • `ARG VTX_BUILD` в стадии фронта (мутант выживал);
  • lock не сверялся с полным деревом зависимостей (CI вырезал тяжёлое
    поддерево) — scripts/check_lock.py и job `lock` в CI;
  • удалённый из CI шаг «--fresh отвергает прод-строку» (мутант выживал) —
    шаг CI здесь запускается по-настоящему.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from app import config, db
from app.jobs import store

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts import check_lock  # noqa: E402

README = (ROOT / "README.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")
CI = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))


# ---- _load: «Прервано» пишет только процесс сервиса --------------------------

def _running_jobs_file(path: Path) -> bytes:
    raw = [{"id": "live1", "filename": "live1.mp3", "audio_path": "", "language": "ru",
            "diarize": False, "status": "running", "owner": "alice"},
           {"id": "an1", "filename": "an1.mp3", "audio_path": "", "language": "ru",
            "diarize": False, "status": "analyzing", "owner": "alice"}]
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path.read_bytes()


class TestПрерванныеПишетТолькоСервис:
    @pytest.fixture(autouse=True)
    def _restore(self, monkeypatch):
        monkeypatch.setattr(db, "enabled", lambda: False)
        f = config.JOBS_FILE
        saved = f.read_bytes() if f.exists() else None
        yield
        if saved is None:
            f.unlink(missing_ok=True)
        else:
            f.write_bytes(saved)
        store._jobs.clear()

    def test_чужой_процесс__в_памяти_прервано_на_диске_нетронуто(self, monkeypatch):
        monkeypatch.delenv("VTX_SERVICE_PROCESS", raising=False)
        before = _running_jobs_file(config.JOBS_FILE)
        store._jobs.clear()
        store._load()
        assert store._jobs["live1"].status == "error"      # в памяти — как раньше
        assert config.JOBS_FILE.read_bytes() == before, "живые задачи сервиса переписаны"

    def test_чужой_процесс_на_postgres__ни_одного_upsert(self, monkeypatch):
        monkeypatch.delenv("VTX_SERVICE_PROCESS", raising=False)
        monkeypatch.setattr(db, "enabled", lambda: True)
        monkeypatch.setattr(db, "jobs_load", lambda: [
            {"id": "live1", "filename": "a", "audio_path": "", "language": "ru",
             "diarize": False, "status": "running", "owner": "alice"}])
        monkeypatch.setattr(db, "job_upsert", lambda d: pytest.fail("upsert из чужого процесса"))
        monkeypatch.setattr(db, "jobs_save", lambda rows: pytest.fail("jobs_save из чужого процесса"))
        store._jobs.clear()
        store._load()

    def test_процесс_сервиса__пишет(self, monkeypatch):
        monkeypatch.setenv("VTX_SERVICE_PROCESS", "1")
        before = _running_jobs_file(config.JOBS_FILE)
        store._jobs.clear()
        store._load()
        assert config.JOBS_FILE.read_bytes() != before


def _stub_whisper(tmp: Path) -> Path:
    pkg = tmp / "stubs" / "faster_whisper"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("WhisperModel = object\n", encoding="utf-8")
    (pkg / "utils.py").write_text("_MODELS = {}\n", encoding="utf-8")
    return pkg.parent


def _script_env(tmp: Path, data: Path, service: bool) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("VTX_", "DATABASE_URL", "PG"))}
    env.update({"VTX_DATA_DIR": str(data), "VTX_RECORDER_ENABLED": "0", "VTX_OLLAMA": "0",
                "VTX_AUTO_SETUP": "0", "VTX_SECRET_KEY": "test-master-key-do-not-use-in-prod",
                "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                "PYTHONPATH": os.pathsep.join([str(ROOT), str(_stub_whisper(tmp))])})
    if service:
        env["VTX_SERVICE_PROCESS"] = "1"
    return env


@pytest.mark.parametrize("service", [False, True], ids=["скрипт", "контроль-сервис"])
def test_compare_models_не_переписывает_задачи_сервиса(tmp_path, service):
    data = tmp_path / "data"
    data.mkdir()
    before = _running_jobs_file(data / "jobs.json")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "compare_models.py"),
                    "nobody", "no-such-job"], cwd=ROOT, env=_script_env(tmp_path, data, service),
                   capture_output=True, text=True, encoding="utf-8", timeout=180)
    changed = (data / "jobs.json").read_bytes() != before
    # Контроль: с флагом сервиса тот же импорт пишет — значит, тест видит запись.
    assert changed is service


@pytest.mark.parametrize("service", [False, True], ids=["скрипт", "контроль-сервис"])
def test_gen_api_table_не_переписывает_задачи_сервиса(tmp_path, service):
    # Тот же импорт, что делает скрипт (app.main → app.jobs), без записи doc.
    data = tmp_path / "data"
    data.mkdir()
    before = _running_jobs_file(data / "jobs.json")
    code = ("import runpy; runpy.run_path(r'%s', run_name='gen_api_table_import_only')"
            % (ROOT / "scripts" / "gen_api_table.py"))
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                       env=_script_env(tmp_path, data, service),
                       capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert r.returncode == 0, r.stderr[-2000:]
    assert ((data / "jobs.json").read_bytes() != before) is service


def test_флаг_ставит_только_запуск_сервиса():
    run_sh = (ROOT / "docker" / "run.sh").read_text(encoding="utf-8").splitlines()
    exec_i = next(i for i, s in enumerate(run_sh) if s.startswith("exec uvicorn"))
    exp_i = next(i for i, s in enumerate(run_sh) if s.strip() == "export VTX_SERVICE_PROCESS=1")
    assert exp_i < exec_i
    # В окружении образа/compose флага быть не должно: его унаследовал бы
    # `docker compose exec app python scripts/…`.
    for f in ("Dockerfile", "docker-compose.yml", ".env.example"):
        txt = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(ENV\s+)?VTX_SERVICE_PROCESS\s*[=:]", txt, re.M), f


# ---- README и .env.example ---------------------------------------------------

def _paragraphs(text: str) -> list[str]:
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


@pytest.mark.parametrize("name,text", [("README.md", README), (".env.example", ENV_EXAMPLE)],
                         ids=["README", "env.example"])
def test_совет_не_задавать_database_url_только_с_оговоркой_о_старой_установке(name, text):
    """Мутант «вернуть старый совет» выживал: совет «не задавайте/закомментируйте
    DATABASE_URL» без оговорки ломает прод (том создан с паролем voise)."""
    advice = re.compile(r"не задавайте|leave it commented|закомментир|не нужен", re.I)
    for p in _paragraphs(text):
        if "DATABASE_URL" in p and advice.search(p):
            low = re.sub(r"[\s#]+", " ", p).lower()      # переносы и «#» комментариев
            assert "существующ" in low or "старой установ" in low, (name, p[:300])
            assert "не удаляйте" in low, (name, p[:300])


def test_readme_нет_путей_в_обход_reindex():
    """`docker compose up -d app` (и `up -d` без сервиса) пересоздаёт db через
    depends_on на новом образе — без REINDEX, который делает только update.sh."""
    for line in README.splitlines():
        if re.search(r"docker compose up -d\b.*\bapp\b", line) and "--build" not in line:
            assert "--no-deps" in line, line
        if "VTX_MODEL=" in line and "up -d" in line:
            assert "--no-deps app" in line, line
    after_env = next(ln for ln in README.splitlines() if ln.startswith("После правки `.env`"))
    assert "--no-deps app" in after_env, after_env


def test_readme_разовая_инструкция_на_этот_релиз():
    m = re.search(r"^### Разово:.*?(?=^#{2,3} )", README, re.M | re.S)
    assert m, "нет раздела «Разово: …» для первого обновления после 30.10"
    sec = m.group(0)
    pull = sec.find("git pull --ff-only")
    upd = sec.find("bash scripts/update.sh")
    assert 0 <= pull < upd, "сначала git pull руками, потом НОВЫЙ update.sh"
    assert "voise:voise" in sec and "НЕ удаляйте" in sec


def test_readme_пароль_и_доллар():
    assert "любые символы" not in README
    assert re.search(r"POSTGRES_PASSWORD='", README), "пример в одинарных кавычках"
    assert "`$`" in README


BACKUP_NAMES = ["backup.sql", "backup-2026-10-30.sql", ".env.bak-2026-10-30", ".env.bak"]


def test_бэкапы_вне_репозитория():
    targets = re.findall(r"(?:>\s*|cp \.env\s+)(\S*(?:backup|\.env\.bak)\S*)", README)
    assert targets, "бэкапы в README не найдены"
    for t in targets:
        t = t.strip("`'\"")
        assert t.startswith(("~/", "/", "$HOME", '"$HOME', "$BACKUP_DIR", '"$BACKUP_DIR')), t


@pytest.mark.skipif(not shutil.which("git"), reason="нет git")
def test_бэкапы_в_корне_игнорирует_git():
    for name in BACKUP_NAMES:
        r = subprocess.run(["git", "check-ignore", "-q", "--no-index", name], cwd=ROOT)
        assert r.returncode == 0, f"{name} не в .gitignore"
    r = subprocess.run(["git", "check-ignore", "-q", "--no-index", ".env.example"], cwd=ROOT)
    assert r.returncode == 1, ".env.example не должен игнорироваться"


def test_бэкапы_в_корне_не_уходят_в_контекст_сборки():
    pats = [ln.strip() for ln in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]
    for name in BACKUP_NAMES:
        assert any(fnmatch.fnmatch(name, p) for p in pats), f"{name} уйдёт в контекст сборки"


# ---- Dockerfile --------------------------------------------------------------

def test_arg_vtx_build_один_раз_и_только_в_последней_стадии():
    """Мутант `ARG VTX_BUILD` в стадии фронта выживал: тест смотрел только
    стадию приложения, а npm ci/build пересобирались бы на каждом коммите."""
    from tests.test_deploy_3010 import _instructions
    ins = _instructions(ROOT / "Dockerfile")
    last_from = max(i for i, s in enumerate(ins) if s.startswith("FROM "))
    args = [i for i, s in enumerate(ins) if re.match(r"ARG\s+VTX_BUILD\b", s)]
    assert len(args) == 1 and args[0] > last_from, [ins[i] for i in args]


# ---- lock против полного дерева ----------------------------------------------

def _report(*pkgs):
    return {"install": [{"metadata": {"name": n, "version": v}} for n, v in pkgs]}


class TestCheckLock:
    LOCK = "# шапка\nfoo==1.0\nBar_Baz==2.0  # комментарий\n"

    def test_совпадает(self):
        pins = check_lock.parse_lock(self.LOCK)
        assert check_lock.problems(_report(("foo", "1.0"), ("bar-baz", "2.0")), pins) == []

    def test_транзитивный_без_пина(self):
        pins = check_lock.parse_lock(self.LOCK)
        errs = check_lock.problems(_report(("foo", "1.0"), ("bar-baz", "2.0"), ("qux", "3")), pins)
        assert errs and "qux" in errs[0]

    def test_версия_разошлась(self):
        pins = check_lock.parse_lock(self.LOCK)
        errs = check_lock.problems(_report(("foo", "1.1"), ("bar-baz", "2.0")), pins)
        assert errs and "foo" in errs[0]

    def test_лишний_пин(self):
        # Пин пакета, которого нет в дереве, pip молча игнорирует (-c).
        pins = check_lock.parse_lock(self.LOCK + "ghost==9.9\n")
        errs = check_lock.problems(_report(("foo", "1.0"), ("bar-baz", "2.0")), pins)
        assert errs and "ghost" in errs[0]

    def test_не_точная_версия(self):
        with pytest.raises(ValueError):
            check_lock.parse_lock("foo>=1.0\n")

    def test_настоящий_lock_разбирается(self):
        pins = check_lock.parse_lock((ROOT / "requirements.lock.txt").read_text(encoding="utf-8"))
        for heavy in ("faster-whisper", "ctranslate2", "onnxruntime", "tokenizers",
                      "huggingface-hub", "av"):
            assert heavy in pins, heavy


def _ci_steps():
    for job_name, job in CI["jobs"].items():
        for step in job.get("steps", []):
            yield job_name, step


def test_ci_сверяет_lock_с_полным_деревом():
    """Тяжёлое поддерево (faster-whisper → onnxruntime, tokenizers,
    huggingface_hub…) CI вырезал из установки, и ложные пины в нём проходили."""
    runs = [(j, s.get("run", "")) for j, s in _ci_steps()
            if "--dry-run" in s.get("run", "") and "check_lock.py" in s.get("run", "")]
    assert runs, "нет шага CI: pip install --dry-run … + scripts/check_lock.py"
    job, run = runs[0]
    assert "-r requirements.txt" in run and "-c requirements.lock.txt" in run
    assert "grep -v" not in run, "дерево должно резолвиться целиком"
    assert "--ignore-installed" in run
    assert CI["jobs"][job]["runs-on"].startswith("ubuntu")
    setup = [s for s in CI["jobs"][job]["steps"] if "setup-python" in str(s.get("uses", ""))]
    assert setup and str(setup[0]["with"]["python-version"]) == "3.12"


# ---- шаг CI «compose config на вариантах .env» — по-настоящему ---------------

def _bash():
    from tests.test_update_sh import BASH
    return BASH


def _compose_ok() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True,
                              timeout=30).returncode == 0
    except Exception:
        return False


def _ci_compose_step() -> str:
    for _, step in _ci_steps():
        if step.get("name") == "compose config на вариантах .env":
            return step["run"]
    raise AssertionError("шага «compose config на вариантах .env» нет в CI")


def _run_ci_step(tmp: Path, checker_src: str):
    for f in ("docker-compose.yml", "Caddyfile", ".env.example"):
        shutil.copy(ROOT / f, tmp / f)
    (tmp / "scripts").mkdir()
    (tmp / "scripts" / "check_compose_config.py").write_text(checker_src, encoding="utf-8")
    shims = tmp / "shims"
    shims.mkdir()
    py3 = shims / "python3"
    py3.write_text('#!/usr/bin/env bash\nexec "$REAL_PY" "$@"\n', encoding="utf-8", newline="\n")
    py3.chmod(0o755)
    (tmp / "step.sh").write_text(_ci_compose_step(), encoding="utf-8", newline="\n")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("POSTGRES_", "DATABASE_URL", "DOMAIN", "COMPOSE_", "PG"))}
    env.update({"PATH": str(shims) + os.pathsep + env.get("PATH", ""),
                "REAL_PY": sys.executable.replace("\\", "/"), "PYTHONUTF8": "1"})
    return subprocess.run([_bash(), "step.sh"], cwd=tmp, env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=300)


@pytest.mark.skipif(not _compose_ok(), reason="нет docker compose")
class TestШагCIcompose:
    SRC = (ROOT / "scripts" / "check_compose_config.py").read_text(encoding="utf-8")

    def test_проходит_на_настоящем_скрипте(self, tmp_path):
        r = _run_ci_step(tmp_path, self.SRC)
        assert r.returncode == 0, (r.stdout + r.stderr)[-3000:]

    def test_ловит_скрипт_который_не_отвергает_прод_строку(self, tmp_path):
        # Мутант: --fresh ничего не ужесточает. Шаг CI с проверкой
        # «--fresh обязан отвергать старую строку» обязан упасть.
        mutant = self.SRC.replace('fresh = "--fresh" in argv', "fresh = False")
        assert mutant != self.SRC
        r = _run_ci_step(tmp_path, mutant)
        assert r.returncode != 0, "шаг CI не заметил, что --fresh принимает прод-строку"
