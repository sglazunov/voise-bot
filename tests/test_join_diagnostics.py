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
