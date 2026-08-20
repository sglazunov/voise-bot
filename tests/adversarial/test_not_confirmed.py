"""Атаки, которые НЕ подтвердились. Тесты здесь проходят — это доказательство,
что защита на месте, а не «мы посмотрели глазами».

Держим их в наборе: любая из этих границ может поехать при следующем разрезе
модулей, и тогда тест упадёт.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app import security
from app.automation.scheduler import MeetingState, scheduler
from app.jobs import store
from conftest import login, register


class TestPathTraversal:
    """Гипотеза: маршрут SPA `/{full_path:path}` отдаёт любой файл сервера."""

    def test_traversal_attempts_return_the_spa_shell(self, client):
        register(client)
        for path in ("../../data/secret.key",
                     "..%2f..%2fdata%2fsecret.key",
                     "static/../../../../etc/passwd",
                     "%2e%2e/%2e%2e/users.json",
                     "/etc/passwd"):
            r = client.get("/" + path.lstrip("/"))
            body = r.content
            assert b"pbkdf2" not in body and b"root:x:" not in body
            assert r.headers.get("content-type", "").startswith("text/html")


class TestCrossTeamJobs:
    """Гипотеза: чужой job_id даёт доступ к записи другой команды."""

    def test_every_per_job_route_is_404_for_a_stranger(self, client):
        register(client, "alice", phone="+7 999 000-00-01")
        job = store.create("Планёрка.mp4", "/data/uploads/x.mp4", "ru", False,
                           owner="alice")
        store.result_path(job.id, "txt").write_text("секрет", encoding="utf-8")
        client.post("/api/auth/logout")
        register(client, "mallory", phone="+7 999 000-00-02")

        gets = (f"/api/jobs/{job.id}", f"/api/jobs/{job.id}/partial",
                f"/api/jobs/{job.id}/result?format=txt")
        posts = (f"/api/jobs/{job.id}/cancel", f"/api/jobs/{job.id}/pause",
                 f"/api/jobs/{job.id}/retry", f"/api/jobs/{job.id}/reanalyze",
                 f"/api/jobs/{job.id}/redeliver", f"/api/jobs/{job.id}/notes")
        for p in gets:
            assert client.get(p).status_code == 404, p
        for p in posts:
            assert client.post(p, json={}).status_code == 404, p
        assert client.get("/api/jobs").json() == []


class TestCrossTeamMeetings:
    """Гипотеза: task_id чужой команды пускает к живой расшифровке и «Стоп»."""

    def test_meeting_routes_are_closed_for_a_stranger(self, client):
        register(client, "alice", phone="+7 999 000-00-01")
        st = MeetingState(key="alice:42:x", task_id=42, title="Планёрка",
                          url="https://telemost.yandex.ru/j/room1",
                          start=datetime.now(timezone.utc), owner="alice",
                          state="recording")
        st.live_text = "секретный разговор"
        scheduler._states[st.key] = st
        try:
            client.post("/api/auth/logout")
            register(client, "mallory", phone="+7 999 000-00-02")
            assert client.get("/api/automation/meetings/42/live").status_code == 404
            assert client.post("/api/automation/meetings/42/notes",
                               json={"notes": "hi"}).status_code == 404
            assert client.post(
                "/api/automation/scheduler/stop-recording?task_id=42"
            ).status_code == 400
            assert st.live_notes == "" and st.stop_flag is False
        finally:
            scheduler._states.clear()


class TestSecretsAtRest:
    """Гипотеза: секрет одной команды расшифровывается ключом другой."""

    def test_other_login_cannot_decrypt(self):
        token = security.encrypt_secret("alice", "wk-REAL-TOKEN")
        assert token != "wk-REAL-TOKEN"
        assert security.decrypt_secret("mallory", token) == ""
        assert security.decrypt_secret("alice", token) == "wk-REAL-TOKEN"


class TestSessionStore:
    """Гипотеза: утёкший sessions.json позволяет угнать сессию (в файле лежит
    сам токен)."""

    def test_only_the_hash_is_persisted(self, client):
        register(client)
        token = client.cookies.get(security.SESSION_COOKIE)
        raw = (security.config.DATA_DIR / "sessions.json").read_text(encoding="utf-8")
        assert token and token not in raw
        assert security._token_key(token) in raw


class TestBruteForce:
    """Гипотеза: подбор пароля не ограничен."""

    def test_login_is_throttled(self, client):
        register(client)
        codes = [client.post("/api/auth/login",
                             json={"username": "alice", "password": f"wrong{i}"}
                             ).status_code
                 for i in range(security.BRUTE_MAX_ATTEMPTS + 2)]
        assert 429 in codes, codes
        # И правильный пароль тоже не проходит, пока окно не остынет.
        assert login(client).status_code == 429


class TestXForwardedFor:
    """Гипотеза: заголовок X-Forwarded-For обходит счётчик попыток."""

    def test_spoofed_header_is_ignored_when_proxy_is_not_trusted(self, client):
        register(client)
        codes = []
        for i in range(security.BRUTE_MAX_ATTEMPTS + 2):
            codes.append(client.post(
                "/api/auth/login",
                json={"username": "alice", "password": f"wrong{i}"},
                headers={"X-Forwarded-For": f"10.0.0.{i}"}).status_code)
        assert 429 in codes, codes
