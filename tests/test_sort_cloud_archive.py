"""Разовая раскладка архива облака по папкам встреч (scripts/sort_cloud_archive.py).

Запрос владельца 28.10: новые встречи раскладываются сами, а сотни старых
файлов лежат плоско в «Телемост-записи» и «Телемост-протоколы» — разложить их
скриптом, а не руками.
"""
from __future__ import annotations

import uuid

from app import meeting_series
from app.automation.clouds import yandex_disk
from scripts import sort_cloud_archive as sca


def _user():
    return "u" + uuid.uuid4().hex[:8]


def _setup(tmp_path, names, protos=()):
    root = tmp_path / "rec"
    (root / "protocols").mkdir(parents=True)
    for n in names:
        (root / n).write_bytes(b"v")
    for n in protos:
        (root / "protocols" / n).write_bytes(b"p")
    cfg = {"cloud": "local", "local_dir": str(root), "folders_by_series": True,
           "oneoff_folder": "Разовые встречи", "protocol_folder": "disk:/Протоколы"}
    return root, cfg


OO1 = "20.10.2026, 10:00. - Встреча с ОО.mp4"
OO2 = "27.10.2026, 10:00. - Встреча с ОО.mp4"
OO2P = "27.10.2026, 10:00. - Встреча с ОО - протокол.docx"
JUR = "22.10.2026, 15:00. - Созвон с юристом.mp4"


def _run(user, cfg, only=""):
    cloud = sca.backend_for(cfg)
    files, skipped = sca.collect(cloud)
    return cloud, sca.plan(user, cfg, cloud, files, only), skipped


def test_план_серия_и_разовая_и_чужие_файлы(tmp_path):
    root, cfg = _setup(tmp_path, [OO1, OO2, JUR, "смета.xlsx", "заметки.docx"], [OO2P])
    _cloud, groups, skipped = _run(_user(), cfg)
    by = {g["title"]: g for g in groups}
    assert by["Встреча с ОО"]["place"]["sub"] == "Встреча с ОО"
    assert by["Встреча с ОО"]["place"]["kind"] == "series"
    assert len(by["Встреча с ОО"]["moves"]) == 3          # два видео и протокол
    assert by["Созвон с юристом"]["place"]["sub"] == "Разовые встречи"
    assert len(skipped) == 2                               # без даты в начале имени
    # план ничего не двигает
    assert (root / OO1).exists() and (root / "protocols" / OO2P).exists()


def test_перенос_кладёт_видео_и_протокол_рядом(tmp_path):
    u = _user()
    root, cfg = _setup(tmp_path, [OO1, OO2, JUR], [OO2P])
    cloud, groups, _ = _run(u, cfg)
    res = sca.apply(u, cloud, groups)
    assert res["moved"] == 4 and not res["failed"]
    assert sorted(p.name for p in (root / "Встреча с ОО").iterdir()) == sorted([OO1, OO2, OO2P])
    assert (root / "Разовые встречи" / JUR).exists()
    # дни запомнены: следующая встреча ОО сразу в папку серии
    e = meeting_series.load(u)["встреча с оо"]
    assert set(e["seen_dates"]) == {"20.10.2026", "27.10.2026"}
    # разовая — в очереди переезда, если название станет серией
    j = meeting_series.load(u)["созвон с юристом"]
    assert j["oneoff_files"][0]["path"].endswith("Разовые встречи/" + JUR)
    # повторный запуск — переносить нечего
    _c, again, _ = _run(u, cfg)
    assert not any(g["moves"] for g in again)


def test_разовая_из_прошлого_запуска_уезжает_в_серию(tmp_path):
    u = _user()
    root, cfg = _setup(tmp_path, [OO2])
    (root / "Разовые встречи").mkdir()
    (root / "Разовые встречи" / OO1).write_bytes(b"v")
    cloud, groups, _ = _run(u, cfg)
    sca.apply(u, cloud, groups)
    assert (root / "Встреча с ОО" / OO1).exists() and (root / "Встреча с ОО" / OO2).exists()


def test_карточка_решает(tmp_path):
    u = _user()
    meeting_series.upsert(u, "Встреча с ОО", {"folder_mode": "oneoff"})
    meeting_series.upsert(u, "Созвон с юристом", {"context": "юрист компании"})
    _root, cfg = _setup(tmp_path, [OO1, OO2, JUR])
    _c, groups, _ = _run(u, cfg)
    by = {g["key"]: g["place"]["sub"] for g in groups}
    assert by == {"встреча с оо": "Разовые встречи", "созвон с юристом": "Созвон с юристом"}


def test_только_одна_серия(tmp_path):
    _root, cfg = _setup(tmp_path, [OO1, OO2, JUR])
    _c, groups, _ = _run(_user(), cfg, only="Встреча с ОО")
    assert [g["key"] for g in groups] == ["встреча с оо"]


def test_лимит(tmp_path):
    u = _user()
    root, cfg = _setup(tmp_path, [OO1, OO2, JUR])
    cloud, groups, _ = _run(u, cfg)
    assert sca.apply(u, cloud, groups, limit=1)["moved"] == 1
    assert sum(1 for p in root.iterdir() if p.is_file()) == 2


def test_одноимённый_файл_не_затирается(tmp_path):
    u = _user()
    root, cfg = _setup(tmp_path, [OO1, OO2])
    (root / "Встреча с ОО").mkdir()
    (root / "Встреча с ОО" / OO1).write_bytes(b"other")
    cloud, groups, _ = _run(u, cfg)
    res = sca.apply(u, cloud, groups)
    assert res["moved"] == 1 and len(res["failed"]) == 1
    assert (root / "Встреча с ОО" / OO1).read_bytes() == b"other"


class _LinkChanges:
    label, root = "fake", "disk:/R"
    sources: list = []

    def __init__(self):
        self.calls = 0

    def dest(self, sub, name):
        return f"{self.root}/{sub}/{name}"

    def move(self, src, dst):
        self.calls += 1
        return {"ok": False, "link_changed": True, "error": "ссылка сменилась"}


def test_сменилась_ссылка_останавливает_весь_перенос():
    cloud = _LinkChanges()
    groups = [{"place": {"sub": "S", "kind": "series"},
               "moves": [{"name": n, "path": f"disk:/R/{n}", "dst": f"disk:/R/S/{n}",
                          "day": "20.10.2026", "kind": "video"} for n in (OO1, OO2)]}]
    res = sca.apply(_user(), cloud, groups)
    assert cloud.calls == 1 and res["stopped"] and res["moved"] == 0


def test_яндекс_список_папки_постранично(monkeypatch):
    pages = {0: [{"name": f"{i}.mp4", "path": f"disk:/R/{i}.mp4", "type": "file"}
                 for i in range(3)] + [{"name": "Серия", "path": "disk:/R/Серия", "type": "dir"}],
             4: [{"name": "x.mp4", "path": "disk:/R/x.mp4", "type": "file"}]}
    seen = []

    def rj(method, url, headers=None, params=None, **kw):
        seen.append((params["offset"], headers["Authorization"]))
        return 200, {"_embedded": {"items": pages.get(params["offset"], []), "total": 5}}

    monkeypatch.setattr(yandex_disk, "request_json", rj)
    out = yandex_disk.list_folder("disk:/R", {"token": "w", "read_token": "r"})
    assert [f["name"] for f in out] == ["0.mp4", "1.mp4", "2.mp4", "x.mp4"]
    assert seen == [(0, "OAuth r"), (4, "OAuth r")]      # читает токеном чтения


def test_яндекс_без_прав_на_чтение_внятно(monkeypatch):
    monkeypatch.setattr(yandex_disk, "request_json", lambda *a, **k: (403, {}))
    try:
        yandex_disk.list_folder("disk:/R", {"token": "w"})
    except yandex_disk.CloudError as e:
        assert "ЧТЕНИЕ" in str(e)
    else:
        raise AssertionError("ожидалась ошибка")
    monkeypatch.setattr(yandex_disk, "request_json", lambda *a, **k: (404, {}))
    assert yandex_disk.list_folder("disk:/R", {"token": "w"}) == []


def test_протокол_второй_части_записи_той_же_серии():
    assert meeting_series.series_key(
        "27.10.2026, 10:00. - Встреча с ОО (2) - протокол.docx") == "встреча с оо"
    assert meeting_series.display_title(
        "27.10.2026, 10:00. - Встреча с ОО (2) - протокол.docx") == "Встреча с ОО"


def test_яндекс_пути(monkeypatch):
    cfg = {"cloud": "yandex_disk", "protocol_folder": "disk:/Протоколы",
           "yandex_disk": {"token": "t", "folder": "disk:/Телемост-записи/"}}
    y = sca.backend_for(cfg)
    assert y.sources == ["disk:/Телемост-записи", "disk:/Протоколы",
                         "disk:/Телемост-записи/Разовые встречи"]
    assert y.dest("ОО", "a.mp4") == "disk:/Телемост-записи/ОО/a.mp4"


def test_папки_встреч_только_внутри_папки_записей(monkeypatch):
    """Владелец 28.10: папки встреч не разбрасывать по всему Диску."""
    for folder in ("", "disk:/", "disk:", "/"):
        assert yandex_disk.meetings_root({"folder": folder}) == "disk:/Телемост-записи"
    calls = []
    monkeypatch.setattr(yandex_disk, "upload", lambda f, n, c: calls.append(c["folder"]) or {"ok": True})
    from app.automation import clouds
    clouds.upload("x", "a.mp4", {"cloud": "yandex_disk", "yandex_disk": {"folder": "disk:/"}},
                  subfolder="ОО")
    assert calls == ["disk:/Телемост-записи/ОО"]
