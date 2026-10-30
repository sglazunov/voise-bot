"""Части записи, восстановление, облако — раздел 4 второго раунда
адверсариального ревью 30.10 (docs/АДВЕРСАРИАЛЬНОЕ-РЕВЮ-2026-10-30-2.md).

Каждый тест класса находки падал на коде до правки (`4fc23c0`). Класс
«Мутанты» закрывает выживших мутантов из отчёта: при поломке строки он
краснеет, а весь прежний набор оставался зелёным.

Имена файлов в тестах — без двоеточия: на NTFS «10:00» в имени — это поток
данных, а не имя, и набор должен идти и на машине разработчика (Windows).
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from datetime import date
from pathlib import Path

import pytest

from app import weeek_tasks as wt
from app.automation import notify
from app.automation import scheduler as sched_mod
from app.automation import snapshots
from app.automation.clouds import local as local_cloud
from app.automation.clouds import yandex_disk
from app.automation.scheduler import MeetingState, Scheduler
from app.jobs import Job, store
from tests.test_scheduler_review_3010 import FAIL, KEY, URL, _card, _Env, _ok, _Slot


class _Crash(BaseException):
    """Процесс умер посреди записи (except Exception его не ловит)."""


def _file(tmp_path, name: str, data: bytes = b"v" * 64) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def _job(path: Path, status: str = "queued"):
    """Задача в хранилище МИМО очереди: воркер, поднятый другим тестом, не
    должен менять её статус посреди проверки."""
    job = Job(id=uuid.uuid4().hex[:12], filename=path.name, audio_path=str(path),
              language="ru", diarize=False, owner="team", status=status)
    store._jobs[job.id] = job
    return job


def _record_to_out(env):
    """Рекордер, который пишет ровно в путь, выбранный `_run` (как настоящий)."""
    def record(url, out, cfg, **kw):
        Path(out).write_bytes(b"v" * (100 + len(env.parts)))
        env.parts.append(Path(out))
        return {"ok": True, "path": out, "reason": "stopped", "size": 100}
    return record


@pytest.fixture
def resume(monkeypatch):
    """Восстановление после перезапуска: дозаливка и сирота — заглушки."""
    calls, orphans = [], []

    def fake_late(self, user, st, cfg_, out, first_delay=300):
        calls.append((st, out, first_delay))

    monkeypatch.setattr(Scheduler, "_late_upload", fake_late)
    monkeypatch.setattr(Scheduler, "_await_and_upload_protocol",
                        lambda self, *a, **k: None)
    monkeypatch.setattr(Scheduler, "_finalize_orphan",
                        lambda self, st, cfg, out: orphans.append((st, out)))
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

    return type("R", (), {"calls": calls, "orphans": orphans, "state": state,
                          "wait": staticmethod(wait)})


# --------------------------------------------------------------------------- #
# В: второй повторный заход перезаписывал первую часть В ОБЛАКЕ
# --------------------------------------------------------------------------- #
class TestИменаЧастей:
    def test_каждая_часть_уезжает_под_своим_именем(self, monkeypatch, tmp_path):
        env = _Env(monkeypatch, tmp_path)
        monkeypatch.setattr(sched_mod.recorder, "record_meeting", _record_to_out(env))
        st = _card()
        env.upload_results = [_ok(1), _ok(2), _ok(3)]
        for _ in range(3):
            env.run(st)
        names = [Path(p).name for p, _ in env.uploads]
        assert len(set(names)) == 3, f"части ушли в облако под одним именем: {names}"
        assert "(часть 2)" in names[1] and "(часть 3)" in names[2]
        assert "часть" not in names[0]
        assert [p.get("cloud_url") for p in st.parts] == ["https://disk/p1",
                                                          "https://disk/p2"]

    def test_номер_части_переживает_перезапуск(self, monkeypatch, tmp_path):
        env = _Env(monkeypatch, tmp_path)
        monkeypatch.setattr(sched_mod.recorder, "record_meeting", _record_to_out(env))
        st = _card()
        env.upload_results = [_ok(1), _ok(2)]
        env.run(st)
        env.run(st)
        # Перезапуск: карточку заново собрал опрос из снапшота.
        polled = _card()
        Scheduler()._restore_snapshot(polled, snapshots.load("team"))
        env.upload_results = [_ok(3)]
        env.run(polled)
        assert "(часть 3)" in Path(env.uploads[-1][0]).name

    def test_выгрузка_записи_не_затирает_файл_в_облаке(self, monkeypatch, tmp_path):
        seen = {}
        monkeypatch.setattr(sched_mod.delivery.clouds, "upload",
                            lambda *a, **k: seen.update(k) or {"ok": True, "url": "u",
                                                               "backend": "yandex_disk"})
        sched_mod.delivery.upload_with_retry(str(_file(tmp_path, "a.mp4")),
                                             {"cloud": "yandex_disk"}, lambda m: None,
                                             attempts=1)
        assert seen.get("overwrite") is False


class _FakeDisk:
    """REST Яндекс.Диска на уровне `request`/`request_json` модуля."""

    def __init__(self, files: dict[str, bytes], md5: bool = True):
        self.files = dict(files)
        self.md5 = md5
        self.upload_params: list[dict] = []
        self.puts: list[str] = []

    def request_json(self, method, url, headers=None, params=None, **kw):
        params = params or {}
        if url.endswith("/resources/upload"):
            self.upload_params.append(dict(params))
            path = params["path"]
            if path in self.files and params.get("overwrite") != "true":
                return 409, {"error": "DiskResourceAlreadyExistsError"}
            return 200, {"href": "https://upload/" + path}
        if url.endswith("/resources"):
            path = params["path"]
            if path not in self.files:
                return 404, {}
            data = self.files[path]
            return 200, {"size": len(data),
                         "md5": hashlib.md5(data).hexdigest() if self.md5 else None,
                         "public_url": "https://disk/d/" + path.rsplit("/", 1)[-1]}
        return 404, {}

    def request(self, method, url, headers=None, params=None, data=None, **kw):
        if url.startswith("https://upload/"):
            path = url[len("https://upload/"):]
            self.files[path] = data.read() if hasattr(data, "read") else data
            self.puts.append(path)
            return 201, b""
        if url.endswith("/resources/publish"):
            return 200, b""
        if url.endswith("/resources"):
            return 409, b""          # папка уже есть
        return 404, b""


def _disk(monkeypatch, files, md5=True):
    fake = _FakeDisk(files, md5)
    monkeypatch.setattr(yandex_disk, "request_json", fake.request_json)
    monkeypatch.setattr(yandex_disk, "request", fake.request)
    return fake


CFG_DISK = {"token": "t", "folder": "disk:/Записи"}


class TestОблакоНеЗатирает:
    def test_яндекс_другой_файл_с_тем_же_именем_не_затирается(self, monkeypatch, tmp_path):
        disk = _disk(monkeypatch, {"disk:/Записи/a.mp4": b"part-one"})
        res = yandex_disk.upload(str(_file(tmp_path, "a.mp4", b"part-two!!")), "a.mp4",
                                 CFG_DISK, overwrite=False)
        assert res["ok"]
        assert disk.files["disk:/Записи/a.mp4"] == b"part-one", "первая часть затёрта"
        assert res["path"] != "disk:/Записи/a.mp4"
        assert disk.files[res["path"]] == b"part-two!!"
        assert all(p.get("overwrite") != "true" for p in disk.upload_params)

    def test_яндекс_тот_же_размер_другое_содержимое(self, monkeypatch, tmp_path):
        disk = _disk(monkeypatch, {"disk:/Записи/a.mp4": b"part-one"})
        res = yandex_disk.upload(str(_file(tmp_path, "a.mp4", b"part-two")), "a.mp4",
                                 CFG_DISK, overwrite=False)
        assert disk.files["disk:/Записи/a.mp4"] == b"part-one"
        assert disk.files[res["path"]] == b"part-two"

    def test_яндекс_без_md5_решает_размер(self, monkeypatch, tmp_path):
        disk = _disk(monkeypatch, {"disk:/Записи/a.mp4": b"part-one"}, md5=False)
        res = yandex_disk.upload(str(_file(tmp_path, "a.mp4", b"part-two!!")), "a.mp4",
                                 CFG_DISK, overwrite=False)
        assert disk.files["disk:/Записи/a.mp4"] == b"part-one"
        assert disk.files[res["path"]] == b"part-two!!"

    def test_яндекс_тот_же_файл_не_заливается_второй_раз(self, monkeypatch, tmp_path):
        disk = _disk(monkeypatch, {"disk:/Записи/a.mp4": b"same"})
        res = yandex_disk.upload(str(_file(tmp_path, "a.mp4", b"same")), "a.mp4",
                                 CFG_DISK, overwrite=False)
        assert res["ok"] and res["path"] == "disk:/Записи/a.mp4"
        assert res["url"] and disk.puts == []

    def test_яндекс_по_умолчанию_как_раньше(self, monkeypatch, tmp_path):
        """Протокол при «Пересобрать» заменяет прежний — там overwrite нужен."""
        disk = _disk(monkeypatch, {"disk:/Записи/a.docx": b"old"})
        res = yandex_disk.upload(str(_file(tmp_path, "a.docx", b"new")), "a.docx", CFG_DISK)
        assert res["ok"] and res["path"] == "disk:/Записи/a.docx"
        assert disk.upload_params[-1]["overwrite"] == "true"

    def test_локально_другой_файл_не_затирается(self, tmp_path):
        out = tmp_path / "cloud"
        out.mkdir()
        (out / "a.mp4").write_bytes(b"part-one")
        cfg = {"local_dir": str(out)}
        res = local_cloud.upload(str(_file(tmp_path, "a.mp4", b"part-two")), "a.mp4",
                                 cfg, overwrite=False)
        assert res["ok"] and (out / "a.mp4").read_bytes() == b"part-one"
        assert Path(res["path"]).read_bytes() == b"part-two"

    def test_локально_тот_же_файл_не_копируется(self, tmp_path):
        out = tmp_path / "cloud"
        out.mkdir()
        (out / "a.mp4").write_bytes(b"same")
        os.utime(out / "a.mp4", (1_000_000_000, 1_000_000_000))
        res = local_cloud.upload(str(_file(tmp_path, "a.mp4", b"same")), "a.mp4",
                                 {"local_dir": str(out)}, overwrite=False)
        assert res["ok"] and res["path"] == str(out / "a.mp4")
        assert sorted(p.name for p in out.iterdir()) == ["a.mp4"]
        assert (out / "a.mp4").stat().st_mtime == 1_000_000_000, "файл скопирован заново"

    def test_через_clouds_upload(self, monkeypatch, tmp_path):
        out = tmp_path / "cloud"
        out.mkdir()
        (out / "a.mp4").write_bytes(b"part-one")
        from app.automation import clouds
        res = clouds.upload(str(_file(tmp_path, "a.mp4", b"part-two")), "a.mp4",
                            {"cloud": "local", "local_dir": str(out)}, overwrite=False)
        assert (out / "a.mp4").read_bytes() == b"part-one" and res["ok"]


# --------------------------------------------------------------------------- #
# В: падение во время второй части теряло её, если у первой была задача
# --------------------------------------------------------------------------- #
class TestПадениеВоВремяЧасти:
    def _crash_second(self, monkeypatch, env, st):
        def crash(url, out, cfg, **kw):
            Path(out).write_bytes(b"v" * 100)
            raise _Crash()
        monkeypatch.setattr(sched_mod.recorder, "record_meeting", crash)
        with pytest.raises(_Crash):
            env.run(st)

    def test_вторая_часть_восстанавливается_как_сирота(self, monkeypatch, tmp_path, resume):
        env = _Env(monkeypatch, tmp_path)
        p1 = _file(tmp_path, "p1.mp4")
        j1 = _job(p1)
        st = _card(state="transcribing", out_path=str(p1), job_id=j1.id,
                   cloud_url="https://disk/p1")
        snapshots.save(st)
        self._crash_second(monkeypatch, env, st)
        snap = snapshots.load("team")[KEY]
        assert snap["job_id"] is None, "у второй части в снапшоте задача первой"
        assert [p.get("job_id") for p in snap["parts"]] == [j1.id]
        Scheduler()._resume_pending()
        assert [o for _, o in resume.orphans] == [snap["out_path"]], \
            "вторая часть после перезапуска не обработана"

    def test_старый_снапшот_с_задачей_прежней_части(self, tmp_path, resume):
        """Снапшот, записанный прежним кодом: job_id первой части у второй."""
        p1 = _file(tmp_path, "p1.mp4")
        p2 = _file(tmp_path, "p2.mp4")
        j1 = _job(p1)
        snapshots.save(_card(state="recording", out_path=str(p2), job_id=j1.id))
        Scheduler()._resume_pending()
        assert [o for _, o in resume.orphans] == [str(p2)]
        st = resume.orphans[0][0]
        assert st.job_id is None and [p.get("job_id") for p in st.parts] == [j1.id]

    def test_старый_снапшот_и_карточка_опроса(self, tmp_path, resume):
        """Карточку раньше собрал опрос из того же старого снапшота: задача
        прежней части уходит с неё и в её `parts`."""
        p1 = _file(tmp_path, "p1.mp4")
        p2 = _file(tmp_path, "p2.mp4")
        j1 = _job(p1)
        snapshots.save(_card(state="recording", out_path=str(p2), job_id=j1.id))
        s = Scheduler()
        polled = _card(url=URL)
        s._restore_snapshot(polled, snapshots.load("team"))
        s._states[KEY] = polled
        s._resume_pending()
        assert resume.orphans and resume.orphans[0][0] is polled
        assert polled.job_id is None
        assert [p.get("job_id") for p in polled.parts] == [j1.id]

    def test_прерванная_задача_прежней_части_перезапускается(self, tmp_path, resume,
                                                             monkeypatch):
        p1 = _file(tmp_path, "p1.mp4")
        p2 = _file(tmp_path, "p2.mp4")
        j1 = _job(p1, status="error")
        j1.error = "Прервано (сервис был перезапущен)"
        retried = []
        monkeypatch.setattr(sched_mod.store, "retry", lambda jid: retried.append(jid))
        snapshots.save(_card(state="recording", out_path=str(p2),
                             parts=[{"out_path": str(p1), "job_id": j1.id}]))
        Scheduler()._resume_pending()
        assert retried == [j1.id]


# --------------------------------------------------------------------------- #
# В: повторный заход, пока предыдущая часть выгружается — два `_run`
# --------------------------------------------------------------------------- #
class TestОдинRunНаКарточку:
    def test_повторный_заход_во_время_выгрузки_отклоняется(self, monkeypatch, tmp_path):
        env = _Env(monkeypatch, tmp_path)
        gate, entered = threading.Event(), threading.Event()

        def upload(path, name, settings, **kw):
            entered.set()
            gate.wait(5)
            return _ok(1)
        monkeypatch.setattr(sched_mod.delivery.clouds, "upload", upload)
        monkeypatch.setattr(sched_mod.recorder, "acquire_slot", lambda: _Slot())
        monkeypatch.setattr(sched_mod.security, "team_of", lambda u: u)
        launched = []
        monkeypatch.setattr(Scheduler, "_run",
                            lambda self, st, slot, manual=False: launched.append(st))
        st = _card()
        env.sched._states[KEY] = st
        t = threading.Thread(target=_REAL_RUN, args=(env.sched, st, _Slot(), True))
        t.start()
        try:
            assert entered.wait(5)
            assert st.state == "uploading"
            res = env.sched.run_now("team", "42")
        finally:
            gate.set()
            t.join(5)
        assert not res["ok"], "второй _run на той же карточке запущен"
        assert launched == []
        assert "выгруж" in res["error"]
        # Первая часть доехала — теперь заход разрешён.
        assert env.sched.run_now("team", "42")["ok"]
        assert launched == [st]


_REAL_RUN = Scheduler._run


# --------------------------------------------------------------------------- #
# С: дозаливка, пересёкшаяся с повторным заходом, удаляла исходник части 1
# --------------------------------------------------------------------------- #
class TestДозаливкаИПовторныйЗаход:
    def test_исходник_с_задачей_в_очереди_не_удаляется(self, monkeypatch, tmp_path):
        env = _Env(monkeypatch, tmp_path)
        p1 = _file(tmp_path, "p1.mp4")
        j1 = _job(p1)
        st = _card(state="transcribing", out_path=str(p1), job_id=j1.id,
                   upload_error="облако недоступно")
        orig = env._upload

        def upload(path, name, settings, **kw):
            if path == str(p1):
                env.upload_results = [FAIL]
                env.run(st)                  # повторный заход посреди дозаливки
                env.uploads.append((path, kw.get("owner")))
                return _ok(1)
            return orig(path, name, settings, **kw)
        monkeypatch.setattr(sched_mod.delivery.clouds, "upload", upload)
        env.late_upload(st, str(p1))
        assert p1.exists(), "исходник части 1 удалён, пока её задача в очереди"
        assert j1.delete_audio_when_done
        part = next(p for p in st.parts if p["out_path"] == str(p1))
        assert part["cloud_url"] == "https://disk/p1" and not part.get("upload_error")
        assert env.fields == [], "поле «Видео» получило ссылку прежней части"


# --------------------------------------------------------------------------- #
# С: во время записи части 2 карточка показывала «done» по задаче части 1
# --------------------------------------------------------------------------- #
class TestКарточкаВоВремяЧасти:
    def test_идёт_запись_а_не_готово(self, monkeypatch, tmp_path):
        env = _Env(monkeypatch, tmp_path)
        monkeypatch.setattr(sched_mod.security, "team_of", lambda u: u)
        monkeypatch.setattr(sched_mod.recorder, "active_recordings", lambda: 1)
        p1 = _file(tmp_path, "p1.mp4")
        j1 = _job(p1, status="done")
        st = _card(state="done", out_path=str(p1), job_id=j1.id,
                   cloud_url="https://disk/p1")
        env.sched._states[KEY] = st
        seen = {}

        def record(url, out, cfg, **kw):
            seen.update(env.sched.status("team")["meetings"][0])
            return env._record(url, out, cfg, **kw)
        monkeypatch.setattr(sched_mod.recorder, "record_meeting", record)
        env.upload_results = [_ok(2)]
        env.run(st)
        assert seen["state"] == "recording", "во время части 2 карточка «done» — нет «Стоп»"

    def test_запись_важнее_статуса_задачи(self, tmp_path):
        j = _job(_file(tmp_path, "p.mp4"), status="done")
        m = {"state": "recording", "detail": "Идёт запись", "job_id": j.id}
        Scheduler()._enrich_from_job(m)
        assert m["state"] == "recording" and m["detail"] == "Идёт запись"

    def test_ожидатель_прежней_части_не_закрывает_идущую(self, monkeypatch, tmp_path):
        monkeypatch.setattr(notify, "send", lambda *a, **k: True)
        j1 = _job(_file(tmp_path, "p1.mp4"), status="done")
        j1.docx_providers = ["gemini"]
        st = _card(state="recording", detail="Идёт запись", job_id=None)
        Scheduler()._await_and_upload_protocol(st, j1.id, {})
        assert st.state == "recording"

    def test_ожидатель_своей_части_закрывает(self, monkeypatch, tmp_path):
        monkeypatch.setattr(notify, "send", lambda *a, **k: True)
        j1 = _job(_file(tmp_path, "p1.mp4"), status="done")
        j1.docx_providers = ["gemini"]
        st = _card(state="transcribing", job_id=j1.id)
        Scheduler()._await_and_upload_protocol(st, j1.id, {})
        assert st.state == "done"


# --------------------------------------------------------------------------- #
# Н: рестарт между удачной дозаливкой и записью снапшота — повторная заливка
# --------------------------------------------------------------------------- #
class TestСнапшотДоWeeek:
    def test_ссылка_сохранена_до_записи_в_weeek(self, monkeypatch, tmp_path):
        env = _Env(monkeypatch, tmp_path, cfg={"post_back_to_weeek": True})
        p = _file(tmp_path, "p.mp4")
        st = _card(out_path=str(p), upload_error="облако недоступно")
        snapshots.save(st)
        seen = []
        monkeypatch.setattr(sched_mod.delivery, "write_weeek_field",
                            lambda *a, **k: seen.append(dict(snapshots.load("team")[KEY])))
        env.upload_results = [_ok(1)]
        env.late_upload(st, str(p))
        assert seen, "поле Weeek не писалось"
        assert seen[0]["cloud_url"] == "https://disk/p1" and not seen[0]["upload_error"]

    def test_прежняя_часть_сохранена_до_комментария(self, monkeypatch, tmp_path):
        env = _Env(monkeypatch, tmp_path, cfg={"post_back_to_weeek": True})
        p1, p2 = _file(tmp_path, "p1.mp4"), _file(tmp_path, "p2.mp4")
        st = _card(out_path=str(p2), parts=[{"out_path": str(p1),
                                              "upload_error": "облако недоступно"}])
        snapshots.save(st)
        seen = []
        monkeypatch.setattr(sched_mod.weeek, "add_comment",
                            lambda *a: seen.append(snapshots.load("team")[KEY]["parts"]))
        env.upload_results = [_ok(1)]
        env.late_upload(st, str(p1))
        assert seen and seen[0][0]["cloud_url"] == "https://disk/p1"
        assert not seen[0][0].get("upload_error")


# --------------------------------------------------------------------------- #
# Н: сроки после «релиз»/«сборка»
# --------------------------------------------------------------------------- #
class TestСрокиРелиз:
    BASE = date(2026, 10, 30)

    @pytest.mark.parametrize("text", ["к релизу 20.11", "срок сборки 20.11",
                                      "до релиза 20.11", "к сборке 20.11"])
    def test_дата_после_релиза_срок(self, text):
        assert wt.parse_due(text, self.BASE)[0] == "2026-11-20"

    @pytest.mark.parametrize("text", ["релиз 1.2", "релиз 10.12", "сборка 12.3",
                                      "выпустили релиз 11.10 вчера", "версия 10.12"])
    def test_номер_релиза_не_срок(self, text):
        assert wt.parse_due(text, self.BASE)[0] is None


# --------------------------------------------------------------------------- #
# Н: без токена Weeek запись, прерванная посреди встречи, не обрабатывалась
# --------------------------------------------------------------------------- #
class TestСиротаБезТокена:
    def test_запись_прерванная_рестартом_обрабатывается(self, tmp_path, resume):
        resume.state["cfg"] = {"do_protocol": True}
        p = _file(tmp_path, "p.mp4")
        snapshots.save(_card(state="recording", out_path=str(p)))
        Scheduler()._resume_pending()
        assert [o for _, o in resume.orphans] == [str(p)]

    @pytest.mark.parametrize("token,task,written", [
        ("tok", "42", True), ("", "42", False), ("tok", "link-20261030-1", False)])
    def test_поле_видео_только_с_токеном_и_не_по_ссылке(self, monkeypatch, tmp_path,
                                                         token, task, written):
        env = _Env(monkeypatch, tmp_path, cfg={"weeek_token": token})
        p = _file(tmp_path, "p.mp4")
        env.upload_results = [_ok(1)]
        env.sched._finalize_orphan(_card(task_id=task), dict(env.cfg), str(p))
        assert bool(env.fields) is written


# --------------------------------------------------------------------------- #
# Н: скрипт раскладки архива — команда, а не логин участника
# --------------------------------------------------------------------------- #
class TestАрхивКоманды:
    def test_main_с_логином_участника(self, monkeypatch, tmp_path, capsys):
        from app import config, meeting_series
        from scripts import sort_cloud_archive as sca
        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        monkeypatch.setattr(sched_mod.security, "team_of",
                            lambda u: "alpha" if u in ("bob", "alpha") else u)
        cfg = {"cloud": "local", "folders_by_series": True,
               "oneoff_folder": "Разовые встречи"}
        monkeypatch.setattr(sched_mod.auto_settings, "load", lambda u: dict(cfg))
        mine = tmp_path / "recordings" / "alpha"
        mine.mkdir(parents=True)
        for name in ("20.10.2026, 10-00. - Встреча с ОО.mp4",
                     "27.10.2026, 10-00. - Встреча с ОО.mp4"):
            (mine / name).write_bytes(b"v")
        assert sca.main(["--user", "bob", "--apply"]) == 0
        out = capsys.readouterr().out
        assert "Файлов встреч: 2" in out, out
        assert sorted(p.name for p in (mine / "Встреча с ОО").iterdir()) == [
            "20.10.2026, 10-00. - Встреча с ОО.mp4",
            "27.10.2026, 10-00. - Встреча с ОО.mp4"]
        assert meeting_series.find("alpha", "Встреча с ОО")[1] is not None


# --------------------------------------------------------------------------- #
# Н: откат удаления не перезапускал уже вышедшую дозаливку
# --------------------------------------------------------------------------- #
class TestОткатУдаления:
    def test_дозаливка_возобновляется(self, tmp_path, resume):
        p = _file(tmp_path, "p.mp4")
        s = Scheduler()
        st = _card(state="done", out_path=str(p), upload_error="облако недоступно")
        s._states[KEY] = st
        s.forget_team("team")
        s.forget_done("team", restore=True)
        assert [c[1] for c in resume.wait()] == [str(p)]

    def test_без_отката_не_возобновляется(self, tmp_path, resume):
        p = _file(tmp_path, "p.mp4")
        s = Scheduler()
        s._states[KEY] = _card(state="done", out_path=str(p), upload_error="x")
        s.forget_team("team")
        s.forget_done("team")
        assert resume.wait(timeout=0.3) == []


# --------------------------------------------------------------------------- #
# Выжившие мутанты (раздел 4 отчёта)
# --------------------------------------------------------------------------- #
class TestМутантыДозаливкиЧастей:
    def test_путь_в_облаке_берётся_у_части(self, monkeypatch, tmp_path):
        env = _Env(monkeypatch, tmp_path)
        p1, p2 = _file(tmp_path, "p1.mp4"), _file(tmp_path, "p2.mp4")
        st = _card(out_path=str(p2), cloud_path="disk:/Записи/p2.mp4",
                   parts=[{"out_path": str(p1), "cloud_path": "disk:/Записи/p1.mp4",
                           "upload_error": "нет ссылки"}])
        env.late_upload(st, str(p1))
        assert env.published == ["disk:/Записи/p1.mp4"]

    def test_неудача_обновляет_часть(self, monkeypatch, tmp_path):
        env = _Env(monkeypatch, tmp_path)
        p1, p2 = _file(tmp_path, "p1.mp4"), _file(tmp_path, "p2.mp4")
        st = _card(out_path=str(p2), parts=[{"out_path": str(p1), "upload_error": "старая"}])
        env.upload_results = [{"ok": False, "uploaded": True, "backend": "yandex_disk",
                               "path": "disk:/Записи/p1.mp4", "error": "нет ссылки"}]
        env.late_upload(st, str(p1))
        part = st.parts[0]
        assert part["cloud_path"] == "disk:/Записи/p1.mp4"
        assert part["upload_error"] != "старая" and part["upload_error"]
        assert snapshots.load("team")[KEY]["parts"][0]["cloud_path"] == "disk:/Записи/p1.mp4"
        assert st.cloud_path is None and st.upload_error is None

    def test_задача_берётся_у_части(self, monkeypatch, tmp_path):
        env = _Env(monkeypatch, tmp_path)
        p1, p2 = _file(tmp_path, "p1.mp4"), _file(tmp_path, "p2.mp4")
        j1, j2 = _job(p1), _job(p2, status="done")
        st = _card(out_path=str(p2), job_id=j2.id, cloud_url="https://disk/p2",
                   parts=[{"out_path": str(p1), "job_id": j1.id, "upload_error": "x"}])
        env.upload_results = [_ok(1)]
        env.late_upload(st, str(p1))
        assert p1.exists() and j1.delete_audio_when_done

    def test_задача_ищется_по_файлу_если_связь_потеряна(self, monkeypatch, tmp_path):
        env = _Env(monkeypatch, tmp_path)
        p = _file(tmp_path, "p.mp4")
        j = _job(p)
        st = _card(out_path=str(p), upload_error="x")       # job_id на карточке нет
        env.upload_results = [_ok(1)]
        env.late_upload(st, str(p))
        assert p.exists() and j.delete_audio_when_done

    def test_удаление_аккаунта_во_время_выгрузки(self, monkeypatch, tmp_path):
        env = _Env(monkeypatch, tmp_path, cfg={"post_back_to_weeek": True})
        p = _file(tmp_path, "p.mp4")
        st = _card(out_path=str(p), upload_error="x")
        snapshots.save(st)

        def upload(*a, **k):
            st.forgotten = True
            return _ok(1)
        monkeypatch.setattr(sched_mod.delivery.clouds, "upload", upload)
        env.late_upload(st, str(p))
        assert env.fields == [] and env.comments == []
        assert snapshots.load("team")[KEY]["upload_error"] == "x"


class TestМутантыЧастейВRun:
    def test_прежняя_часть_уносит_задачу_и_путь(self, monkeypatch, tmp_path):
        env = _Env(monkeypatch, tmp_path)
        p1 = _file(tmp_path, "p1.mp4")
        st = _card(out_path=str(p1), job_id="j1", cloud_path="disk:/Записи/p1.mp4",
                   cloud_url="https://disk/p1")
        env.upload_results = [FAIL]
        env.run(st)
        part = st.parts[0]
        assert part["out_path"] == str(p1) and part["job_id"] == "j1"
        assert part["cloud_path"] == "disk:/Записи/p1.mp4"
        assert part["cloud_url"] == "https://disk/p1"
        assert st.job_id is None


class TestМутантыВосстановления:
    def test_части_в_ветке_задачи(self, tmp_path, resume):
        p0 = _file(tmp_path, "p0.mp4")
        p1 = _file(tmp_path, "p1.mp4")
        j1 = _job(p1, status="running")
        snapshots.save(_card(state="transcribing", out_path=str(p1), job_id=j1.id,
                             cloud_url="https://disk/p1",
                             parts=[{"out_path": str(p0), "upload_error": "x"}]))
        Scheduler()._resume_pending()
        assert [c[1] for c in resume.wait()] == [str(p0)]

    def test_части_в_ветке_сироты(self, tmp_path, resume):
        p0 = _file(tmp_path, "p0.mp4")
        p1 = _file(tmp_path, "p1.mp4")
        snapshots.save(_card(state="recording", out_path=str(p1),
                             parts=[{"out_path": str(p0), "upload_error": "x"}]))
        Scheduler()._resume_pending()
        assert [c[1] for c in resume.wait()] == [str(p0)]
        assert [o for _, o in resume.orphans] == [str(p1)]

    def test_уехавшая_часть_не_дозаливается(self, tmp_path, resume):
        p0 = _file(tmp_path, "p0.mp4")
        p1 = _file(tmp_path, "p1.mp4")
        # Ошибка выгрузки осталась от прежней попытки, но ссылка уже есть.
        snapshots.save(_card(state="done", out_path=str(p1), cloud_url="https://disk/p1",
                             parts=[{"out_path": str(p0), "cloud_url": "https://disk/p0",
                                     "upload_error": "облако недоступно"}]))
        Scheduler()._resume_pending()
        assert resume.wait(timeout=0.3) == []

    def test_видео_дозаливается_когда_задачу_удалил_ретеншн(self, tmp_path, resume):
        p = _file(tmp_path, "p.mp4")
        snapshots.save(_card(state="transcribing", out_path=str(p), job_id="purged",
                             upload_error="облако недоступно"))
        Scheduler()._resume_pending()
        assert [c[1] for c in resume.wait()] == [str(p)]

    def test_части_восстанавливает_сам_restore_snapshot(self, tmp_path):
        parts = [{"out_path": str(tmp_path / "p0.mp4"), "job_id": "j0",
                  "upload_error": "x"}]
        snapshots.save(_card(state="done", out_path=str(tmp_path / "p1.mp4"), parts=parts))
        st = _card()
        Scheduler()._restore_snapshot(st, snapshots.load("team"))
        assert st.parts == parts
