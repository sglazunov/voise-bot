"""Скриншот неудачного входа открывается с карточки встречи (2026-09-21).

Бот при «Не удалось войти» сохранял `<запись>.join-failed.png` в папку
записей — и понять, ЧТО он увидел, можно было только по ssh. Теперь путь
живёт в карточке (`MeetingState.screenshot`), переживает перезапуск через
снапшот и отдаётся эндпоинтом `/api/automation/meetings/{id}/screenshot`."""
from __future__ import annotations

import pytest

from app import security
from app.automation import scheduler as sched_mod
from app.automation import snapshots
from app.automation.scheduler import MeetingState
from conftest import login, register


def _state(tmp_path, **over) -> MeetingState:
    kw = dict(key="team:42:2026-09-21T09:00:00+00:00", task_id="42",
              title="ОД сайт", url="https://telemost.yandex.ru/j/1",
              start=None, owner="team", state="error")
    kw.update(over)
    return MeetingState(**kw)


def test_карточка_знает_о_скриншоте_только_когда_файл_есть(tmp_path):
    st = _state(tmp_path, screenshot=str(tmp_path / "a.join-failed.png"))
    assert st.public()["has_screenshot"] is False, "файла ещё нет"
    (tmp_path / "a.join-failed.png").write_bytes(b"\x89PNG\r\n")
    assert st.public()["has_screenshot"] is True


def test_путь_скриншота_переживает_снапшот(tmp_path):
    st = _state(tmp_path, screenshot=str(tmp_path / "a.join-failed.png"))
    snap = snapshots.of_state(st)
    assert snap["screenshot"] == str(tmp_path / "a.join-failed.png")


class TestЭндпоинт:
    @pytest.fixture
    def user(self, client, monkeypatch, tmp_path):
        register(client)
        login(client)
        team = security.team_of("alice")
        # имя записи — как на бою: кириллица, запятые, точки
        st = _state(tmp_path, owner=team,
                    screenshot=str(tmp_path / "21.09.2026, 09:00. - ОД сайт.join-failed.png"))
        monkeypatch.setattr(sched_mod.scheduler, "_states", {st.key: st})
        return st

    def test_без_файла_404(self, client, user):
        r = client.get("/api/automation/meetings/42/screenshot")
        assert r.status_code == 404

    def test_png_и_html_отдаются(self, client, user, tmp_path):
        (tmp_path / "21.09.2026, 09:00. - ОД сайт.join-failed.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (tmp_path / "21.09.2026, 09:00. - ОД сайт.join-failed.html").write_text("<html>shell</html>", encoding="utf-8")
        r = client.get("/api/automation/meetings/42/screenshot")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.headers["cache-control"] == "no-store"
        assert r.content.startswith(b"\x89PNG")
        r = client.get("/api/automation/meetings/42/screenshot?kind=html")
        assert r.status_code == 200
        # HTML чужого сайта — только как файл, не как страница нашего домена
        cd = r.headers["content-disposition"]
        assert "attachment" in cd and cd.isascii(), "заголовок только латиницей"
        assert "telemost-42" in cd
        assert r.headers["content-type"].startswith("text/plain")
        assert "shell" in r.text
        assert client.get("/api/automation/meetings/42/screenshot?kind=exe").status_code == 400

    def test_чужая_встреча_не_отдаётся(self, client, user, tmp_path):
        (tmp_path / "21.09.2026, 09:00. - ОД сайт.join-failed.png").write_bytes(b"\x89PNG")
        from fastapi.testclient import TestClient
        from app.main import app
        other = TestClient(app)
        # телефон уникален — повторный даёт 400 и тест ловил бы 401 вместо 404
        assert register(other, "bob", phone="+79995556677").status_code == 200
        assert login(other, "bob").status_code == 200
        assert other.get("/api/automation/meetings/42/screenshot").status_code == 404
