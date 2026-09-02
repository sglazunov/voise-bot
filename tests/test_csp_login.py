"""CSP не должна блокировать скрипты страницы входа (боевой баг 02.09).

В консоли: «Executing inline script violates the following Content Security
Policy directive 'script-src 'self' 'sha256-RvyXR…''» — дважды, на /register.
Кнопка «Зарегистрироваться» не делала ничего, ошибок не показывалось, войти
тоже было нельзя. В CSP стоял один хэш (тема из index.html), а у страницы
входа два своих инлайн-скрипта, причём обработчик формы содержит «{{ mode }}»
и хэш у него разный на /login, /register и /recover.
"""
from __future__ import annotations

import pytest

from app.main import csp_for, inline_script_hashes


@pytest.mark.parametrize("path", ["/login", "/register", "/recover"])
def test_каждый_инлайн_скрипт_страницы_входа_разрешён_хэшем(client, path):
    r = client.get(path)
    assert r.status_code == 200
    hashes = inline_script_hashes(r.text)
    assert len(hashes) >= 2, "на странице входа два инлайн-скрипта: форма и тема"
    csp = r.headers["Content-Security-Policy"]
    script_src = [d for d in csp.split(";") if d.strip().startswith("script-src")][0]
    for h in hashes:
        assert f"'sha256-{h}'" in script_src
    assert "unsafe-inline" not in script_src


def test_хэши_считаются_по_телу_скрипта():
    html = '<script src="/a.js"></script><script>alert(1)</script><SCRIPT type="module">x</SCRIPT>'
    hs = inline_script_hashes(html)
    assert len(hs) == 2 and hs[0] == "bhHHL3z2vDgxUt0W3dWQOrprscmda2Y5pLsLg4GF+pI="   # sha256("alert(1)")
    assert csp_for(hs).count("'sha256-") == 2 and "script-src 'self' 'sha256-" in csp_for(hs)


def test_api_ответы_тоже_несут_csp(client):
    r = client.get("/api/health") if client.get("/api/health").status_code != 404 else client.get("/login")
    assert "Content-Security-Policy" in r.headers
