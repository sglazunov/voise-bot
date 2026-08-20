"""Test bootstrap: isolate all on-disk state in a throwaway data dir and keep
the recorder/ollama off so tests are hermetic and fast.

These env vars must be set BEFORE app.config is imported (it reads them at import),
so conftest.py is the right place — pytest imports it first.
"""
import os
import tempfile

# FORCE (not setdefault!) the isolation env: inside the app container these
# variables are already set to PRODUCTION values (VTX_DATA_DIR=/data,
# DATABASE_URL=postgres://…), and setdefault would silently keep them — the
# suite would then read AND WIPE live data.
os.environ["VTX_DATA_DIR"] = tempfile.mkdtemp(prefix="vtx-test-")
os.environ["VTX_RECORDER_ENABLED"] = "0"
os.environ["VTX_OLLAMA"] = "0"
# Deterministic master key for the secret-encryption tests.
os.environ["VTX_SECRET_KEY"] = "test-master-key-do-not-use-in-prod"
os.environ.pop("DATABASE_URL", None)
# Тот же случай, что и с DATA_DIR: в боевом контейнере .env задаёт VTX_HTTPS=1
# (за Caddy), и кука сессии получает флаг Secure. TestClient ходит по
# http://testserver и такую куку не сохраняет — все проверки авторизации падали
# с 401 (13 штук), хотя приложение исправно. VTX_TRUST_PROXY=1 по той же логике
# заставляет брать IP клиента из X-Forwarded-For, которого в тестах нет.
os.environ["VTX_HTTPS"] = "0"
os.environ["VTX_TRUST_PROXY"] = "0"

import shutil  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402
from pathlib import Path  # noqa: E402

# Заглушка faster_whisper. Без неё не собирался НИ ОДИН тест: conftest тянет
# app.jobs → app.transcribe, а тот импортирует WhisperModel на уровне модуля.
# Из-за этого даже чистые тесты имён и Weeek требовали установленный движок
# распознавания (~1 ГБ зависимостей). Настоящая модель тестам не нужна: те, что
# реально распознают, помечены отдельно и в обычном прогоне не участвуют.
if "faster_whisper" not in sys.modules:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        stub = types.ModuleType("faster_whisper")

        class _StubModel:                     # pragma: no cover — только импорт
            def __init__(self, *a, **k):
                raise RuntimeError(
                    "faster-whisper не установлен: это заглушка для тестов, "
                    "распознавание в этом окружении недоступно.")

        stub.WhisperModel = _StubModel
        utils = types.ModuleType("faster_whisper.utils")
        utils._MODELS = {}
        stub.utils = utils
        sys.modules["faster_whisper"] = stub
        sys.modules["faster_whisper.utils"] = utils

import pytest  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app import config, security, sms  # noqa: E402
from app.jobs import store  # noqa: E402
from app.main import app  # noqa: E402


def _wipe_state() -> None:
    """Reset users, sessions, per-user dirs and the in-memory job store."""
    for name in ("users.json", "sessions.json", "recovery.json"):
        Path(config.DATA_DIR / name).unlink(missing_ok=True)
    udir = config.DATA_DIR / "users"
    if udir.exists():
        shutil.rmtree(udir, ignore_errors=True)
    store._jobs.clear()
    security._FAILED.clear()   # brute-force windows must not leak between tests
    sms.sent_messages.clear()  # sent-SMS log (recovery codes) must not leak


@pytest.fixture(autouse=True)
def clean_state():
    _wipe_state()
    yield
    _wipe_state()


@pytest.fixture
def client():
    # raise_server_exceptions=False so we assert on HTTP status, not tracebacks.
    return TestClient(app, raise_server_exceptions=True)


def register(client, username="alice", password="password123", code="",
             phone="+7 999 000-00-00"):
    return client.post("/api/auth/register",
                       json={"username": username, "password": password,
                             "code": code, "phone": phone})


def login(client, username="alice", password="password123"):
    return client.post("/api/auth/login",
                      json={"username": username, "password": password})
