"""Вёрстка Телемоста поменялась (21.09.2026), бот не вошёл — а в карточке
только «Не удалось войти (см. скриншот)». Чтобы подобрать селекторы, нужны
ТОЧНЫЕ тексты кнопок на странице: их теперь пишет сам бот."""
from __future__ import annotations

from app.automation.recorder.browser import page_summary


class _El:
    def __init__(self, text="", aria="", ph="", visible=True):
        self._t, self._a, self._p, self._v = text, aria, ph, visible

    def is_visible(self): return self._v
    def inner_text(self): return self._t
    def get_attribute(self, name):
        return {"aria-label": self._a, "placeholder": self._p}.get(name) or None


class _Page:
    url = "https://telemost.yandex.ru/j/123"

    def __init__(self, els): self._els = els
    def title(self): return "Яндекс Телемост"
    def query_selector_all(self, sel): return self._els


def test_сводка_страницы_называет_кнопки():
    page = _Page([_El("Подключиться к встрече"), _El("", aria="Выключить микрофон"),
                  _El("скрытая", visible=False), _El("", ph="Ваше имя"),
                  _El("Подключиться к встрече")])
    s = page_summary(page)
    assert "«Яндекс Телемост»" in s and "telemost.yandex.ru/j/123" in s
    assert "Подключиться к встрече" in s and "Выключить микрофон" in s and "Ваше имя" in s
    assert "скрытая" not in s
    assert s.count("Подключиться к встрече") == 1, "дубли схлопываются"


def test_пустая_страница_не_роняет_сводку():
    class Broken:
        url = "x"
        def title(self): raise RuntimeError("closed")
        def query_selector_all(self, sel): raise RuntimeError("closed")
    assert "ни одной видимой" in page_summary(Broken())


# ---------------------------------------------------------------------------
# 21.09.2026: встреча внутри <iframe> оболочки Мессенджера. Главный документ —
# список чатов, кнопки «Подключиться»/«Участники»/«Чат» — во вложенном фрейме.
# Поиск только по главному документу их не видел — бот «не входил» на встречу.
# ---------------------------------------------------------------------------
from app.automation.recorder.browser import TelemostBot, _FULLSCREEN_CLASS


class _Btn(_El):
    def __init__(self, text="", aria="", box=None, **kw):
        super().__init__(text, aria, **kw)
        self.clicks = 0
        self._box = box

    def click(self): self.clicks += 1
    def fill(self, v): self.value = v
    def bounding_box(self): return self._box


class _Frame:
    """Фрейм: словарь «селектор → элемент»; всё остальное — пусто."""
    def __init__(self, url, els, content="<html/>", box=None, tag="button"):
        self.url, self._els, self._content, self._box, self._tag = url, els, content, box, tag

    def query_selector(self, sel): return self._els.get(sel)
    def query_selector_all(self, sel):
        return [e for k, e in self._els.items() if self._tag in sel or k == sel]
    def content(self): return self._content
    def frame_element(self):
        box = self._box
        class _FE:
            def bounding_box(self_inner): return box
        return _FE()


class _FramedPage(_Frame):
    url = "https://telemost.yandex.ru/j/1"

    def __init__(self, els, child: _Frame, fullscreen=False):
        super().__init__(self.url, els)
        self.frames = [self, child]
        self.fullscreen = fullscreen
        self.waits = 0

    def title(self): return "Яндекс Телемост — 8 новых сообщений"
    def wait_for_timeout(self, ms): self.waits += 1
    def evaluate(self, js):
        assert _FULLSCREEN_CLASS in js
        return self.fullscreen


def _bot(page):
    bot = object.__new__(TelemostBot)
    bot._page = page
    bot.log = []
    bot._on_log = bot.log.append
    bot._should_stop = None
    bot.cfg = {}
    return bot


def test_элемент_во_вложенном_фрейме_находится():
    join = _Btn("Подключиться")
    child = _Frame("https://telemost.yandex.ru/j/1?embedded",
                   {'button:has-text("Подключиться")': join,
                    'button:has-text("Участники")': _Btn("Участники 1")})
    page = _FramedPage({'button[aria-label="Почта"]': _Btn("", aria="Почта")}, child)
    bot = _bot(page)
    assert bot._find('button:has-text("Подключиться")') is join
    assert bot._click_any(['button:has-text("Подключиться")'], overall_ms=1000)
    assert join.clicks == 1
    assert bot.is_in_call(), "«Участники» во фрейме = в звонке"
    assert bot.participant_count() == 1


def test_сводка_страницы_видит_кнопки_фрейма():
    child = _Frame("child", {"b": _Btn("Подключиться")})
    page = _FramedPage({"a": _Btn("", aria="Почта")}, child)
    s = page_summary(page)
    assert "Почта" in s and "Подключиться" in s and "фреймов: 2" in s


def test_разворот_окна_встречи_по_подписи_кнопки(tmp_path):
    toggle = _Btn("", aria="Свернуть панель")
    child = _Frame("child", {'button[aria-label*="Свернуть"]': toggle})
    page = _FramedPage({}, child)
    bot = _bot(page)
    # клик переводит оболочку в полноэкранный режим
    orig_click = toggle.click
    def click():
        orig_click(); page.fullscreen = True
    toggle.click = click
    assert bot.expand_meeting() is True
    assert toggle.clicks == 1
    assert any("на весь экран" in ln for ln in bot.log)


def test_разворот_окна_по_кнопке_в_углу_без_подписи():
    """Подписи у кнопки может не быть — тогда берётся кнопка-иконка в левом
    верхнем углу ОКНА ВСТРЕЧИ (координаты со сдвигом на положение iframe)."""
    corner = _Btn("", box={"x": 560, "y": 40, "width": 40, "height": 40})
    far = _Btn("", box={"x": 900, "y": 40, "width": 40, "height": 40})
    child = _Frame("child", {"corner": corner, "far": far},
                   box={"x": 536, "y": 12, "width": 1112, "height": 915})
    page = _FramedPage({}, child)
    bot = _bot(page)
    orig = corner.click
    def click():
        orig(); page.fullscreen = True
    corner.click = click
    assert bot.expand_meeting() is True
    assert corner.clicks == 1 and far.clicks == 0


def test_без_оболочки_разворот_молчит():
    """Старая вёрстка (гость без Мессенджера): кнопки нет, класса нет —
    это не ошибка, и предупреждения в карточке быть не должно."""
    page = _FramedPage({}, _Frame("child", {}))
    bot = _bot(page)
    assert bot.expand_meeting() is False
    assert not any("⚠" in ln for ln in bot.log)


def test_html_сохраняется_по_каждому_фрейму(tmp_path):
    child = _Frame("https://telemost.yandex.ru/j/1?embedded", {}, content="<b>meet</b>")
    page = _FramedPage({}, child)
    page._content = "<html>shell</html>"
    bot = _bot(page)
    out = tmp_path / "rec.join-failed.html"
    bot.dump_html(str(out))
    assert out.read_text(encoding="utf-8") == "<html>shell</html>"
    fr = tmp_path / "rec.join-failed.frame1.html"
    assert "meet" in fr.read_text(encoding="utf-8") and "embedded" in fr.read_text(encoding="utf-8")


def test_поле_поиска_мессенджера_не_принимается_за_поле_имени():
    """В оболочке Мессенджера единственное текстовое поле — поиск по чатам
    (placeholder «Поиск», type=text). Прежний общий селектор input[type=text]
    вписывал туда имя бота — «Указал имя: Протокол-бот» в строку поиска."""
    from app.automation.recorder.browser import _NAME_INPUTS
    search = _Btn("", aria="Поиск", ph="Поиск")
    page = _FramedPage({'input[type="text"]': search, 'input[aria-label="Поиск"]': search},
                       _Frame("child", {}))
    bot = _bot(page)
    assert not bot._fill_any(_NAME_INPUTS, "Протокол-бот", overall_ms=300)
    assert not hasattr(search, "value")
    # а настоящее поле имени — находится
    name = _Btn("", ph="Ваше имя")
    page2 = _FramedPage({}, _Frame("child", {'input[placeholder*="мя"]': name}))
    assert _bot(page2)._fill_any(_NAME_INPUTS, "Протокол-бот", overall_ms=300)
    assert name.value == "Протокол-бот"


# ---------------------------------------------------------------------------
# 21.09.2026, второй заход: скриншот с карточки показал промо-окно «Большое
# обновление в Телемосте» поверх оболочки — окно встречи за ним не открывалось.
# ---------------------------------------------------------------------------
def test_промо_окно_закрывается_кнопкой_подтверждения():
    ok = _Btn("Звучит отлично")
    page = _FramedPage({'button:has-text("Звучит отлично")': ok}, _Frame("child", {}))
    bot = _bot(page)
    assert bot._dismiss_popups() is True
    assert ok.clicks == 1
    assert any("Закрыл всплывающее окно" in ln and "Звучит отлично" in ln for ln in bot.log)


def test_крестик_закрывает_диалог_оболочки_но_не_окно_встречи():
    """У прелобби встречи (во фрейме) свой крестик «Закрыть» — он закрывает
    саму встречу. Его трогать нельзя; крестик диалога в главном документе — можно."""
    meeting_x = _Btn("", aria="Закрыть")
    child = _Frame("child", {'button[aria-label="Закрыть"]': meeting_x,
                             '[role="dialog"] button[aria-label="Закрыть"]': meeting_x})
    page = _FramedPage({}, child)
    bot = _bot(page)
    assert bot._dismiss_popups() is False
    assert meeting_x.clicks == 0
    dialog_x = _Btn("", aria="Закрыть")
    page2 = _FramedPage({'[role="dialog"] button[aria-label="Закрыть"]': dialog_x}, child)
    bot2 = _bot(page2)
    assert bot2._dismiss_popups() is True
    assert dialog_x.clicks == 1 and meeting_x.clicks == 0


def test_без_всплывающих_окон_ничего_не_нажимается():
    join = _Btn("Подключиться")
    page = _FramedPage({}, _Frame("child", {'button:has-text("Подключиться")': join}))
    bot = _bot(page)
    assert bot._dismiss_popups() is False
    assert join.clicks == 0 and bot.log == []
