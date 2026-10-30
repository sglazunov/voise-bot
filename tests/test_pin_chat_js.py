"""Прокрутка чата к низу перед чтением (`_pin_chat_bottom`, `_PIN_JS`).

Адверсариальное ревью 30.10: `_PIN_JS` тянул к «низу» список чатов оболочки
Мессенджера (превью «Иван: стоп»), а список `column-reverse` у самого низа
считал не у низа — `pinned` становился ложным навсегда. Ни тело скрипта, ни
`_pin_chat_bottom` не проверялись ничем (мутант «всегда True» выживал).

Тело скрипта проверяется в НАСТОЯЩЕМ Chromium (headless shell из Playwright,
`--dump-dom`); без браузера эти тесты пропускаются.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from app.automation.recorder.browser import TelemostBot


# --- _pin_chat_bottom: что делается с результатами фреймов -------------------
class _Fr:
    def __init__(self, result=None, boom=False, shell=False):
        self.result, self.boom, self.shell = result, boom, shell
        self.calls = []

    def evaluate(self, js, arg=None):
        self.calls.append((js, arg))
        if self.boom:
            raise RuntimeError("frame detached")
        return self.result

    def query_selector(self, sel):
        return object() if self.shell and "yamb" in sel else None


class _Pg:
    def __init__(self, frames): self.frames = frames


def _bot(frames):
    b = object.__new__(TelemostBot)
    b._page = _Pg(frames) if frames is not None else None
    return b


def test_нет_страницы_или_слова_не_у_низа():
    assert _bot(None)._pin_chat_bottom("стоп") is False
    fr = _Fr({"found": 1, "bottom": True})
    assert _bot([fr])._pin_chat_bottom("") is False
    assert fr.calls == []


def test_ни_одного_списка_не_найдено_не_у_низа():
    assert _bot([_Fr({"found": 0, "bottom": True})])._pin_chat_bottom("стоп") is False
    assert _bot([_Fr(None)])._pin_chat_bottom("стоп") is False
    assert _bot([_Fr(boom=True)])._pin_chat_bottom("стоп") is False


def test_все_найденные_у_низа():
    fr = _Fr({"found": 2, "bottom": True})
    assert _bot([fr, _Fr({"found": 0, "bottom": True})])._pin_chat_bottom("стоп") is True
    assert fr.calls == [(TelemostBot._PIN_JS, "стоп")]


def test_один_не_у_низа_значит_не_у_низа():
    frames = [_Fr({"found": 1, "bottom": True}), _Fr({"found": 1, "bottom": False})]
    assert _bot(frames)._pin_chat_bottom("стоп") is False
    frames = [_Fr({"found": 1, "bottom": False}), _Fr({"found": 1, "bottom": True})]
    assert _bot(frames)._pin_chat_bottom("стоп") is False


def test_сбой_одного_фрейма_не_роняет_остальные():
    frames = [_Fr(boom=True), _Fr({"found": 1, "bottom": True})]
    assert _bot(frames)._pin_chat_bottom("стоп") is True


def test_при_оболочке_список_чатов_не_трогается():
    """Главный документ оболочки — список чатов: его не прокручивать и его
    «низ» в расчёт не брать."""
    main = _Fr({"found": 1, "bottom": False}, shell=True)
    meeting = _Fr({"found": 1, "bottom": True})
    assert _bot([main, meeting])._pin_chat_bottom("стоп") is True
    assert main.calls == [] and len(meeting.calls) == 1


def test_без_оболочки_главный_документ_и_есть_встреча():
    main = _Fr({"found": 1, "bottom": True})
    assert _bot([main])._pin_chat_bottom("стоп") is True
    assert len(main.calls) == 1


# --- тело _PIN_JS в настоящем Chromium ---------------------------------------
def _chromium() -> str | None:
    env = os.environ.get("VTX_TEST_CHROMIUM")
    if env and Path(env).exists():
        return env
    roots = [os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright"),
             os.path.expanduser("~/.cache/ms-playwright")]
    for root in roots:
        for pat in ("chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell*",
                    "chromium-*/chrome-*/chrome", "chromium-*/chrome-*/chrome.exe"):
            for p in sorted(glob.glob(os.path.join(root, pat)), reverse=True):
                if os.path.isfile(p) and os.access(p, os.X_OK):
                    return p
    return None


_CHROME = _chromium()
needs_chromium = pytest.mark.skipif(_CHROME is None, reason="нет Chromium из Playwright")

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
.box { height: 100px; overflow-y: auto; }
.rev { display: flex; flex-direction: column-reverse; }
.tall { height: 1000px; flex-shrink: 0; }
</style></head><body>
<div id="chat" class="box"><div class="tall">Сергей Беск<br>стоп<br>11:26
  <div>Иван<br>стоп</div></div></div>
<div id="rev" class="box rev"><div class="tall">стоп</div></div>
<div id="revup" class="box rev"><div class="tall">стоп</div></div>
<div id="noword" class="box"><div class="tall">привет</div></div>
<div id="small" class="box"><div style="height:130px">стоп</div></div>
<div id="hidden" style="height:100px;overflow:hidden"><div class="tall">стоп</div></div>
<div id="outer" class="box"><div class="tall">
  <div id="inner" class="box"><div class="tall">стоп</div></div></div></div>
<pre id="out"></pre>
<script>
const PIN = __PIN__;
const $ = id => document.getElementById(id);
$("revup").scrollTop = -300;
const before = {revup: $("revup").scrollTop};
const r1 = PIN("СТОП");
const pos = {};
for (const id of ["chat", "rev", "revup", "noword", "small", "hidden", "outer", "inner"]) {
  pos[id] = {top: $(id).scrollTop, max: $(id).scrollHeight - $(id).clientHeight};
}
// Список, который не даёт себя прокрутить (подгрузка, перехват прокрутки).
const stuck = document.createElement("div");
stuck.className = "box";
stuck.innerHTML = '<div class="tall">стоп</div>';
document.body.appendChild(stuck);
Object.defineProperty(stuck, "scrollTop", {get() { return 0; }, set(v) {}});
const r2 = PIN("стоп");
const r3 = PIN("нет такого слова");
$("out").textContent = JSON.stringify({before, r1, pos, r2, r3});
</script></body></html>"""


@pytest.fixture(scope="module")
def pin_result(tmp_path_factory):
    page = tmp_path_factory.mktemp("pin") / "pin.html"
    page.write_text(_PAGE.replace("__PIN__", TelemostBot._PIN_JS), encoding="utf-8")
    out = subprocess.run(
        [_CHROME, "--headless", "--no-sandbox", "--disable-gpu", "--dump-dom",
         page.resolve().as_uri()],
        capture_output=True, timeout=60)
    dom = out.stdout.decode("utf-8", "replace")
    m = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
    assert m and m.group(1), f"скрипт не отработал: {dom[-500:]} {out.stderr[-500:]!r}"
    return json.loads(m.group(1))


@needs_chromium
def test_js_обычный_список_прокручен_к_низу(pin_result):
    p = pin_result["pos"]["chat"]
    assert p["max"] > 0 and p["top"] == p["max"]


@needs_chromium
def test_js_column_reverse_у_низа_не_трогается_и_считается_низом(pin_result):
    assert pin_result["pos"]["rev"]["top"] == 0
    assert pin_result["r1"]["bottom"] is True


@needs_chromium
def test_js_column_reverse_прокрученный_вверх_возвращается_к_низу(pin_result):
    assert pin_result["before"]["revup"] == -300
    assert pin_result["pos"]["revup"]["top"] == 0


@needs_chromium
def test_js_чужие_и_мелкие_списки_не_считаются(pin_result):
    pos = pin_result["pos"]
    assert pos["noword"]["top"] == 0          # без слова — не трогается
    assert pos["hidden"]["top"] == 0          # overflow: hidden — не список
    assert pos["small"]["top"] == 0           # переполнение меньше 40 px
    # Ближайший прокручиваемый предок — внутренний, внешний не трогается.
    assert pos["inner"]["top"] == pos["inner"]["max"] > 0
    assert pos["outer"]["top"] == 0
    # chat (два «стоп» — один список), rev, revup, inner.
    assert pin_result["r1"]["found"] == 4


@needs_chromium
def test_js_список_не_у_низа_даёт_false(pin_result):
    assert pin_result["r2"] == {"found": 5, "bottom": False}


@needs_chromium
def test_js_слова_нет_списков_нет(pin_result):
    assert pin_result["r3"] == {"found": 0, "bottom": True}


# --- второй раунд ревью 30.10 (§5) -------------------------------------------
# Отдельная страница: на общей `_PAGE` выше прежние тесты считают списки.
_PAGE2 = """<!doctype html><html><head><meta charset="utf-8"><style>
.box { height: 100px; overflow-y: auto; }
.tall { height: 1000px; flex-shrink: 0; }
</style></head><body>
<div id="blockrev" class="box" style="display:block;flex-direction:column-reverse">
  <div class="tall">стоп</div></div>
<div id="revnear" class="box" style="display:flex;flex-direction:column-reverse">
  <div class="tall">стоп</div></div>
<div id="revfar" class="box" style="display:flex;flex-direction:column-reverse">
  <div class="tall">стоп</div></div>
<div id="inline" class="box" style="display:inline-flex;flex-direction:column-reverse;width:100px">
  <div class="tall">стоп</div></div>
<pre id="out"></pre>
<script>
const PIN = __PIN__;
const $ = id => document.getElementById(id);
$("revnear").scrollTop = -10;
$("revfar").scrollTop = -30;
$("inline").scrollTop = -300;
const before = {revnear: $("revnear").scrollTop, revfar: $("revfar").scrollTop};
const r1 = PIN("стоп");
const pos = {};
for (const id of ["blockrev", "revnear", "revfar", "inline"]) {
  pos[id] = {top: $(id).scrollTop, max: $(id).scrollHeight - $(id).clientHeight};
}
$("out").textContent = JSON.stringify({before, r1, pos});
</script></body></html>"""


@pytest.fixture(scope="module")
def pin_result2(tmp_path_factory):
    page = tmp_path_factory.mktemp("pin2") / "pin2.html"
    page.write_text(_PAGE2.replace("__PIN__", TelemostBot._PIN_JS), encoding="utf-8")
    out = subprocess.run(
        [_CHROME, "--headless", "--no-sandbox", "--disable-gpu", "--dump-dom",
         page.resolve().as_uri()],
        capture_output=True, timeout=60)
    dom = out.stdout.decode("utf-8", "replace")
    m = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
    assert m and m.group(1), f"скрипт не отработал: {dom[-500:]} {out.stderr[-500:]!r}"
    return json.loads(m.group(1))


@needs_chromium
def test_js_column_reverse_без_flex_обычный_список(pin_result2):
    """`flex-direction: column-reverse` без `display:flex` ничего не
    переворачивает: верх такого списка (scrollTop 0) — это верх, а не низ."""
    p = pin_result2["pos"]["blockrev"]
    assert p["max"] > 0 and p["top"] == p["max"]
    assert pin_result2["r1"]["bottom"] is True


@needs_chromium
def test_js_column_reverse_допуск_у_низа(pin_result2):
    """В пределах 20 px от низа список не дёргается; дальше — к низу (0)."""
    assert pin_result2["before"] == {"revnear": -10, "revfar": -30}
    assert pin_result2["pos"]["revnear"]["top"] == -10
    assert pin_result2["pos"]["revfar"]["top"] == 0


@needs_chromium
def test_js_inline_flex_column_reverse_тоже_перевёрнут(pin_result2):
    assert pin_result2["pos"]["inline"]["top"] == 0
    assert pin_result2["r1"] == {"found": 4, "bottom": True}
