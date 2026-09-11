"""Папка протоколов в форме Яндекс.Диска не должна уезжать в Google как ID."""
from __future__ import annotations

import app.automation.clouds as clouds


def _capture(monkeypatch) -> dict:
    seen: dict = {}

    def fake_upload(file_path, name, cfg):
        seen["cfg"] = cfg
        return {"ok": True, "backend": "gdrive"}

    monkeypatch.setattr(clouds.gdrive, "upload", fake_upload)
    return seen


def test_yandex_style_folder_ignored_for_gdrive(monkeypatch, tmp_path):
    seen = _capture(monkeypatch)
    f = tmp_path / "p.docx"
    f.write_bytes(b"x")
    clouds.upload(str(f), "p.docx", {"cloud": "gdrive", "gdrive": {}},
                  folder="disk:/Телемост/Протоколы")
    assert "folder_id" not in seen["cfg"], "путь Я.Диска не идентификатор папки Google"


def test_real_folder_id_passes_through(monkeypatch, tmp_path):
    seen = _capture(monkeypatch)
    f = tmp_path / "p.docx"
    f.write_bytes(b"x")
    clouds.upload(str(f), "p.docx", {"cloud": "gdrive", "gdrive": {}},
                  folder="1AbCdEf_ghIJKlmNOpQrs")
    assert seen["cfg"]["folder_id"] == "1AbCdEf_ghIJKlmNOpQrs"


# --- снятый бэкенд (Яндекс.Диск убран 11.09.2026) --------------------------
# В сохранённых настройках боевого сервера лежит cloud: "yandex_disk". Тихий
# откат на локальную папку недопустим: её не чистит никто, и диск сервера
# забился бы записями встреч молча.

def test_removed_backend_explains_itself(tmp_path):
    f = tmp_path / "rec.mp4"
    f.write_bytes(b"x")
    res = clouds.upload(str(f), "rec.mp4", {"cloud": "yandex_disk"})
    assert res["ok"] is False
    assert res["backend"] == "yandex_disk"
    assert "Google Drive" in res["error"], "человеку надо сказать, что делать"


def test_removed_backend_does_not_fall_back_to_local(tmp_path):
    f = tmp_path / "rec.mp4"
    f.write_bytes(b"x")
    target = tmp_path / "cloud"
    clouds.upload(str(f), "rec.mp4",
                  {"cloud": "yandex_disk", "local_dir": str(target)})
    assert not target.exists(), "снятое облако не должно подменяться локальной папкой"


def test_removed_backend_visible_in_readiness():
    r = clouds.readiness({"cloud": "yandex_disk"})
    assert r["selected"] == "yandex_disk"
    assert r["backends"]["yandex_disk"]["ready"] is False
    assert "yandex_disk" not in clouds.BACKENDS
