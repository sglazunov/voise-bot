"""Список адресов API зафиксирован: перенос маршрутов не должен их менять.

Маршруты автоматизации переехали из main.py в отдельный роутер. Такой перенос
легко ломает адрес молча — префикс роутера и путь маршрута складываются, и
опечатка даёт 404 не в тесте, а у пользователя. Поэтому полный список
зафиксирован здесь: любое добавление, удаление или переименование адреса теперь
требует осознанной правки этого файла.
"""
from __future__ import annotations

from app.main import app

EXPECTED = {
    "GET /api/automation/clouds/status",
    "GET /api/automation/meetings",
    "GET /api/automation/meetings/{task_id}/live",
    "GET /api/automation/meetings/{task_id}/notes",
    "GET /api/automation/recorder/audio-test",
    "GET /api/automation/recorder/login-status",
    "GET /api/automation/recorder/login/screen",
    "GET /api/automation/recorder/status",
    "GET /api/automation/scheduler/status",
    "GET /api/automation/settings",
    "GET /api/automation/weeek/probe",
    "GET /api/automation/weeek/projects",
    "GET /api/context",
    "GET /api/jobs",
    "GET /api/jobs/{job_id}",
    "GET /api/jobs/{job_id}/partial",
    "GET /api/jobs/{job_id}/result",
    "GET /api/model/status",
    "GET /api/ollama/status",
    "GET /api/presets",
    "GET /api/profile",
    "GET /api/prompt/default",
    "GET /api/protocol-delivery/status",
    "GET /api/providers",
    "GET /api/providers/keys",
    "GET /api/providers/nvidia/verify",
    "GET /api/search",
    "GET /api/setup/auto",
    "GET /api/setup/deps",
    "GET /api/stats",
    "GET /api/system/recommend",
    "PATCH /api/jobs/{job_id}/analysis",
    "POST /api/auth/login",
    "POST /api/auth/logout",
    "POST /api/auth/recover/request",
    "POST /api/auth/recover/verify",
    "POST /api/auth/register",
    "POST /api/automation/clouds/test",
    "POST /api/automation/meetings/links",
    "POST /api/automation/meetings/{task_id}/decision",
    "POST /api/automation/meetings/{task_id}/notes",
    "POST /api/automation/notify/test",
    "POST /api/automation/recorder/login",
    "POST /api/automation/recorder/login/action",
    "POST /api/automation/recorder/login/close",
    "POST /api/automation/recorder/login/open",
    "POST /api/automation/recorder/test",
    "POST /api/automation/scheduler/poll-now",
    "POST /api/automation/scheduler/run-now",
    "POST /api/automation/scheduler/stop-recording",
    "POST /api/automation/settings",
    "POST /api/context",
    "POST /api/jobs",
    "POST /api/jobs/{job_id}/ask",
    "POST /api/jobs/{job_id}/cancel",
    "POST /api/jobs/{job_id}/notes",
    "POST /api/jobs/{job_id}/pause",
    "POST /api/jobs/{job_id}/reanalyze",
    "POST /api/jobs/{job_id}/redeliver",
    "POST /api/jobs/{job_id}/regen-topic",
    "POST /api/jobs/{job_id}/resume",
    "POST /api/jobs/{job_id}/retry",
    "POST /api/model/download",
    "POST /api/presets",
    "POST /api/profile/delete",
    "POST /api/profile/password",
    "POST /api/profile/phone",
    "POST /api/profile/team/remove",
    "POST /api/providers/connect",
    "POST /api/providers/custom/refresh",
    "POST /api/providers/disconnect",
    "POST /api/providers/keys/remove",
    "POST /api/providers/nvidia/verify",
}


def _actual() -> set[str]:
    out = set()
    for r in app.routes:
        path = getattr(r, "path", "")
        if not path.startswith("/api"):
            continue
        for m in sorted(getattr(r, "methods", None) or []):
            if m in ("HEAD", "OPTIONS"):
                continue
            out.add(f"{m} {path}")
    return out


def test_адреса_api_не_изменились():
    actual = _actual()
    assert not (EXPECTED - actual), f"адреса ПРОПАЛИ: {sorted(EXPECTED - actual)}"
    assert not (actual - EXPECTED), (
        "новые адреса — допишите их в EXPECTED осознанно: "
        f"{sorted(actual - EXPECTED)}")
