"""V17: выгрузка в Google Drive идёт потоком, а не через чтение файла в RAM."""
from __future__ import annotations

import app.automation.clouds.gdrive as gdrive


def _body(tmp_path, payload: bytes):
    f = tmp_path / "rec.mp4"
    f.write_bytes(payload)
    return gdrive._MultipartBody(b"HEAD", f, b"TAIL")


def test_length_matches_what_is_read(tmp_path):
    body = _body(tmp_path, b"x" * 5000)
    try:
        got = b""
        while True:
            chunk = body.read(1024)
            if not chunk:
                break
            got += chunk
    finally:
        body.close()
    assert got == b"HEAD" + b"x" * 5000 + b"TAIL"
    assert body.length == len(got)


def test_file_is_read_in_chunks(tmp_path):
    """Главное свойство: файл не приходит одним куском в память."""
    body = _body(tmp_path, b"y" * 4096)
    try:
        sizes = []
        while True:
            chunk = body.read(1024)
            if not chunk:
                break
            sizes.append(len(chunk))
    finally:
        body.close()
    assert max(sizes) <= 1024


def test_empty_file(tmp_path):
    body = _body(tmp_path, b"")
    try:
        assert body.read(64) == b"HEAD"
        assert body.read(64) == b"TAIL"
        assert body.read(64) == b""
    finally:
        body.close()
    assert body.length == 8


def test_upload_streams_and_sets_content_length(tmp_path, monkeypatch):
    src = tmp_path / "meet.mp4"
    src.write_bytes(b"z" * 3000)
    seen: dict = {}

    monkeypatch.setattr(gdrive, "_access_token", lambda cfg: "tok")

    def fake_request_json(method, url, **kw):
        if url == gdrive.UPLOAD_URL:
            seen["headers"] = kw["headers"]
            seen["timeout"] = kw.get("timeout")
            data = kw["data"]
            assert hasattr(data, "read"), "тело должно быть потоком, а не bytes"
            total = 0
            while True:
                c = data.read(1 << 16)
                if not c:
                    break
                total += c.__len__()
            seen["total"] = total
            return 200, {"id": "FILE1"}
        return 200, {}

    monkeypatch.setattr(gdrive, "request_json", fake_request_json)
    monkeypatch.setattr(gdrive, "request", lambda *a, **kw: (200, b"{}"))

    res = gdrive.upload(str(src), "meet.mp4", {})
    assert res["ok"] is True
    assert seen["total"] == int(seen["headers"]["Content-Length"])
    # Таймаут должен быть куда больше умолчания в 120 секунд.
    assert seen["timeout"] >= 600
