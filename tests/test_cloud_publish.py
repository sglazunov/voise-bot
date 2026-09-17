"""Файл лёг в облако, а публичной ссылки нет (боевой случай 17.09.2026).

«Ревью веб-дизайнеры» 10:00: запись и протокол загрузились на Яндекс.Диск, а
PUT /resources/publish не прошёл. Выгрузка считалась удачной (ok=True без
url): карточка писала «запись локально», Weeek не получил ни видео, ни
протокола, локальная копия удалялась, поздняя дозагрузка не запускалась, а
причина в шапке была «Не удалось выгрузить протокол в облако». Следующая
встреча в 11:00 прошла нормально — сбой был у одного шага, у публикации.
"""
from __future__ import annotations

import pytest

from app.automation import delivery
from app.automation.clouds import yandex_disk


class _Http:
    """Заглушка HTTP-слоя Яндекс.Диска: заливка всегда проходит, публикация
    отвечает по сценарию."""

    def __init__(self, publish_codes, meta=(200, {"public_url": "https://disk.yandex.ru/d/ok"})):
        self.publish_codes = list(publish_codes)
        self.meta = meta
        self.calls: list[str] = []

    def request(self, method, url, *, headers=None, params=None, data=None, timeout=120):
        self.calls.append(f"{method} {url.split('/v1/disk')[-1].split('?')[0]}")
        if url.endswith("/resources/publish"):
            code = self.publish_codes.pop(0) if self.publish_codes else self.publish_codes_last
            return code, b"{}"
        if "/resources" in url and method == "PUT":
            return 201, b""
        if "uploader" in url:
            return 201, b""
        return 200, b""

    publish_codes_last = 500

    def request_json(self, method, url, **kw):
        self.calls.append(f"{method}(json) {url.split('/v1/disk')[-1].split('?')[0]}")
        if url.endswith("/resources/upload"):
            return 200, {"href": "https://uploader.disk.yandex.net/x"}
        if url.endswith("/resources") and method == "GET":
            return self.meta
        return 200, {}


@pytest.fixture
def http(monkeypatch):
    def make(publish_codes, **kw):
        h = _Http(publish_codes, **kw)
        monkeypatch.setattr(yandex_disk, "request", h.request)
        monkeypatch.setattr(yandex_disk, "request_json", h.request_json)
        monkeypatch.setattr(yandex_disk.time, "sleep", lambda *_: None)
        return h
    return make


CFG = {"token": "t", "folder": "disk:/Записи"}


class TestПубликацияЯндексДиска:
    def test_публикация_повторяется_после_429(self, http, tmp_path):
        f = tmp_path / "v.mp4"; f.write_bytes(b"x")
        h = http([429, 503, 200])
        up = yandex_disk.upload(str(f), "v.mp4", CFG)
        assert up["ok"] and up["url"] == "https://disk.yandex.ru/d/ok" and up["published"]
        assert h.calls.count("PUT /resources/publish") == 3

    def test_без_ссылки_причина_названа_с_кодом(self, http, tmp_path):
        f = tmp_path / "v.mp4"; f.write_bytes(b"x")
        http([500, 500, 500])
        up = yandex_disk.upload(str(f), "v.mp4", CFG)
        assert up["ok"] and up["url"] is None and not up["published"]
        assert "HTTP 500" in up["public_note"] and "загружен" in up["public_note"]
        assert up["path"] == "disk:/Записи/v.mp4"

    def test_повторная_публикация_уже_лежащего_файла(self, http):
        h = http([200])
        res = yandex_disk.publish("disk:/Записи/v.mp4", CFG)
        assert res["ok"] and res["url"].startswith("https://disk.yandex.ru/")
        assert not any("upload" in c for c in h.calls), "файл заливаться заново не должен"

    def test_403_на_публикации_не_повторяется(self, http):
        h = http([403])
        res = yandex_disk.publish("disk:/Записи/v.mp4", CFG)
        assert not res["ok"] and "403" in res["error"]
        assert h.calls.count("PUT /resources/publish") == 1


class TestДоставка:
    def test_загружено_без_ссылки_это_не_успех(self, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"; f.write_bytes(b"x")
        logs: list[str] = []
        monkeypatch.setattr(delivery.time, "sleep", lambda *_: None)
        monkeypatch.setattr(delivery.clouds, "upload", lambda *a, **k: {
            "ok": True, "backend": "yandex_disk", "url": None, "path": "disk:/x/v.mp4",
            "public_note": "Файл загружен на Диск, но публичная ссылка не получена: HTTP 500"})
        monkeypatch.setattr(delivery.clouds, "publish", lambda p, cfg: {"ok": False, "error": "HTTP 500"})
        up = delivery.upload_with_retry(str(f), {"cloud": "yandex_disk"}, logs.append, attempts=2)
        assert not up["ok"] and up["uploaded"] and up["path"] == "disk:/x/v.mp4"
        assert "публичная ссылка" in up["error"]
        assert not delivery.delivered_elsewhere(up, str(f)), "локальную копию держим"

    def test_вторая_попытка_публикует_а_не_заливает_заново(self, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"; f.write_bytes(b"x")
        uploads: list[int] = []
        monkeypatch.setattr(delivery.time, "sleep", lambda *_: None)
        monkeypatch.setattr(delivery.clouds, "upload", lambda *a, **k: uploads.append(1) or {
            "ok": True, "backend": "yandex_disk", "url": None, "path": "disk:/x/v.mp4",
            "public_note": "нет ссылки"})
        monkeypatch.setattr(delivery.clouds, "publish",
                            lambda p, cfg: {"ok": True, "url": "https://disk.yandex.ru/d/late", "backend": "yandex_disk"})
        up = delivery.upload_with_retry(str(f), {"cloud": "yandex_disk"}, lambda *_: None, attempts=3)
        assert up["ok"] and up["url"] == "https://disk.yandex.ru/d/late"
        assert len(uploads) == 1
        assert delivery.delivered_elsewhere(up, str(f))

    def test_поздняя_дозагрузка_сначала_публикует(self, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"; f.write_bytes(b"x")
        called: list[str] = []
        monkeypatch.setattr(delivery.clouds, "publish",
                            lambda p, cfg: called.append("publish") or {"ok": True, "url": "https://disk.yandex.ru/d/z", "backend": "yandex_disk"})
        monkeypatch.setattr(delivery, "upload_with_retry",
                            lambda *a, **k: called.append("upload") or {"ok": False, "error": "x"})
        up = delivery.publish_or_upload(str(f), {}, "disk:/x/v.mp4", lambda *_: None)
        assert up["ok"] and up["url"] and called == ["publish"]

    def test_без_пути_в_облаке_обычная_выгрузка(self, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"; f.write_bytes(b"x")
        monkeypatch.setattr(delivery, "upload_with_retry",
                            lambda *a, **k: {"ok": True, "url": "https://disk.yandex.ru/d/u", "backend": "yandex_disk", "path": "disk:/u"})
        up = delivery.publish_or_upload(str(f), {}, None, lambda *_: None)
        assert up["ok"] and up["url"].endswith("/u")


class TestСнапшот:
    def test_путь_в_облаке_переживает_перезапуск(self):
        from app.automation import snapshots
        from app.automation.scheduler import MeetingState
        st = MeetingState(key="k", task_id="1", title="t", url="", start=None,
                          cloud_path="disk:/Записи/v.mp4", upload_error="нет ссылки")
        snap = snapshots.of_state(st)
        assert snap["cloud_path"] == "disk:/Записи/v.mp4"
