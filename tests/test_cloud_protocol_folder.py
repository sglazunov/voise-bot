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
