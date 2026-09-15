"""Публичный лендинг на корне сайта (2026-09-15).

Незалогиненному — лендинг с ценами и почтой, залогиненному — SPA, как и
раньше. Счётчик Метрики вставляется только при VTX_METRIKA_ID, и только тогда
CSP лендинга пускает хосты Метрики; остальной сайт CSP не меняет.
"""
from __future__ import annotations

import pytest
from conftest import login, register

from app import config
from app.main import inline_script_hashes


def _script_src(csp: str) -> str:
    return [d for d in csp.split(";") if d.strip().startswith("script-src")][0]


class TestЛендинг:
    def test_незалогиненному_корень_отдаёт_лендинг(self, client):
        r = client.get("/")
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
        for s in ("MeetFlow", "200 ₽", "300 ₽", "6 000 ₽", "до 50 встреч", config.CONTACT_EMAIL,
                  'rel="canonical"', "application/ld+json", "Телемост"):
            assert s in r.text, s

    def test_все_призывы_ведут_на_письмо_а_не_на_регистрацию(self, client):
        html = client.get("/").text
        assert 'href="/register"' not in html
        assert html.count(f"mailto:{config.CONTACT_EMAIL}") >= 8
        assert "любой трекинговой системой" in html or "любую трекинговую систему" in html

    def test_страница_landing_тоже_публична(self, client):
        assert client.get("/landing").status_code == 200

    def test_залогиненному_корень_не_лендинг(self, client):
        register(client)
        login(client)
        r = client.get("/")
        assert "Платите за встречи" not in r.text     # либо SPA, либо 404 без сборки
        assert r.status_code in (200, 404)

    def test_остальное_по_прежнему_закрыто(self, client):
        assert client.get("/settings", follow_redirects=False).status_code == 303
        assert client.get("/api/jobs").status_code == 401

    def test_robots_и_sitemap(self, client, monkeypatch):
        monkeypatch.setattr(config, "SITE_URL", "https://voice.example.ru")
        r = client.get("/robots.txt")
        assert r.status_code == 200 and "Sitemap: https://voice.example.ru/sitemap.xml" in r.text
        assert "Disallow: /" in r.text and "Allow: /$" in r.text
        s = client.get("/sitemap.xml")
        assert s.status_code == 200 and "<loc>https://voice.example.ru/</loc>" in s.text
        assert "https://voice.example.ru/" in client.get("/").text

    def test_канонический_адрес_из_запроса_без_настройки(self, client, monkeypatch):
        monkeypatch.setattr(config, "SITE_URL", "")
        assert 'href="http://testserver/"' in client.get("/").text


class TestМетрика:
    def test_без_номера_счётчика_нет_и_хостов_в_csp(self, client, monkeypatch):
        monkeypatch.setattr(config, "METRIKA_ID", "")
        r = client.get("/")
        assert "mc.yandex.ru" not in r.text
        assert "mc.yandex.ru" not in r.headers["Content-Security-Policy"]

    def test_счётчик_вставляется_и_разрешён_хэшем(self, client, monkeypatch):
        monkeypatch.setattr(config, "METRIKA_ID", "12345678")
        r = client.get("/")
        assert "ym(12345678, 'init'" in r.text and "mc.yandex.ru/watch/12345678" in r.text
        assert "tag.js?id=12345678" in r.text and "webvisor:true" in r.text
        csp = r.headers["Content-Security-Policy"]
        script_src = _script_src(csp)
        assert "https://mc.yandex.ru" in script_src and "unsafe-inline" not in script_src
        for h in inline_script_hashes(r.text):
            assert f"'sha256-{h}'" in script_src
        assert "connect-src 'self' https://mc.yandex.ru" in csp
        assert "img-src 'self' data: blob: https://mc.yandex.ru" in csp

    def test_номер_по_умолчанию_и_выключение_нулём(self):
        # Без перезагрузки модуля: reload(config) сбивал настройки соседним тестам.
        assert config._metrika_id("112674625") == "112674625"
        assert config._metrika_id(" 12345678 ") == "12345678"
        assert config._metrika_id("0") == "" and config._metrika_id("") == ""
        assert config._metrika_id("off") == ""

    def test_хосты_метрики_не_протекают_на_страницу_входа(self, client, monkeypatch):
        monkeypatch.setattr(config, "METRIKA_ID", "12345678")
        assert "mc.yandex.ru" not in client.get("/login").headers["Content-Security-Policy"]


class TestИконка:
    @pytest.mark.parametrize("path,ctype", [
        ("/favicon.svg", "image/svg+xml"), ("/favicon.ico", "image/x-icon"),
        ("/favicon-32.png", "image/png"), ("/apple-touch-icon.png", "image/png"),
        ("/icon-512.png", "image/png"), ("/og-image.png", "image/png")])
    def test_иконки_отдаются_без_входа(self, client, path, ctype):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith(ctype)
        assert "max-age" in r.headers.get("cache-control", "")

    def test_ссылки_на_иконку_на_всех_страницах(self, client):
        for path in ("/", "/login"):
            html = client.get(path).text
            assert 'href="/favicon.svg"' in html and 'href="/apple-touch-icon.png"' in html
        assert 'property="og:image"' in client.get("/").text

    def test_в_карточке_протокола_нет_заголовков_h4(self, client):
        html = client.get("/").text
        assert "<h4" not in html and html.count("<h1") == 1
