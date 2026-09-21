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
    def __init__(self, text="", aria="", box=None, vanish=False, **kw):
        super().__init__(text, aria, **kw)
        self.clicks = 0
        self._box = box
        self._vanish = vanish      # промо-кнопка: после клика исчезает

    def click(self, **kw):
        self.clicks += 1
        if self._vanish:
            self._v = False
    in_widget = False            # лежит ли кнопка в виджете звонка оболочки
    def evaluate(self, js):
        if "closest" in js:
            return self.in_widget
        self.click()
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
    class keyboard:
        pressed: list = []
        @classmethod
        def press(cls, k): cls.pressed.append(k)
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
    ok = _Btn("Звучит отлично", vanish=True)
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
    dialog_x = _Btn("", aria="Закрыть", vanish=True)
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


def test_окно_не_закрылось_кликом_пробуются_запасные_способы():
    """21.09: клик по «Звучит отлично» проходил, а окно оставалось — карточка
    трижды писала «закрыл». Теперь исчезновение проверяется, пробуются Escape,
    принудительный и JS-клик, и в лог идёт честное «не закрылось»."""
    stuck = _Btn("Звучит отлично")           # не исчезает никогда
    page = _FramedPage({'button:has-text("Звучит отлично")': stuck}, _Frame("child", {}))
    page.keyboard.pressed.clear()
    bot = _bot(page)
    assert bot._dismiss_popups() is False
    assert stuck.clicks >= 3, "обычный, принудительный и JS-клик"
    assert "Escape" in page.keyboard.pressed
    assert any("не закрылось" in ln for ln in bot.log)
    assert not any(ln.startswith("Закрыл") for ln in bot.log)


def test_кнопка_завершить_в_виджете_оболочки_не_значит_в_звонке():
    """Скриншот записи 21.09: бот 18 минут писал прелобби «Подключиться» —
    в боковой панели оболочки на стадии «Подключение» уже есть красная
    «Завершить звонок» (yamb-call-widget__end-button), и is_in_call верил ей."""
    widget_btn = _Btn("", aria="Завершить звонок"); widget_btn.in_widget = True
    page = _FramedPage({'button[aria-label*="авершить"]': widget_btn}, _Frame("child", {}))
    assert _bot(page).is_in_call() is False
    real_btn = _Btn("", aria="Завершить звонок")        # in_widget=False — панель звонка
    page2 = _FramedPage({}, _Frame("child", {'button[aria-label*="авершить"]': real_btn}))
    assert _bot(page2).is_in_call() is True
    page3 = _FramedPage({}, _Frame("child", {'button:has-text("Участники")': _Btn("Участники 2")}))
    assert _bot(page3).is_in_call() is True


def test_профиль_слота_помечается_закрытым_корректно(tmp_path):
    import json
    from app.automation.recorder.browser import _mark_clean_exit
    d = tmp_path / "Default"; d.mkdir()
    (d / "Preferences").write_text(json.dumps({"profile": {"exit_type": "Crashed", "exited_cleanly": False}, "x": 1}), encoding="utf-8")
    _mark_clean_exit(tmp_path)
    data = json.loads((d / "Preferences").read_text(encoding="utf-8"))
    assert data["profile"] == {"exit_type": "Normal", "exited_cleanly": True} and data["x"] == 1
    _mark_clean_exit(tmp_path / "нет-такого")          # нет профиля — тишина


# ---------------------------------------------------------------------------
# 21.09.2026 (вечер): вход — один цикл опроса, а не цепочка шагов с паузами.
# Раньше каждый шаг ждал СВОЙ таймаут до конца, даже когда элемента не было
# (10 с заглушка, 8 с имя, 5 с микрофон/камера, 3+4+6 с пауз) — полминуты
# до входа, начало разговора мимо записи.
# ---------------------------------------------------------------------------
class _LivePage(_FramedPage):
    """Страница, у которой кнопки появляются ПО ХОДУ: `script` — список
    «на каком опросе какой селектор появляется в дочернем фрейме»."""

    def __init__(self, script: dict[int, dict], strong_after: int | None = None):
        # tag="\0": query_selector_all отдаёт только точные совпадения — иначе
        # любая кнопка сходила бы за «Завершить звонок» в is_in_call.
        self.child = _Frame("child", {}, tag="\0")
        super().__init__({}, self.child)
        self.script, self.strong_after, self.polls = script, strong_after, 0
        self.gotos: list[str] = []

    def goto(self, url, **kw): self.gotos.append(url)

    def wait_for_timeout(self, ms):
        self.waits += 1
        self.polls += 1
        for sel, el in self.script.get(self.polls, {}).items():
            self.child._els[sel] = el
        if self.strong_after is not None and self.polls >= self.strong_after:
            self.child._els['button:has-text("Участники")'] = _Btn("Участники")


def _fake_clock(monkeypatch, page, step=1.0):
    """Секунда «проходит» за каждый опрос — тест не ждёт реального времени."""
    clock = [0.0]
    monkeypatch.setattr("app.automation.recorder.browser.time.time", lambda: clock[0])
    orig = page.wait_for_timeout
    def wait(ms):
        clock[0] += step
        orig(ms)
    page.wait_for_timeout = wait


def _joiner(page, cfg=None):
    bot = _bot(page)
    bot.cfg = cfg or {"auth_mode": "profile", "join_timeout_sec": 60}
    bot._launch = lambda: None
    bot._display = None
    return bot


def test_вход_идёт_по_мере_появления_кнопок_без_лишних_пауз():
    cont = _Btn("Продолжить в браузере", vanish=True)
    join = _Btn("Подключиться")
    mic = _Btn(aria="Выключить микрофон")
    page = _LivePage({2: {'button:has-text("Продолжить в браузере")': cont},
                      5: {'button:has-text("Подключиться")': join,
                          'button[aria-label*="икрофон"]': mic}},
                     strong_after=8)
    bot = _joiner(page)
    assert bot.join("https://telemost.yandex.ru/j/1") is True
    assert cont.clicks == 1 and join.clicks == 1 and mic.clicks >= 1
    assert page.polls < 15, f"вход занял {page.polls} опросов по 300 мс"
    assert any("Прошёл заглушку" in ln for ln in bot.log)
    assert any(ln.startswith("Бот в звонке ✓") for ln in bot.log)


def test_продолжить_на_заглушке_не_считается_входом(monkeypatch):
    """«Продолжить» есть и в списке кнопок входа: клик по заглушке не должен
    объявляться входом."""
    cont = _Btn("Продолжить", vanish=False)      # заглушка никуда не девается
    page = _LivePage({1: {'button:has-text("Продолжить")': cont}})
    _fake_clock(monkeypatch, page)
    bot = _joiner(page, {"auth_mode": "profile", "join_timeout_sec": 20})
    assert bot.join("https://telemost.yandex.ru/j/1") is False
    assert cont.clicks >= 1
    assert not any("Нажал кнопку входа" in ln for ln in bot.log)


def test_уже_в_звонке_выход_из_цикла_сразу():
    page = _LivePage({}, strong_after=0)
    page.child._els['button:has-text("Участники")'] = _Btn("Участники")
    bot = _joiner(page)
    assert bot.join("https://telemost.yandex.ru/j/1") is True
    assert page.polls == 0
    assert any("уже в звонке" in ln for ln in bot.log)


def test_гость_вводит_имя_под_аккаунтом_нет():
    inp = _Btn(); join = _Btn("Подключиться")
    page = _LivePage({1: {'input[name="name"]': inp, 'button:has-text("Подключиться")': join}},
                     strong_after=3)
    bot = _joiner(page, {"auth_mode": "guest", "bot_join_name": "Бот", "join_timeout_sec": 10})
    assert bot.join("u") is True
    assert inp.value == "Бот"
    inp2 = _Btn()
    page = _LivePage({1: {'input[name="name"]': inp2, 'button:has-text("Подключиться")': _Btn("Подключиться")}},
                     strong_after=3)
    bot = _joiner(page)
    bot.join("u")
    assert not hasattr(inp2, "value")


def test_без_окна_встречи_одна_перезагрузка(monkeypatch):
    page = _LivePage({})
    page.frames = [page]                          # фреймов нет — оболочка пустая
    _fake_clock(monkeypatch, page)
    bot = _joiner(page, {"auth_mode": "profile", "join_timeout_sec": 40})
    assert bot.join("https://telemost.yandex.ru/j/1") is False
    assert page.gotos.count("https://telemost.yandex.ru/j/1") == 2, "ровно одна перезагрузка"
    assert any("перезагружаю" in ln for ln in bot.log)


def test_попытка_открыть_приложение_жмёт_escape_на_x(monkeypatch):
    """Диалог «Open xdg-open?» — отдельное окно Chromium, CDP до него не
    достаёт. Escape уходит через XTest на дисплей слота."""
    import app.automation.recorder.browser as b
    pressed = []
    monkeypatch.setattr(b, "_xtest_key", lambda d, k: pressed.append((d, k)) or True)
    page = _LivePage({})
    bot = _joiner(page)
    bot._display = ":99"
    bot._x_press_escape(delays=(0, 0))
    import time as _t
    for _ in range(50):
        if len(pressed) == 2:
            break
        _t.sleep(0.02)
    assert pressed == [(":99", 0xFF1B), (":99", 0xFF1B)]


class _LinkForm:
    """Окно «Номер звонка или ссылка на него»: заголовок → контейнер → поле + кнопка."""
    def __init__(self):
        self.inp = _Btn(); self.inp.pressed = []
        self.inp.press = lambda k: self.inp.pressed.append(k)
        self.btn = _Btn("Подключиться")
        self.gone = False
    # заголовок
    def is_visible(self): return not self.gone
    def evaluate_handle(self, js): return self
    def as_element(self): return self
    def query_selector(self, sel):
        return self.inp if sel == "input" else self.btn


def test_окно_ссылка_на_звонок_заполняется_и_не_считается_входом():
    """Кадр записи 21.09: оболочка открыла главную с окном «Номер звонка или
    ссылка на него», а кнопка «Подключиться» в нём совпадает с кнопкой входа —
    бот нажимал её с пустым полем и «входил» в никуда."""
    form = _LinkForm()
    page = _LivePage({}, strong_after=6)
    page.child._els["text=Номер звонка или ссылка"] = form
    page.child._els['button:has-text("Подключиться")'] = form.btn
    url = "https://telemost.yandex.ru/j/777"
    def click(**kw):
        form.btn.clicks += 1
        if form.inp.value == url:
            form.gone = True
            del page.child._els['button:has-text("Подключиться")']
    form.btn.click = click
    bot = _joiner(page)
    assert bot.join(url) is True
    assert form.inp.value == url and form.btn.clicks == 1
    assert any("спросила ссылку" in ln for ln in bot.log)
    assert not any("Нажал кнопку входа" in ln for ln in bot.log)
