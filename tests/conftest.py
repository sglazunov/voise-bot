"""Test bootstrap: isolate all on-disk state in a throwaway data dir and keep
the recorder/ollama off so tests are hermetic and fast.

These env vars must be set BEFORE app.config is imported (it reads them at import),
so conftest.py is the right place — pytest imports it first.
"""
import os
import tempfile

os.environ.setdefault("VTX_DATA_DIR", tempfile.mkdtemp(prefix="vtx-test-"))
os.environ.setdefault("VTX_RECORDER_ENABLED", "0")
os.environ.setdefault("VTX_OLLAMA", "0")
# Deterministic master key for the secret-encryption tests.
os.environ.setdefault("VTX_SECRET_KEY", "test-master-key-do-not-use-in-prod")
# NEVER let tests touch a real Postgres: inside the app container DATABASE_URL
# points at the PRODUCTION db, and _wipe_state would corrupt live users.
os.environ.pop("DATABASE_URL", None)

import shutil  # noqa: E402
from pathlib import Path  # noqa: E402

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
