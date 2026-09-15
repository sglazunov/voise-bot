"""Метрики ценности: протокол прочитали и он пригодился (docs/ТЗ-МЕТРИКИ §4—§5).

До этого ни одно продуктовое действие человека не фиксировалось — ни в базе,
ни в логах приложения; единственным следом была строка uvicorn в stdout, без
пользователя и без исхода. Поэтому §5 не существовал вовсе: «сколько встреч
обработано» растёт от календаря клиента, а не от полезности продукта, и
отличить работающий продукт от генератора файлов в пустоту было нечем.

⚠️ Главная ловушка, которую здесь и проверяем: событие «протокол открыт» ставит
ЧЕЛОВЕК. Если его поставит опрос из SPA или фоновая задача, метрика ценности
станет метрикой трафика.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import events, security, stats
from conftest import login, register


@pytest.fixture
def team(tmp_path, monkeypatch):
    """Файловый режим и одна команда на всех — для проверок журнала и сводки.

    ⚠️ НЕ autouse: класс с эндпоинтом работает на НАСТОЯЩИХ пользователях и
    каталогах (изоляцию команд иначе не проверить), а подменённый `team_of`
    сваливал бы туда всех в одну команду.
    """
    monkeypatch.setattr(security, "team_of", lambda u: "team")
    monkeypatch.setattr(events.security, "team_of", lambda u: "team")
    monkeypatch.setattr(events.security, "user_dir", lambda t: tmp_path)
    monkeypatch.setattr(events.db, "enabled", lambda: False)
    monkeypatch.setattr(stats.security, "team_of", lambda u: "team")
    monkeypatch.setattr(stats.security, "user_dir", lambda t: tmp_path)
    monkeypatch.setattr(stats.db, "enabled", lambda: False)


def json_dump(o) -> str:
    import json
    return json.dumps(o, ensure_ascii=False)


def _built(jid: str, at: float | None = None, ok: bool = True) -> None:
    """Строка собранного протокола — знаменатель §5."""
    job = type("J", (), {
        "id": jid, "owner": "u", "filename": "встреча.mp4", "duration": 3600.0,
        "speakers": 3, "status": "done", "finished_at": at or time.time(),
        "analysis_error": None if ok else "движок не ответил",
        "provider": "gemini", "preset": "planerka", "transcribe_sec": 2400.0,
        "analysis": {"tasks": [], "decisions": []} if ok else None,
        "llm_usage": {}, "stop_reason": "call_ended",
    })()
    stats.record(job)


# --------------------------------------------------------------------------- #
# 1. Событие
# --------------------------------------------------------------------------- #
class TestEventLog:
    def test_повтор_за_сутки_не_считается(self, team):
        """И7: дедупликация «встреча + человек + сутки». Иначе считались бы не
        читатели, а нажатия — человек возвращается к документу по три раза."""
        assert events.record(events.OPENED, user="сергей", job_id="j1") is True
        assert events.record(events.OPENED, user="сергей", job_id="j1") is False
        assert len(events._file_load("team")) == 1

    def test_первое_открытие_не_перезаписывается(self, team):
        """На первом событии стоит «время до первого открытия» (И11)."""
        events.record(events.OPENED, user="сергей", job_id="j1", at=1000.0)
        events.record(events.OPENED, user="сергей", job_id="j1", at=9000.0)
        assert events._file_load("team")[0]["at"] == 1000.0

    def test_разные_люди_считаются_отдельно(self, team):
        assert events.record(events.OPENED, user="сергей", job_id="j1") is True
        assert events.record(events.OPENED, user="зоя", job_id="j1") is True
        assert len({r["actor"] for r in events._file_load("team")}) == 2

    def test_логина_в_событии_нет(self, team):
        """И60—И65: витрина не должна знать имён. Псевдоним считается от
        мастер-ключа сервера и от команды."""
        events.record(events.OPENED, user="сергей", job_id="j1")
        row = events._file_load("team")[0]
        assert "сергей" not in json_dump(row)
        assert row["actor"] == security.pseudonym("сергей", "team")

    def test_действия_человека_отделены_от_служебных(self, team):
        events.record(events.OPENED, user="сергей", job_id="j1")
        events.record(events.OPENED, user="бот", job_id="j2", source=events.BOT)
        rows = events.load("team", 0)
        assert len(rows) == 2
        assert len(events.human_rows(rows)) == 1

    def test_повторные_действия_пишутся_каждое(self, team):
        """Правок за день бывает много, и каждая что-то говорит о качестве."""
        for _ in range(3):
            events.record(events.EDITED, user="сергей", job_id="j1",
                          once_a_day=False)
        assert len(events._file_load("team")) == 3


# --------------------------------------------------------------------------- #
# 2. Сводка
# --------------------------------------------------------------------------- #
class TestValueSummary:
    def test_прочитанным_считается_открытый_в_окне(self, team):
        now = time.time()
        _built("j1", at=now - 3600)          # прочитан через час
        _built("j2", at=now - 3600)          # не открывали
        events.record(events.OPENED, user="сергей", job_id="j1", at=now)
        s = stats.summary("u", days=30)
        assert (s["protocols_built"], s["protocols_read"]) == (2, 1)
        assert s["time_to_open_min"] == 60

    def test_открытие_позже_окна_не_считается_прочтением(self, team):
        """Окно 72 ч меряет свежесть пользы. Протокол, открытый через месяц, —
        это уже «возврат к архиву», другая метрика."""
        now = time.time()
        _built("j1", at=now - 30 * 86400)
        events.record(events.OPENED, user="сергей", job_id="j1", at=now)
        s = stats.summary("u", days=60)
        assert s["protocols_read"] == 0

    def test_окно_считается_от_готовности_а_не_от_запроса(self, team):
        """Иначе вчерашние встречи всегда «не прочитаны», а месячные всегда
        прочитаны — метрика меряла бы длину периода."""
        now = time.time()
        _built("j1", at=now - 20 * 86400)
        events.record(events.OPENED, user="зоя", job_id="j1",
                      at=now - 20 * 86400 + 600)
        assert stats.summary("u", days=30)["protocols_read"] == 1

    def test_несобравшийся_протокол_не_в_знаменателе(self, team):
        """Файл, которого нет, никто не мог прочитать."""
        now = time.time()
        _built("j1", at=now, ok=False)
        assert stats.summary("u", days=30)["protocols_built"] == 0

    def test_доля_не_показывается_при_малом_знаменателе(self, team):
        now = time.time()
        for i in range(5):
            _built(f"j{i}", at=now)
        events.record(events.OPENED, user="сергей", job_id="j0", at=now)
        s = stats.summary("u", days=30)
        assert s["read_ratio"] is None
        assert (s["protocols_built"], s["protocols_read"]) == (5, 1)

    def test_доля_считается_от_двадцати(self, team):
        now = time.time()
        for i in range(20):
            _built(f"j{i}", at=now)
        for i in range(15):
            events.record(events.OPENED, user="сергей", job_id=f"j{i}", at=now)
        s = stats.summary("u", days=30)
        assert s["read_ratio"] == pytest.approx(0.75)

    def test_читателей_на_протокол_медианой(self, team):
        """⚠️ Долю прочитанных без этого числа читать нельзя: одно открытие
        владельца, проверяющего бота, от чтения командой неотличимо."""
        now = time.time()
        _built("j1", at=now)
        for who in ("сергей", "зоя", "кирилл"):
            events.record(events.OPENED, user=who, job_id="j1", at=now)
        assert stats.summary("u", days=30)["readers_median"] == 3

    def test_уникальные_читатели_скрыты_пока_их_мало(self, team):
        """И63: при четырёх читателях доля вычисляется обратно до человека."""
        now = time.time()
        _built("j1", at=now)
        for who in ("сергей", "зоя", "кирилл"):
            events.record(events.OPENED, user=who, job_id="j1", at=now)
        assert stats.summary("u", days=30)["readers_total"] is None
        for who in ("павел", "анна"):
            events.record(events.OPENED, user=who, job_id="j1", at=now)
        assert stats.summary("u", days=30)["readers_total"] == 5

    def test_прочитали_не_равно_пригодилось(self, team):
        """И12: открытие в числитель «породил действие» НЕ входит."""
        now = time.time()
        _built("j1", at=now)
        _built("j2", at=now)
        events.record(events.OPENED, user="сергей", job_id="j1", at=now)
        events.record(events.EXPORTED, user="сергей", job_id="j2", at=now)
        s = stats.summary("u", days=30)
        assert s["protocols_read"] == 1
        assert s["protocols_acted"] == 1      # только j2

    def test_автоматическое_действие_не_идёт_в_ценность(self, team):
        now = time.time()
        _built("j1", at=now)
        events.record(events.OPENED, user="сергей", job_id="j1", at=now,
                      source=events.SYSTEM)
        events.record(events.TASK_CREATED, user="сергей", job_id="j1", at=now,
                      source=events.SYSTEM, once_a_day=False)
        s = stats.summary("u", days=30)
        assert (s["protocols_read"], s["protocols_acted"]) == (0, 0)


# --------------------------------------------------------------------------- #
# 3. Эндпоинт: настоящий вход, настоящая задача
# --------------------------------------------------------------------------- #
class TestOpenedEndpoint:
    @pytest.fixture
    def logged(self, client):
        # Настоящие пользователи и каталоги: conftest уводит VTX_DATA_DIR во
        # временную папку, так что боевые данные не трогаются.
        register(client, "alice")
        login(client, "alice")
        return client

    def _job(self, analysis, owner="alice"):
        # Кладём задачу прямо в хранилище: `store.create` поставил бы её в
        # очередь, и воркер полез бы в распознавание, которого в этом окружении
        # нет. Нам нужна только карточка с протоколом.
        from app.jobs import Job, store
        job = Job(id="job1", filename="встреча.mp4", audio_path="/tmp/x.mp4",
                  language="ru", diarize=False, owner=owner, status="done",
                  analysis=analysis)
        store._jobs[job.id] = job
        return job

    def test_встреча_без_протокола_события_не_даёт(self, logged):
        job = self._job(None)
        r = logged.post(f"/api/jobs/{job.id}/opened")
        assert r.status_code == 200 and r.json()["recorded"] is False
        assert events.load("alice", 0) == []

    def test_открытие_протокола_пишется_один_раз(self, logged):
        job = self._job({"tasks": [], "decisions": []})
        assert logged.post(f"/api/jobs/{job.id}/opened").json()["recorded"] is True
        assert logged.post(f"/api/jobs/{job.id}/opened").json()["recorded"] is False
        rows = events.load("alice", 0)
        assert len(rows) == 1 and rows[0]["kind"] == events.OPENED
        assert rows[0]["source"] == events.HUMAN
        assert rows[0]["job_id"] == job.id

    def test_опрос_событий_не_создаёт(self, logged):
        """⚠️ Главная ловушка И7. SPA опрашивает список и карточку задачи каждые
        две секунды, пока идёт работа; docx-ссылка тоже дёргается интерфейсом.
        Если событие «прочитано» поставит опрос, доля прочитанных протоколов
        станет долей открытых вкладок. Регрессия на будущее: не добавлять
        `events.record` в читающие маршруты."""
        job = self._job({"tasks": [], "decisions": []})
        logged.get("/api/jobs")
        logged.get(f"/api/jobs/{job.id}")
        logged.get(f"/api/jobs/{job.id}/partial")
        assert events.load("alice", 0) == []

    def test_чужую_задачу_открыть_нельзя(self, logged, client):
        """Изоляция команд: событие не должно быть лазейкой к чужим задачам."""
        job = self._job({"tasks": []})
        from starlette.testclient import TestClient
        from app.main import app
        other = TestClient(app)
        # ⚠️ Телефон должен быть СВОЙ: он уникален, и повторный отдаёт 400 —
        # регистрация молча не произошла бы, а тест ловил бы 401 вместо 404.
        assert register(other, "bob", phone="+79995556677").status_code == 200
        assert login(other, "bob").status_code == 200
        assert other.post(f"/api/jobs/{job.id}/opened").status_code == 404
