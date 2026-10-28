"""Раскладка по встречам в облаке (docs/ТЗ-ПАПКИ-ВСТРЕЧ.md).

Запрос владельца 28.10: «все протоколы в одной папке» — нужно, чтобы у
встречи с ОО была своя папка, у «Виртуального помощника» своя, и в каждой —
видео и протокол каждой встречи с датой; разовые встречи — в отдельную общую.
"""
from __future__ import annotations

import uuid

import pytest

from app import cloud_folders, meeting_series
from app.automation import clouds
from app.automation.clouds import yandex_disk


def _user():
    return "u" + uuid.uuid4().hex[:8]


CFG = {"cloud": "yandex_disk", "folders_by_series": True,
       "oneoff_folder": "Разовые встречи",
       "yandex_disk": {"token": "t", "folder": "disk:/Телемост-записи"}}


class TestИмяПапки:
    def test_служебные_знаки_и_длина(self):
        assert cloud_folders.safe_name("ОД / сайт: редизайн?") == "ОД сайт редизайн"
        assert cloud_folders.safe_name("  ...Встреча...  ") == "Встреча"
        assert len(cloud_folders.safe_name("я" * 300)) == 80
        assert cloud_folders.safe_name("") == ""


class TestКудаПоложить:
    def test_раскладка_выключена_плоско(self):
        p = cloud_folders.resolve(_user(), "CRM", dict(CFG, folders_by_series=False))
        assert p["kind"] == "flat" and p["sub"] == ""

    def test_новая_встреча_в_разовые(self):
        p = cloud_folders.resolve(_user(), "20.10.2026, 11:30. - Созвон с юристом.mp4", CFG)
        assert p == {"sub": "Разовые встречи", "kind": "oneoff",
                     "key": "созвон с юристом", "date": "20.10.2026"}

    def test_карточка_с_контекстом_это_серия_с_первой_встречи(self):
        u = _user()
        meeting_series.upsert(u, "Встреча с ОО", {"title": "Встреча с ОО",
                                                   "context": "Проект: ОО"})
        p = cloud_folders.resolve(u, "28.10.2026, 10:00. - Встреча с ОО.mp4", CFG)
        assert p["kind"] == "series" and p["sub"] == "Встреча с ОО"

    def test_вторая_встреча_в_другой_день_делает_серию(self):
        u = _user()
        t1 = "20.10.2026, 11:00. - Виртуальный помощник.mp4"
        assert cloud_folders.resolve(u, t1, CFG)["kind"] == "oneoff"
        meeting_series.remember(u, t1, {"summary": "x"}, job_id="j1", date="20.10.2026")
        # тот же день — та же встреча (видео, потом протокол), всё ещё разовая
        assert cloud_folders.resolve(u, t1, CFG)["kind"] == "oneoff"
        p = cloud_folders.resolve(u, "27.10.2026, 11:00. - Виртуальный помощник.mp4", CFG)
        assert p["kind"] == "series" and p["sub"] == "Виртуальный помощник"

    def test_ручные_режимы_карточки(self):
        u = _user()
        meeting_series.upsert(u, "Планёрка", {"context": "есть", "folder_mode": "oneoff"})
        assert cloud_folders.resolve(u, "Планёрка", CFG)["kind"] == "oneoff"
        meeting_series.upsert(u, "Планёрка", {"folder_mode": "series", "folder": "Планёрки/2026"})
        p = cloud_folders.resolve(u, "Планёрка", CFG)
        assert p["kind"] == "series" and p["sub"] == "Планёрки 2026"

    def test_своя_папка_сразу_без_контекста(self):
        u = _user()
        meeting_series.upsert(u, "Демо", {"folder_mode": "series"})
        assert cloud_folders.resolve(u, "Демо", CFG)["sub"] == "Демо"


class TestПереездИзРазовых:
    def test_файл_разовой_встречи_запоминается(self):
        u = _user()
        place = cloud_folders.resolve(u, "20.10.2026, 11:00. - ОО.mp4", CFG)
        cloud_folders.placed(u, "20.10.2026, 11:00. - ОО.mp4", place,
                             {"ok": True, "backend": "yandex_disk",
                              "path": "disk:/Телемост-записи/Разовые встречи/a.mp4"}, "recording")
        _k, e = meeting_series.find(u, "ОО")
        assert [f["path"] for f in e["oneoff_files"]] == [
            "disk:/Телемост-записи/Разовые встречи/a.mp4"]
        # серийный файл не запоминается
        cloud_folders.placed(u, "ОО", {"kind": "series"}, {"ok": True, "path": "x"}, "protocol")
        assert len(meeting_series.find(u, "ОО")[1]["oneoff_files"]) == 1

    def test_вторая_встреча_перевозит_файлы_первой(self, monkeypatch):
        u = _user()
        t1, t2 = "20.10.2026, 11:00. - ОО.mp4", "27.10.2026, 11:00. - ОО.mp4"
        place1 = cloud_folders.resolve(u, t1, CFG)
        for name in ("a.mp4", "a - протокол.docx", "b.mp4"):
            cloud_folders.placed(u, t1, place1, {"ok": True, "backend": "yandex_disk",
                                 "path": f"disk:/Телемост-записи/Разовые встречи/{name}"}, "x")
        calls = []

        def fake_move(item, sub, cfg):
            calls.append((item["path"], sub))
            if item["path"].endswith("b.mp4"):
                return {"ok": False, "retry": True, "error": "сеть"}
            return {"ok": True}

        monkeypatch.setattr(clouds, "move_to_sibling", fake_move)
        place2 = cloud_folders.resolve(u, t2, CFG)
        assert place2["kind"] == "series"
        logged = []
        assert cloud_folders.adopt(u, t2, place2, CFG, logged.append) == 2
        assert {s for _p, s in calls} == {"ОО"}
        left = meeting_series.find(u, "ОО")[1]["oneoff_files"]
        assert [f["path"].rsplit("/", 1)[1] for f in left] == ["b.mp4"]   # повторит позже
        assert "перенесено" in logged[0]


class TestВыгрузкаВПодпапку:
    def test_яндекс_кладёт_в_подпапку_папки_записей(self, monkeypatch):
        got = {}
        monkeypatch.setattr(yandex_disk, "upload",
                            lambda f, n, cfg: got.update(cfg) or {"ok": True})
        clouds.upload("/x.mp4", "x.mp4", CFG, subfolder="Встреча с ОО")
        assert got["folder"] == "disk:/Телемост-записи/Встреча с ОО"

    def test_локально_файл_ложится_в_подпапку(self, tmp_path):
        src = tmp_path / "rec.mp4"
        src.write_bytes(b"v")
        cfg = {"cloud": "local", "local_dir": str(tmp_path / "out")}
        res = clouds.upload(str(src), "rec.mp4", cfg, subfolder="ОО")
        assert res["ok"] and (tmp_path / "out" / "ОО" / "rec.mp4").exists()

    def test_локальный_переезд_в_соседнюю_папку(self, tmp_path):
        f = tmp_path / "Разовые встречи" / "a.mp4"
        f.parent.mkdir()
        f.write_bytes(b"v")
        res = clouds.move_to_sibling({"backend": "local", "path": str(f)}, "ОО",
                                     {"cloud": "local"})
        assert res["ok"] and (tmp_path / "ОО" / "a.mp4").exists() and not f.exists()

    def test_яндекс_путь_соседней_папки(self, monkeypatch):
        got = []
        monkeypatch.setattr(yandex_disk, "move", lambda s, d, c: got.append((s, d)) or {"ok": True})
        clouds.move_to_sibling({"backend": "yandex_disk",
                                "path": "disk:/Записи/Разовые встречи/a.mp4"}, "ОО", CFG)
        assert got == [("disk:/Записи/Разовые встречи/a.mp4", "disk:/Записи/ОО/a.mp4")]


class _Disk:
    """Заглушка Диска для move. Сценарий: ссылка после переезда, статус чтения
    ссылки после переезда, ответ на перемещение, ответ на откат."""

    def __init__(self, after_url, after_status=200, move_resp=(201, None),
                 back_resp=(201, None), src_status=200, dst_exists=False):
        self.after_url, self.after_status = after_url, after_status
        self.move_resp, self.back_resp = move_resp, back_resp
        self.src_status, self.dst_exists = src_status, dst_exists
        self.moves = []

    def request(self, method, url, **kw):
        return 201, b""

    def request_json(self, method, url, headers=None, params=None, **kw):
        if url.endswith("/resources/move"):
            self.moves.append((params["from"], params["path"]))
            return self.move_resp if len(self.moves) == 1 else self.back_resp
        if url.endswith("/resources"):
            if params["path"].endswith("src"):
                if self.src_status != 200:
                    return self.src_status, {}
                return 200, {"public_url": "https://disk.yandex.ru/d/old", "public_key": "K"}
            if not self.moves and not self.dst_exists:
                return 404, {}
            if self.after_status != 200:
                return self.after_status, {}
            return 200, {"public_url": self.after_url,
                         "public_key": "K" if self.after_url and "old" in self.after_url else "K2"}
        return 200, {}


@pytest.fixture
def disk(monkeypatch):
    def make(*a, **k):
        d = _Disk(*a, **k)
        monkeypatch.setattr(yandex_disk, "request", d.request)
        monkeypatch.setattr(yandex_disk, "request_json", d.request_json)
        monkeypatch.setattr(yandex_disk.time, "sleep", lambda *_: None)
        return d
    return make


SRC, DST = "disk:/A/Разовые/src", "disk:/A/ОО/dst"


@pytest.mark.parametrize("after,ok", [("https://disk.yandex.ru/d/old", True),
                                      ("https://disk.yandex.ru/d/NEW", False),
                                      (None, False)])
def test_яндекс_возвращает_файл_если_ссылка_сменилась(disk, after, ok):
    d = disk(after)
    res = yandex_disk.move(SRC, DST, {"token": "t"})
    assert res["ok"] is ok
    if ok:
        assert d.moves == [(SRC, DST)]
    else:
        assert d.moves[-1] == (DST, SRC), "файл вернули"
        assert res["retry"] is False


def test_сбой_чтения_ссылки_после_переезда_не_откатывает(disk):
    """Переезд прошёл, а Диск на чтение ответил 500 — это не «ссылка
    сменилась»: раньше файл возвращали назад и больше не пробовали."""
    d = disk("https://disk.yandex.ru/d/old", after_status=500)
    res = yandex_disk.move(SRC, DST, {"token": "t"})
    assert res["ok"] and res["path"] == DST and res["url"] == "https://disk.yandex.ru/d/old"
    assert d.moves == [(SRC, DST)]


def test_откат_не_удался_новый_путь_не_теряется(disk):
    disk("https://disk.yandex.ru/d/NEW", back_resp=(423, {"error": "DiskResourceLockedError"}))
    res = yandex_disk.move(SRC, DST, {"token": "t"})
    assert res["ok"] and res["path"] == DST and "вручную" in res["warning"]


@pytest.mark.parametrize("code,retry", [("DiskResourceAlreadyExistsError", False),
                                        ("DiskPathDoesntExistsError", True)])
def test_разные_409_при_переезде(disk, code, retry):
    disk("x", move_resp=(409, {"error": code}))
    res = yandex_disk.move(SRC, DST, {"token": "t"})
    assert res["ok"] is False and res["retry"] is retry


def test_нет_прав_на_чтение_не_повторять(disk):
    disk("x", src_status=403)
    res = yandex_disk.move(SRC, DST, {"token": "t"})
    assert res == {"ok": False, "retry": False, "error": yandex_disk._READ_DENIED}


def test_исходника_нет_но_он_уже_в_папке_серии(disk):
    """Прошлый асинхронный переезд закончился позже 30 с ожидания."""
    disk("https://disk.yandex.ru/d/old", src_status=404, dst_exists=True)
    res = yandex_disk.move(SRC, DST, {"token": "t"})
    assert res["ok"] and res["path"] == DST


def test_google_не_создаёт_папку_если_список_не_прочитан(monkeypatch):
    from app.automation.clouds import gdrive
    calls = []
    monkeypatch.setattr(gdrive, "_access_token", lambda cfg: "tok")
    monkeypatch.setattr(gdrive, "request_json",
                        lambda m, url, **kw: calls.append(m) or (500, {}))
    with pytest.raises(gdrive.CloudError):
        gdrive.child_folder({}, "P", "ОО")
    assert calls == ["GET"], "POST на создание не уходил"


def test_протокол_ложится_в_папку_встречи(monkeypatch, tmp_path):
    """Протокол — рядом с видео той же встречи, а не в «Телемост-протоколы»."""
    from app.automation import settings as auto_settings
    from app.jobs import Job, store
    u = _user()
    meeting_series.upsert(u, "Встреча с ОО", {"context": "Проект ОО"})
    monkeypatch.setattr(auto_settings, "load", lambda _u: dict(
        CFG, protocol_folder="disk:/Телемост-протоколы"))
    got = {}

    def fake_upload(path, name, cfg, backend=None, folder=None, subfolder=None):
        got.update(name=name, folder=folder, subfolder=subfolder)
        return {"ok": True, "url": "https://disk.yandex.ru/d/p", "path": "disk:/p",
                "backend": "yandex_disk"}

    monkeypatch.setattr(clouds, "upload", fake_upload)
    job = Job(id=uuid.uuid4().hex[:12], filename="28.10.2026, 10:00. - Встреча с ОО.mp4",
              audio_path="", language="ru", diarize=False, owner=u,
              deliver_protocol_cloud=True, status="done")
    store._jobs[job.id] = job
    docx = tmp_path / "p.docx"
    docx.write_bytes(b"d")
    store._deliver_protocol(job, docx)
    assert got == {"name": "28.10.2026, 10:00. - Встреча с ОО - протокол.docx",
                   "folder": None, "subfolder": "Встреча с ОО"}
    assert job.protocol_cloud_url == "https://disk.yandex.ru/d/p"


class TestРевью28_10:
    """Находки ревью раскладки (28.10): каждая — сценарий, который ломал
    правило «видео и протокол одной встречи — в одной папке»."""

    def test_день_только_из_начала_имени(self):
        assert meeting_series.meeting_day(
            "28.10.2026, 10:00. - Подготовка к конференции 05.11.2026.mp4") == "28.10.2026"
        # номер версии — не дата; нет даты в начале → время задачи
        ts = 1761638400.0                                     # 28.10.2025 UTC
        import time as _t
        assert meeting_series.meeting_day("Ревью релиза 2.4.15.mp4", ts) == \
            _t.strftime("%d.%m.%Y", _t.localtime(ts))

    def test_дата_в_названии_не_делает_встречу_серией_самой_себе(self):
        u = _user()
        name = "28.10.2026, 10:00. - Подготовка к конференции 05.11.2026.mp4"
        day = meeting_series.meeting_day(name)
        title = "Подготовка к конференции 05.11.2026"           # context_hint из Weeek
        p_video = cloud_folders.resolve(u, title, CFG, date=day)
        cloud_folders.placed(u, title, p_video, {"ok": True, "path": "disk:/R/Разовые встречи/v",
                                                 "backend": "yandex_disk"}, "recording")
        meeting_series.remember(u, title, {"summary": "x"}, job_id="j", date=day)
        p_proto = cloud_folders.resolve(u, title, CFG, date=day)
        assert p_video["kind"] == p_proto["kind"] == "oneoff"

    def test_серия_без_протоколов_не_откатывается_в_разовые(self, monkeypatch):
        monkeypatch.setattr(clouds, "move_to_sibling", lambda *a: {"ok": True})
        u, t = _user(), "Ежедневная запись"
        for day, want in (("20.10.2026", "oneoff"), ("21.10.2026", "series"),
                          ("22.10.2026", "series"), ("23.10.2026", "series")):
            p = cloud_folders.resolve(u, t, CFG, date=day)
            assert p["kind"] == want, day
            cloud_folders.adopt(u, t, p, CFG)
            cloud_folders.placed(u, t, p, {"ok": True, "path": f"disk:/R/{p['sub']}/{day}",
                                           "backend": "yandex_disk"}, "recording")

    def test_короткое_название_не_забирает_чужие_встречи(self):
        u = _user()
        p1 = cloud_folders.resolve(u, "Встреча", CFG, date="20.10.2026")
        cloud_folders.placed(u, "Встреча", p1, {"ok": True, "path": "disk:/R/Разовые встречи/a",
                                                "backend": "yandex_disk"}, "recording")
        p2 = cloud_folders.resolve(u, "Встреча с юристом", CFG, date="27.10.2026")
        assert p2["kind"] == "oneoff"

    def test_файл_отмеченный_во_время_переезда_не_теряется(self, monkeypatch):
        u, t = _user(), "ОО"
        p1 = cloud_folders.resolve(u, t, CFG, date="20.10.2026")
        cloud_folders.placed(u, t, p1, {"ok": True, "path": "disk:/R/Разовые встречи/a",
                                        "backend": "yandex_disk"}, "recording")

        def slow_move(item, sub, cfg):
            # пока «идёт» переезд, доставляется протокол той же встречи
            meeting_series.note_oneoff_file(u, t, {"path": "disk:/R/Разовые встречи/p",
                                                   "date": "20.10.2026"})
            return {"ok": True}

        monkeypatch.setattr(clouds, "move_to_sibling", slow_move)
        p2 = cloud_folders.resolve(u, t, CFG, date="27.10.2026")
        cloud_folders.adopt(u, t, p2, CFG)
        left = [f["path"] for f in meeting_series.find_exact(u, t)[1]["oneoff_files"]]
        assert left == ["disk:/R/Разовые встречи/p"]

    def test_google_без_подпапки_файл_не_переезжает(self):
        u = _user()
        p = cloud_folders.resolve(u, "Разовая", CFG, date="20.10.2026")
        cloud_folders.placed(u, "Разовая", p, {"ok": True, "path": "id1", "backend": "gdrive",
                                               "sub_failed": True}, "recording")
        assert meeting_series.find_exact(u, "Разовая")[1]["oneoff_files"] == []

    def test_предупреждение_переезда_видно_в_логе(self, monkeypatch):
        u, t = _user(), "Серия"
        p1 = cloud_folders.resolve(u, t, CFG, date="20.10.2026")
        cloud_folders.placed(u, t, p1, {"ok": True, "path": "disk:/R/Разовые встречи/a.mp4",
                                        "backend": "yandex_disk"}, "recording")
        monkeypatch.setattr(clouds, "move_to_sibling",
                            lambda *a: {"ok": True, "warning": "обновите ссылку в Weeek вручную"})
        logged = []
        cloud_folders.adopt(u, t, cloud_folders.resolve(u, t, CFG, date="27.10.2026"),
                            CFG, logged.append)
        assert any("вручную" in m and "a.mp4" in m for m in logged)


def test_сбой_раскладки_не_роняет_доставку_протокола(monkeypatch, tmp_path):
    from app.automation import settings as auto_settings
    from app.jobs import Job, store
    monkeypatch.setattr(auto_settings, "load", lambda _u: dict(CFG, protocol_folder="disk:/P"))
    monkeypatch.setattr(cloud_folders, "resolve",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("база легла")))
    got = {}
    monkeypatch.setattr(clouds, "upload", lambda *a, **k: got.update(k) or {
        "ok": True, "url": "https://disk.yandex.ru/d/p", "path": "disk:/P/p"})
    job = Job(id=uuid.uuid4().hex[:12], filename="28.10.2026, 10:00. - X.mp4", audio_path="",
              language="ru", diarize=False, owner=_user(), deliver_protocol_cloud=True,
              status="done")
    store._jobs[job.id] = job
    docx = tmp_path / "p.docx"
    docx.write_bytes(b"d")
    store._deliver_protocol(job, docx)
    assert got.get("folder") == "disk:/P" and job.protocol_cloud_url
