"""Деплой, ревью 30.10 (§1): статус прерванных задач, слои Dockerfile, lock.

1. Задачи, прерванные рестартом, получали «Прервано»/«Анализ прерван» только
   в памяти; в базе строка оставалась running/analyzing до ретеншна (~24 ч), и
   scripts/update.sh считал её работой — обновиться можно было лишь с --force.
2. Правка текста echo в RUN с сертификатом (он стоял ВЫШЕ pip) сбрасывала
   кэш pip и всех следующих слоёв, а ARG VTX_BUILD (новый на каждом коммите)
   стоял выше playwright install. Пересборка заново резолвила зависимости с
   открытыми диапазонами — класс сбоя PyAV 19. Теперь зависимости — сразу
   после apt и из requirements.lock.txt.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from app import config, db
from app.jobs import store

ROOT = Path(__file__).resolve().parent.parent


def _job(jid, status, **kw):
    d = {"id": jid, "filename": f"{jid}.mp3", "audio_path": "", "language": "ru",
         "diarize": False, "status": status, "owner": "alice"}
    d.update(kw)
    return d


class TestПрерванныеЗадачиПослеРестарта:
    RAW = [_job("r1", "running"), _job("q1", "queued"), _job("p1", "paused"),
           _job("a1", "analyzing"), _job("d1", "done")]

    def test_файловый_режим__статус_записан_на_диск(self, monkeypatch):
        monkeypatch.setattr(db, "enabled", lambda: False)
        config.JOBS_FILE.write_text(json.dumps(self.RAW), encoding="utf-8")
        store._jobs.clear()
        store._load()
        on_disk = {d["id"]: d for d in
                   json.loads(config.JOBS_FILE.read_text(encoding="utf-8"))}
        assert {k: on_disk[k]["status"] for k in on_disk} == {
            "r1": "error", "q1": "error", "p1": "error", "a1": "done", "d1": "done"}
        assert "Прервано" in on_disk["r1"]["error"]
        assert "Анализ прерван" in on_disk["a1"]["analysis_error"]
        busy = [d for d in on_disk.values()
                if d["status"] in ("queued", "running", "analyzing")]
        assert busy == [], "update.sh посчитал бы их работой"

    def test_postgres__upsert_только_прерванных(self, monkeypatch):
        monkeypatch.setattr(db, "enabled", lambda: True)
        monkeypatch.setattr(db, "jobs_load", lambda: [dict(d) for d in self.RAW])
        upserts = []
        monkeypatch.setattr(db, "job_upsert", lambda d: upserts.append(d))
        monkeypatch.setattr(db, "jobs_save", lambda rows: pytest.fail(
            "при старте нельзя переписывать всю таблицу"))
        store._jobs.clear()
        store._load()
        assert sorted((d["id"], d["status"]) for d in upserts) == [
            ("a1", "done"), ("p1", "error"), ("q1", "error"), ("r1", "error")]

    def test_битая_строка__ничего_не_пишется(self, monkeypatch):
        # Разбор упал посреди списка — в памяти не все задачи; запись файла
        # стёрла бы остальные.
        monkeypatch.setattr(db, "enabled", lambda: False)
        raw = [_job("r1", "running"), {"id": "bad", "нет_такого_поля": 1}]
        config.JOBS_FILE.write_text(json.dumps(raw), encoding="utf-8")
        before = config.JOBS_FILE.read_text(encoding="utf-8")
        store._jobs.clear()
        store._load()
        assert config.JOBS_FILE.read_text(encoding="utf-8") == before

    @pytest.fixture(autouse=True)
    def _restore_jobs_file(self, monkeypatch):
        # Прерванные пишет только процесс сервиса (docker/run.sh), см.
        # tests/test_deploy_round2_3010.py.
        monkeypatch.setenv("VTX_SERVICE_PROCESS", "1")
        f = config.JOBS_FILE
        saved = f.read_bytes() if f.exists() else None
        yield
        if saved is None:
            f.unlink(missing_ok=True)
        else:
            f.write_bytes(saved)
        store._jobs.clear()


# ---- Dockerfile -------------------------------------------------------------

def _instructions(path: Path) -> list[str]:
    """Инструкции Dockerfile (продолжения строк склеены, комментарии убраны)."""
    out, cur = [], ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#") and not cur:
            continue
        cur += line.rstrip("\\").strip() + " "
        if not line.rstrip().endswith("\\"):
            if cur.strip():
                out.append(cur.strip())
            cur = ""
    return out


def _app_stage() -> list[str]:
    ins = _instructions(ROOT / "Dockerfile")
    start = max(i for i, s in enumerate(ins) if s.startswith("FROM "))
    return ins[start:]


class TestСлоиDockerfile:
    def test_до_зависимостей_только_неизменное(self):
        """Выше pip install — только база, переменные, apt и копия файлов
        зависимостей. Ни ARG (VTX_BUILD меняется на каждом коммите), ни
        RUN, чей текст правят (сертификат), ни кода приложения."""
        st = _app_stage()
        pip_i = next(i for i, s in enumerate(st) if "pip install -r requirements.txt" in s)
        allowed = re.compile(r"^(FROM |ENV PYTHON|WORKDIR |RUN apt-get update |"
                             r"COPY requirements\.txt requirements\.lock\.txt )")
        bad = [s[:60] for s in st[1:pip_i] if not allowed.match(s)]
        assert bad == [], f"выше pip install лишнее: {bad}"

    def test_playwright_сразу_за_зависимостями(self):
        st = _app_stage()
        pip_i = next(i for i, s in enumerate(st) if "pip install -r requirements.txt" in s)
        pw_i = next(i for i, s in enumerate(st) if "playwright install" in s)
        between = [s[:60] for s in st[pip_i + 1:pw_i]
                   if not (s.startswith("ARG DIARIZATION") or "DIARIZATION" in s)]
        assert between == [], f"между pip и playwright: {between}"

    def test_метка_сборки_в_самом_конце(self):
        st = _app_stage()
        arg_i = next(i for i, s in enumerate(st) if s.startswith("ARG VTX_BUILD"))
        after = [s for s in st[arg_i + 1:] if s.startswith(("RUN ", "COPY ", "ADD "))]
        assert after == [], f"ниже ARG VTX_BUILD пересобирается на каждом коммите: {after}"

    def test_pip_ставит_из_lock(self):
        st = _app_stage()
        pip = next(s for s in st if "pip install -r requirements.txt" in s)
        assert "-c requirements.lock.txt" in pip

    def test_lock_не_в_dockerignore(self):
        ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
        assert not any(p.startswith("requirements") for p in ignored)


# ---- lock -------------------------------------------------------------------

def _pins() -> dict[str, str]:
    pins = {}
    for line in (ROOT / "requirements.lock.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, _, ver = line.partition("==")
            assert ver, f"в lock только точные версии: {line}"
            pins[canonicalize_name(name)] = ver
    return pins


def _reqs() -> list[Requirement]:
    out = []
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(Requirement(line))
    return out


class TestLock:
    def test_каждая_зависимость_закреплена_и_в_своём_диапазоне(self):
        pins = _pins()
        for req in _reqs():
            name = canonicalize_name(req.name)
            assert name in pins, f"{req.name} нет в requirements.lock.txt"
            assert req.specifier.contains(pins[name], prereleases=True), (
                f"{req.name}=={pins[name]} вне {req.specifier} — пересоберите lock")

    def test_pyav_в_lock_ниже_19(self):
        assert int(_pins()["av"].split(".")[0]) < 19

    def test_linux_зависимости_на_месте_windows_нет(self):
        pins = _pins()
        assert "uvloop" in pins            # uvicorn[standard] на Linux
        assert "colorama" not in pins      # только Windows
        assert "pywin32" not in pins


def test_ci_ставит_тот_же_lock():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "-c requirements.lock.txt" in ci

