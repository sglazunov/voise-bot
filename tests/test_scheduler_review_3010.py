"""Планировщик, облако, протокол — раздел 6 и дыры раздела 7 адверсариального
ревью 30.10 (docs/АДВЕРСАРИАЛЬНОЕ-РЕВЮ-2026-10-30.md).

Каждый тест класса находки падал на коде до правки. Тесты класса «Мутанты»
закрывают выживших мутантов: при минимальной поломке строки они краснеют, а
весь прежний набор оставался зелёным.
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import analyze, usage
from app import weeek_tasks as wt
from app.automation import scheduler as sched_mod
from app.automation import snapshots
from app.automation.scheduler import MeetingState, Scheduler
from app.jobs import store

KEY = "team:42:2026-10-30T10:00:00+00:00"
URL = "https://telemost.yandex.ru/j/42"
_REAL_LATE = Scheduler._late_upload      # до подмен в фикстурах


class _Slot:
    index = 0


def _card(**over) -> MeetingState:
    kw = dict(key=KEY, task_id="42", title="Планёрка", url=URL, start=None,
              owner="team", state="scheduled")
    kw.update(over)
    return MeetingState(**kw)


class _Env:
    """Обвязка `_run` и `_late_upload`: облако и Weeek — заглушки уровня
    `clouds`, сама доставка (`delivery`) — настоящая."""

    def __init__(self, monkeypatch, tmp_path, cfg=None):
        self.tmp = tmp_path
        self.cfg = {"do_transcribe": False, "live_transcribe": False,
                    "weeek_token": "tok", "weeek_video_field": "Видео",
                    "weeek_set_video_field": True, "post_back_to_weeek": False,
                    "cloud": "yandex_disk"}
        if cfg:
            self.cfg.update(cfg)
        self.parts: list[Path] = []
        self.uploads: list[tuple[str, str | None]] = []   # (файл, owner)
        self.upload_results: list[dict] = []
        self.published: list[str] = []
        self.fields: list[tuple] = []
        self.comments: list[tuple] = []
        self.late: list[tuple] = []                         # (st, out, first_delay)
        self.sched = Scheduler()
        mp = monkeypatch
        mp.setattr(sched_mod.auto_settings, "load", lambda u: dict(self.cfg))
        mp.setattr(sched_mod.recorder, "release_slot", lambda s: None)
        mp.setattr(sched_mod.recorder, "record_meeting", self._record)
        mp.setattr(sched_mod.stats, "record_meeting", lambda st, o: None)
        mp.setattr(sched_mod, "_meeting_place",
                   lambda *a, **k: {"sub": "", "kind": "flat", "key": "", "date": ""})
        mp.setattr(sched_mod.cloud_folders, "placed", lambda *a, **k: None)
        # «10:00» в имени записи — на NTFS это поток данных, а не имя файла.
        free = sched_mod._free_path
        mp.setattr(sched_mod, "_free_path",
                   lambda p: free(p.with_name(p.name.replace(":", "-"))))
        orig = sched_mod.delivery.upload_with_retry
        # Без пауз между попытками: одна попытка на вызов.
        mp.setattr(sched_mod.delivery, "upload_with_retry",
                   lambda out, cfg, log, attempts=3, **kw: orig(out, cfg, log,
                                                                attempts=1, **kw))
        mp.setattr(sched_mod.delivery.clouds, "upload", self._upload)
        mp.setattr(sched_mod.delivery.clouds, "publish", self._publish)
        mp.setattr(sched_mod.delivery, "write_weeek_field",
                   lambda token, tid, field, value, log, **k: self.fields.append(
                       (token, tid, field, value)) or True)
        mp.setattr(sched_mod.weeek, "add_comment",
                   lambda token, tid, text: self.comments.append((token, tid, text)) or True)
        self.orig_late = _REAL_LATE
        mp.setattr(Scheduler, "_late_upload",
                   lambda s, user, st, cfg, out, first_delay=300:
                   self.late.append((st, out, first_delay)))

    def _record(self, url, out, cfg, **kw):
        p = self.tmp / f"part{len(self.parts) + 1}.mp4"
        p.write_bytes(b"v" * 100)
        self.parts.append(p)
        return {"ok": True, "path": str(p), "reason": "stopped", "size": 100}

    def _upload(self, path, name, settings, **kw):
        self.uploads.append((path, kw.get("owner")))
        res = self.upload_results.pop(0) if self.upload_results else {
            "ok": False, "error": "облако недоступно"}
        if not res.get("ok"):
            self.sched._stop.set()        # дозаливка не крутится вечно
        return res

    def _publish(self, remote, settings, backend=None):
        self.published.append(remote)
        return {"ok": True, "backend": "yandex_disk",
                "url": "https://disk/published-" + remote.rsplit("/", 1)[-1]}

    def run(self, st):
        self.sched._run(st, _Slot(), True)

    def late_upload(self, st, out, first_delay=0):
        self.sched._stop.clear()
        self.orig_late(self.sched, st.owner, st, dict(self.cfg), out,
                       first_delay=first_delay)


def _ok(n):
    return {"ok": True, "backend": "yandex_disk", "url": f"https://disk/p{n}",
            "path": f"disk:/Записи/part{n}.mp4"}


FAIL = {"ok": False, "error": "облако недоступно"}


# --------------------------------------------------------------------------- #
# 6.1 (В) Повторный заход + неудачная выгрузка второй части: потеря записи
# --------------------------------------------------------------------------- #
class TestВтораяЧастьНеТеряется:
    def test_дозаливка_не_публикует_путь_первой_части(self, monkeypatch, tmp_path):
        env = _Env(monkeypatch, tmp_path)
        st = _card()
        env.upload_results = [_ok(1), FAIL]
        env.run(st)                                  # часть 1 уехала
        assert st.cloud_url == "https://disk/p1"
        env.run(st)                                  # повторный заход, часть 2 — нет
        p2 = env.parts[1]
        assert st.out_path == str(p2)
        assert st.cloud_path is None and st.cloud_url is None, \
            "у второй части остался путь/ссылка первой"

        env.upload_results = [_ok(2)]
        env.late_upload(st, str(p2))
        assert env.published == [], "дозаливка опубликовала файл первой части"
        assert env.uploads[-1][0] == str(p2), "вторая часть так и не уехала"
        assert st.cloud_url == "https://disk/p2"
        assert env.fields[-1][3] == "https://disk/p2"

    def test_снапшот_второй_части_без_ссылки_первой(self, monkeypatch, tmp_path):
        env = _Env(monkeypatch, tmp_path)
        st = _card()
        env.upload_results = [_ok(1)]
        env.run(st)
        seen = {}

        def record(url, out, cfg, **kw):
            seen.update(snapshots.load("team")[KEY])
            return env._record(url, out, cfg, **kw)
        monkeypatch.setattr(sched_mod.recorder, "record_meeting", record)
        env.run(st)
        assert seen["cloud_url"] is None and seen["cloud_path"] is None


# --------------------------------------------------------------------------- #
# 6.4 (С) Падение во время второй части: выгрузка не пропускается
# --------------------------------------------------------------------------- #
class _Crash(BaseException):
    """Процесс умер посреди записи (except Exception его не ловит)."""


class TestПадениеВоВремяВторойЧасти:
    def test_осиротевшая_вторая_часть_выгружается(self, monkeypatch, tmp_path):
        env = _Env(monkeypatch, tmp_path)
        st = _card()
        env.upload_results = [_ok(1)]
        env.run(st)

        def crash(url, out, cfg, **kw):
            p = tmp_path / "part2.mp4"
            p.write_bytes(b"v" * 100)
            # рекордер пишет в путь, заранее сохранённый в снапшоте
            snap = snapshots.load("team")[KEY]
            Path(snap["out_path"]).parent.mkdir(parents=True, exist_ok=True)
            env.crash_out = snap["out_path"]
            raise _Crash()
        monkeypatch.setattr(sched_mod.recorder, "record_meeting", crash)
        with pytest.raises(_Crash):
            env.run(st)
        # Перезапуск: файл, начатый второй частью, лежит на диске.
        snap = snapshots.load("team")[KEY]
        Path(snap["out_path"]).write_bytes(b"v" * 100)
        monkeypatch.setattr(sched_mod.security, "list_teams", lambda: ["team"])
        env.upload_results = [_ok(2)]
        env.sched = Scheduler()
        env.sched._resume_pending()
        assert [u[0] for u in env.uploads[1:]] == [snap["out_path"]], \
            "вторая часть не выгружена: в снапшоте была ссылка первой"


# --------------------------------------------------------------------------- #
# 6.2 (В) Дыры P0-4 и 6.3 (В) out_path карточки опроса
# --------------------------------------------------------------------------- #
@pytest.fixture
def resume(monkeypatch):
    calls = []
    done = threading.Event()

    def fake_late(self, user, st, cfg_, out, first_delay=300):
        calls.append((st, out, first_delay))
        done.set()

    monkeypatch.setattr(Scheduler, "_late_upload", fake_late)
    monkeypatch.setattr(Scheduler, "_await_and_upload_protocol",
                        lambda self, *a, **k: None)
    monkeypatch.setattr(sched_mod.security, "list_teams", lambda: ["team"])
    monkeypatch.setattr(sched_mod.store, "redeliver", lambda *a, **k: None)
    state = {"cfg": {"weeek_token": "tok", "do_protocol": True, "upload_protocol": True}}
    monkeypatch.setattr(sched_mod.auto_settings, "load", lambda u: dict(state["cfg"]))

    def wait(n=1, timeout=2.0):
        end = time.time() + timeout
        while len(calls) < n and time.time() < end:
            time.sleep(0.01)
        time.sleep(0.05)
        return calls

    return type("R", (), {"calls": calls, "done": done, "state": state,
                          "wait": staticmethod(wait)})


def _snap(tmp_path, state="done", job=True, name="rec", **over) -> MeetingState:
    rec = tmp_path / f"{name}.mp4"
    rec.write_bytes(b"x" * 64)
    jid = store.create(rec.name, str(rec), "ru", False, owner="team").id if job else None
    kw = dict(state=state, out_path=str(rec), job_id=jid,
              upload_error="облако недоступно")
    kw.update(over)
    st = _card(**kw)
    snapshots.save(st)
    return st


class TestДозаливкаПослеРестартаДыры:
    def test_команда_только_запись_без_задачи(self, tmp_path, resume):
        st = _snap(tmp_path, job=False)
        Scheduler()._resume_pending()
        assert [c[1] for c in resume.wait()] == [st.out_path]

    def test_без_токена_weeek(self, tmp_path, resume):
        resume.state["cfg"] = {"do_protocol": True}
        st = _snap(tmp_path)
        Scheduler()._resume_pending()
        assert [c[1] for c in resume.wait()] == [st.out_path]

    def test_без_токена_weeek_встреча_в_работе(self, tmp_path, resume):
        resume.state["cfg"] = {"do_protocol": True}
        st = _snap(tmp_path, state="transcribing")
        Scheduler()._resume_pending()
        assert [c[1] for c in resume.wait()] == [st.out_path]

    def test_застрявшая_первая_часть_после_повторного_захода(self, monkeypatch, tmp_path,
                                                            resume):
        env = _Env(monkeypatch, tmp_path)
        st = _card()
        env.upload_results = [FAIL, _ok(2)]
        env.run(st)                                  # часть 1 не уехала
        env.run(st)                                  # часть 2 уехала
        p1 = str(env.parts[0])
        # перезапуск
        monkeypatch.setattr(Scheduler, "_late_upload",
                            lambda self, user, st, cfg_, out, first_delay=300:
                            resume.calls.append((st, out, first_delay)))
        monkeypatch.setattr(sched_mod.auto_settings, "load",
                            lambda u: {"weeek_token": "tok"})
        Scheduler()._resume_pending()
        assert [c[1] for c in resume.wait()] == [p1], \
            "застрявшая первая часть после рестарта не дозаливается"

    def test_карточка_опроса_получает_путь_записи(self, tmp_path):
        """_poll создал карточку раньше _resume_pending: путь записи из
        снапшота не переносился, и следующий снапшот был уже без него."""
        _snap(tmp_path)
        st = _card()
        Scheduler()._restore_snapshot(st, snapshots.load("team"))
        assert st.out_path and st.out_path.endswith("rec.mp4")

    def test_неудачная_дозаливка_карточки_опроса_не_теряет_путь(self, monkeypatch,
                                                                 tmp_path, resume):
        _snap(tmp_path)
        s = Scheduler()
        polled = _card()
        s._restore_snapshot(polled, snapshots.load("team"))
        s._states[KEY] = polled
        s._resume_pending()
        st, out, _ = resume.wait()[0]
        assert st is polled
        # первая попытка дозаливки сохранила снапшот — путь в нём остался
        snapshots.save(st)
        assert snapshots.load("team")[KEY]["out_path"] == out


class TestСнапшотНаPostgres:
    """На Postgres у таблицы `meetings` было восемь колонок, и снапшот терял
    всё остальное: ошибку выгрузки, путь в облаке, название, прежние части.
    Дозаливка после перезапуска на бою не срабатывала НИКОГДА — признак
    «облако не приняло» до неё не доезжал."""

    def test_поля_без_своей_колонки_доезжают(self, monkeypatch):
        from app import db
        rows: dict = {}

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=()):
                if sql.lstrip().startswith("INSERT INTO meetings"):
                    names = ("username", "meeting_key", "state", "detail", "job_id",
                             "cloud_url", "out_path", "live_notes", "do_protocol",
                             "saved_at", "extra")
                    row = dict(zip(names, params))
                    row["extra"] = getattr(row["extra"], "obj", row["extra"])
                    rows[row["meeting_key"]] = row

            def fetchall(self):
                return list(rows.values())

        class Conn(Cur):
            pass

        monkeypatch.setattr(db, "enabled", lambda: True)
        monkeypatch.setattr(db, "_conn", lambda: Conn())
        monkeypatch.setattr(db, "_cur", lambda conn: Cur())
        st = _card(state="done", out_path="/rec/p2.mp4", upload_error="облако недоступно",
                   cloud_path="disk:/Записи/p2.mp4",
                   parts=[{"out_path": "/rec/p1.mp4", "cloud_path": None,
                           "job_id": None, "upload_error": "x"}])
        snapshots.save(st)
        snap = snapshots.load("team")[KEY]
        assert snap["upload_error"] == "облако недоступно"
        assert snap["cloud_path"] == "disk:/Записи/p2.mp4"
        assert snap["title"] == "Планёрка"
        assert snap["parts"][0]["out_path"] == "/rec/p1.mp4"
        assert snap["out_path"] == "/rec/p2.mp4" and snap["state"] == "done"


# --------------------------------------------------------------------------- #
# 6.7 (Н) start() атомарен; одна дозаливка на файл
# --------------------------------------------------------------------------- #
class TestОдинЗапуск:
    def test_resume_pending_один_раз_при_гонке_start(self, monkeypatch):
        started = []

        class SlowThread:
            def __init__(self, *a, target=None, name="", **k):
                time.sleep(0.05)            # окно между проверкой и присвоением
                self.name, self.alive = name, False

            def start(self):
                started.append(self.name)
                self.alive = True

            def is_alive(self):
                return self.alive

        real = threading.Thread
        monkeypatch.setattr(sched_mod.threading, "Thread", SlowThread)
        s = Scheduler()
        ts = [real(target=s.start) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert started.count("vtx-resume") == 1
        assert started.count("vtx-scheduler") == 1

    def test_перезапуск_цикла_не_повторяет_восстановление(self, monkeypatch):
        started = []

        class T:
            def __init__(self, *a, target=None, name="", **k):
                self.name = name

            def start(self):
                started.append(self.name)

            def is_alive(self):
                return False                # цикл умер

        monkeypatch.setattr(sched_mod.threading, "Thread", T)
        s = Scheduler()
        s.start()
        s.start()
        assert started.count("vtx-scheduler") == 2
        assert started.count("vtx-resume") == 1

    def test_вторая_дозаливка_того_же_файла_не_запускается(self, monkeypatch, tmp_path):
        gate = threading.Event()
        calls = []

        def slow_late(self, user, st, cfg, out, first_delay=300):
            calls.append(out)
            gate.wait(2)

        monkeypatch.setattr(Scheduler, "_late_upload", slow_late)
        s = Scheduler()
        st = _card(out_path=str(tmp_path / "a.mp4"))
        s._start_late_upload("team", st, {}, st.out_path)
        s._start_late_upload("team", st, {}, st.out_path)
        time.sleep(0.1)
        gate.set()
        assert calls == [st.out_path]
        time.sleep(0.1)
        s._start_late_upload("team", st, {}, st.out_path)   # прежняя закончилась
        time.sleep(0.1)
        assert len(calls) == 2


# --------------------------------------------------------------------------- #
# 6.5 (С) Срок задачи — не номер версии после «до»
# --------------------------------------------------------------------------- #
class TestВерсияПослеДо:
    BASE = date(2026, 10, 30)

    @pytest.mark.parametrize("text", ["Обновить до версии 2.4.1", "Обновить до 3.2",
                                      "Поднять до v2.4", "Релиз до версии 2.4",
                                      # по отдельности: хвост «.1» и слово «версия»
                                      "Обновить до 12.10.1", "Обновить до версии 12.10",
                                      "Обновить до v12.10"])
    def test_номер_версии_не_срок(self, text):
        assert wt.parse_due(text, self.BASE, explicit=True) == (None, None)
        assert wt.parse_due(text, self.BASE) == (None, None)

    def test_настоящие_даты_работают(self):
        assert wt.parse_due("до 05.11", self.BASE, explicit=True)[0] == "2026-11-05"
        assert wt.parse_due("до 5.11", self.BASE, explicit=True)[0] == "2026-11-05"
        assert wt.parse_due("до 05.11.2026", self.BASE, explicit=True)[0] == "2026-11-05"


# --------------------------------------------------------------------------- #
# 6.6 (С) Архив локального облака — папка своей команды
# --------------------------------------------------------------------------- #
class TestАрхивЛокальногоОблака:
    def test_папка_команды_и_чужие_файлы(self, monkeypatch, tmp_path):
        from app import config
        from scripts import sort_cloud_archive as sca
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        base = tmp_path / "recordings"
        mine = base / "alpha"
        mine.mkdir(parents=True)
        (base / "beta").mkdir()
        (mine / "20.10.2026, 10-00. - Встреча с ОО.mp4").write_bytes(b"v")
        (base / "beta" / "21.10.2026, 10-00. - Чужая.mp4").write_bytes(b"v")
        (base / "22.10.2026, 10-00. - Старая общая.mp4").write_bytes(b"v")
        cfg = {"cloud": "local", "folders_by_series": True,
               "oneoff_folder": "Разовые встречи"}
        cloud = sca.backend_for(cfg, "alpha")
        files, _ = sca.collect(cloud)
        assert [f["name"] for f in files] == ["20.10.2026, 10-00. - Встреча с ОО.mp4"]
        groups = sca.plan("alpha", cfg, cloud, files)
        dst = Path(groups[0]["moves"][0]["dst"])
        assert mine in dst.parents


# --------------------------------------------------------------------------- #
# Раздел 7: выжившие мутанты зоны планировщика
# --------------------------------------------------------------------------- #
class TestМутантыДозаливки:
    def test_владелец_локального_облака_во_всех_выгрузках(self, monkeypatch, tmp_path):
        env = _Env(monkeypatch, tmp_path)
        st = _card(owner="team")
        env.upload_results = [FAIL]
        env.run(st)                                       # _run
        env.upload_results = [FAIL]
        env.late_upload(st, st.out_path)                  # _late_upload
        orphan = tmp_path / "orphan.mp4"
        orphan.write_bytes(b"v")
        env.upload_results = [FAIL]
        env.sched._finalize_orphan(_card(owner="team"), dict(env.cfg), str(orphan))
        assert [o for _, o in env.uploads] == ["team", "team", "team"]

    @pytest.mark.parametrize("state", ["done", "transcribing"])
    def test_пауза_первой_попытки_после_рестарта(self, tmp_path, resume, state):
        _snap(tmp_path, state=state)
        Scheduler()._resume_pending()
        assert resume.wait()[0][2] == 30

    def test_пауза_дозаливки_из_записи(self, monkeypatch, tmp_path):
        env = _Env(monkeypatch, tmp_path)
        env.upload_results = [FAIL]
        env.run(_card())
        time.sleep(0.05)
        assert env.late and env.late[0][2] == 300

    def test_после_первой_попытки_пауза_300(self, monkeypatch, tmp_path):
        env = _Env(monkeypatch, tmp_path)
        st = _card(out_path=str(tmp_path / "a.mp4"), upload_error="x")
        Path(st.out_path).write_bytes(b"v")
        sleeps = []

        def sleep(sec):
            sleeps.append(sec)
            if len(sleeps) >= 3:
                env.sched._stop.set()
        monkeypatch.setattr(sched_mod.time, "sleep", sleep)
        monkeypatch.setattr(sched_mod.delivery.clouds, "upload",
                            lambda *a, **k: {"ok": False, "error": "x"})
        env.orig_late(env.sched, "team", st, dict(env.cfg), st.out_path, first_delay=30)
        assert sleeps[:3] == [30, 300, 300]

    def test_ссылка_уже_в_облаке_при_ошибке_выгрузки(self, tmp_path, resume):
        _snap(tmp_path, cloud_url="https://disk/x")
        Scheduler()._resume_pending()
        assert resume.wait(timeout=0.3) == []

    def test_без_ошибки_выгрузки_не_дозаливается(self, tmp_path, resume):
        _snap(tmp_path, upload_error=None)
        Scheduler()._resume_pending()
        assert resume.wait(timeout=0.3) == []

    def test_файла_на_диске_нет(self, tmp_path, resume):
        st = _snap(tmp_path)
        Path(st.out_path).unlink()
        Scheduler()._resume_pending()
        assert resume.wait(timeout=0.3) == []

    def test_карточка_опроса_не_подменяется(self, tmp_path, resume):
        _snap(tmp_path)
        s = Scheduler()
        polled = _card(url=URL)
        s._states[KEY] = polled
        s._resume_pending()
        st, out, _ = resume.wait()[0]
        assert s._states[KEY] is polled and st is polled
        assert polled.url == URL
        assert polled.out_path == out, "карточке опроса не дописан путь записи"

    @pytest.mark.parametrize("state", ["done", "transcribing"])
    def test_путь_в_облаке_восстанавливается(self, tmp_path, resume, state):
        _snap(tmp_path, state=state, cloud_path="disk:/Записи/rec.mp4")
        Scheduler()._resume_pending()
        assert resume.wait()[0][0].cloud_path == "disk:/Записи/rec.mp4"

    def test_путь_в_облаке_в_карточке_опроса(self, tmp_path):
        _snap(tmp_path, cloud_path="disk:/Записи/rec.mp4")
        st = _card()
        Scheduler()._restore_snapshot(st, snapshots.load("team"))
        assert st.cloud_path == "disk:/Записи/rec.mp4"

    @pytest.mark.parametrize("state", ["done", "transcribing"])
    def test_название_из_снапшота(self, tmp_path, resume, state):
        _snap(tmp_path, state=state, name="30.10.2026, 10-00. - Планёрка")
        Scheduler()._resume_pending()
        assert resume.wait()[0][0].title == "Планёрка"

    def test_пустой_токен_weeek_не_пишется(self, monkeypatch, tmp_path):
        env = _Env(monkeypatch, tmp_path, cfg={"weeek_token": "",
                                               "post_back_to_weeek": True})
        st = _card(out_path=str(tmp_path / "a.mp4"), upload_error="x")
        Path(st.out_path).write_bytes(b"v")
        env.upload_results = [_ok(1)]
        env.late_upload(st, st.out_path)
        assert st.cloud_url == "https://disk/p1"
        assert env.fields == [] and env.comments == []

    def test_дозаливка_первой_части_не_трогает_вторую(self, monkeypatch, tmp_path):
        env = _Env(monkeypatch, tmp_path, cfg={"post_back_to_weeek": True})
        st = _card()
        env.upload_results = [FAIL, FAIL]
        env.run(st)
        env.run(st)                       # обе части пока не в облаке
        p1, p2 = env.parts
        env.fields.clear()
        env.comments.clear()
        env.upload_results = [_ok(1)]
        env.late_upload(st, str(p1))
        assert st.out_path == str(p2) and st.cloud_url is None
        assert st.upload_error, "вторая часть «доставлена» ссылкой первой"
        assert env.fields == [], "поле «Видео» перезаписано ссылкой первой части"
        assert env.comments and "https://disk/p1" in env.comments[0][2]
        # Запись части остаётся (в ней задача и номер части — второй раунд
        # ревью), но уже со ссылкой и без ошибки: дозаливать её больше нечего.
        part = snapshots.load("team")[KEY]["parts"][0]
        assert part["cloud_url"] == "https://disk/p1" and not part["upload_error"]
        assert st.parts[0]["cloud_url"] == "https://disk/p1"
        assert p2.exists() and not p1.exists()


class TestМутантыКомнаты:
    @pytest.fixture
    def s(self, monkeypatch):
        monkeypatch.setenv("VTX_RECORDER_ENABLED", "1")
        monkeypatch.setattr(sched_mod.stats, "record_meeting", lambda st, o: None)
        monkeypatch.setattr(sched_mod.snapshots, "save", lambda st: None)
        monkeypatch.setattr(sched_mod.auto_settings, "load", lambda u: {"lookahead_min": 2})
        s = Scheduler()
        s._boot_time = 0
        s.started, s.acquired = [], []
        monkeypatch.setattr(sched_mod.recorder, "release_slot", lambda slot: None)

        def acquire():
            s.acquired.append(1)
            return f"slot-{len(s.acquired)}"
        monkeypatch.setattr(sched_mod.recorder, "acquire_slot", acquire)

        class T:
            def __init__(self, *a, target=None, args=(), **k):
                self.args = args

            def start(self):
                s.started.append(self.args[0])
        monkeypatch.setattr(sched_mod.threading, "Thread", T)
        return s

    def test_запись_другой_команды_в_той_же_комнате_не_мешает(self, s):
        now = datetime.now(timezone.utc)
        s._states["other:1:x"] = MeetingState(
            key="other:1:x", task_id="1", title="Чужая", url=URL, start=now,
            owner="other", state="recording")
        mine = MeetingState(key="t:2:x", task_id="2", title="Своя", url=URL,
                            start=now, owner="t", state="scheduled")
        s._states[mine.key] = mine
        with s._lock:
            assert s._recording_in_room("t", mine) is None
        s._maybe_trigger("t", {"lookahead_min": 2})
        assert s.started == [mine]

    def test_занятая_комната_помечается_и_разбирается(self, s, monkeypatch):
        now = datetime.now(timezone.utc)
        a = MeetingState(key="t:200:a", task_id="200", title="A", url=URL,
                         start=now - timedelta(seconds=5), owner="t", state="scheduled")
        b = MeetingState(key="t:201:b", task_id="201", title="B", url=URL,
                         start=now - timedelta(seconds=4), owner="t", state="scheduled")
        s._states[a.key], s._states[b.key] = a, b
        adhoc = MeetingState(key="t:link-1:x", task_id="link-20261030-ab12",
                             title="По ссылке", url=URL, start=now, owner="t",
                             state="scheduled")

        def acquire():
            s.acquired.append(1)
            with s._lock:                 # «по ссылке» зашёл между проверкой и захватом
                adhoc.state = "recording"
                s._states[adhoc.key] = adhoc
            return "slot"
        monkeypatch.setattr(sched_mod.recorder, "acquire_slot", acquire)
        s._maybe_trigger("t", {"lookahead_min": 2})
        assert s.started == []
        assert a.state == "skipped" and b.state == "skipped", "занятая ветка не разобрана"
        assert len(s.acquired) == 1, "комната не помечена занятой — второй слот взят зря"


class TestМутантыРасхода:
    def _job(self, monkeypatch, jid):
        from app.jobs import STATUS_DONE, Job
        job = Job(id=jid, filename="Планёрка.mp4", audio_path="", language="ru",
                  diarize=False, owner="alice", analyze=True, status=STATUS_DONE,
                  created_at=time.time(), analysis={"detailed": [{"topic": "t"}]})
        store._jobs[job.id] = job
        txt = store.result_path(jid, "txt")
        txt.parent.mkdir(parents=True, exist_ok=True)
        txt.write_text("[00:00:01] текст встречи", encoding="utf-8")
        logged = []
        from app import events
        monkeypatch.setattr(events, "record_many",
                            lambda rows, **kw: logged.extend(rows) or len(rows))
        return job, logged

    def test_regen_при_ошибке(self, monkeypatch):
        job, logged = self._job(monkeypatch, "mut-regen")

        def fail(*a, **k):
            usage.record("gemini/x", 400, 0, 40)
            raise RuntimeError("шлюз упал")
        monkeypatch.setattr(analyze, "regen_topic_details", fail)
        with pytest.raises(RuntimeError):
            store.regen_topic(job.id, 0)
        assert job.llm_usage.get("calls") == 1 and job.llm_usage["in"] == 400
        assert logged and all(r.get("rerun") for r in logged)

    def test_ask_при_ошибке(self, monkeypatch):
        job, logged = self._job(monkeypatch, "mut-ask")

        def fail(*a, **k):
            usage.record("gemini/x", 300, 0, 30)
            raise RuntimeError("шлюз упал")
        monkeypatch.setattr(analyze, "ask_meeting", fail)
        with pytest.raises(RuntimeError):
            store.ask(job.id, "что решили?")
        assert job.llm_usage.get("calls") == 1 and job.llm_usage["in"] == 300

    def test_первый_прогон_не_пересборка(self, monkeypatch):
        job, logged = self._job(monkeypatch, "mut-first")

        def ok(*a, **k):
            usage.record("gemini/x", 100, 0, 10)
            return {"summary": "s"}
        monkeypatch.setattr(analyze, "analyze_transcript", ok)
        monkeypatch.setattr(store, "_maybe_verify", lambda job, r, t: r)
        store._protocol_pass(job, "текст", context="")
        assert logged and not any(r.get("rerun") for r in logged)

    def test_первый_прогон_в_конвейере_не_пересборка(self):
        """Вызов в конвейере задачи — без rerun: пересборкой считается только
        `_do_reanalyze`. Проверяем по исходнику — гонять весь конвейер ради
        одного аргумента дороже, чем он стоит."""
        import inspect
        from app import jobs
        args = {}
        for name in ("_process", "_do_reanalyze"):
            src = inspect.getsource(getattr(jobs.JobStore, name))
            call = src.split("self._protocol_pass(")[1]
            args[name] = call[:call.index(")")]
        assert "rerun" not in args["_process"]
        assert "rerun=True" in args["_do_reanalyze"]


class TestМутантыСроков:
    BASE = date(2026, 10, 30)

    def test_без_подсказки_дата_в_тексте_задачи_не_срок(self):
        # Год не указан — проверка «далёкого прошлого» не спасает, решает
        # только требование подсказки.
        assert wt.parse_due("Обсудить итоги 05.11", self.BASE, explicit=True) == (None, None)
        assert wt.parse_due("Обсудить итоги 5 ноября", self.BASE, explicit=True) == (None, None)

    def test_с_подсказкой_дата_в_далёком_прошлом_не_срок(self):
        # Подсказка есть — решает только проверка «далёкого прошлого».
        assert wt.parse_due("до 01.02.2015", self.BASE, explicit=True) == (None, None)
        assert wt.parse_due("до 01.02.2015", self.BASE) == (None, None)
