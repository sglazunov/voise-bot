"""Экономия на входе: кэш неизменной части промпта и окно перегенерации темы.

Разбор 14.09.2026 показал, где сгорают токены: проверка цитат несёт всю
расшифровку в каждом батче, шапка промпта карты пересчитывается на каждом
куске, а `regen_topic_details` при бюджете 210 000 символов отправляет
расшифровку целиком — до шести раз за прогон.
"""
from __future__ import annotations

import app.analyze as analyze
from app.analyze_prompts import _MAP_TEMPLATE
from app.llm_base import CACHE_MARK, cache_parts, cache_strip


class _Backend:
    """Движок без известного TPM — как Anthropic."""
    name = "anthropic"


class TestCacheParts:
    def test_склейка_кусков_равна_промпту_без_меток(self):
        p = "A" * 4000 + CACHE_MARK + "B" * 4000 + CACHE_MARK + "C" * 100
        assert "".join(t for t, _ in cache_parts(p)) == cache_strip(p)

    def test_последний_кусок_никогда_не_кэшируется(self):
        p = "A" * 4000 + CACHE_MARK + "хвост"
        assert cache_parts(p)[-1][1] is False

    def test_короткая_голова_не_тратит_границу(self):
        # Меньше минимального префикса Anthropic кэш всё равно не возьмёт.
        assert cache_parts("мало" + CACHE_MARK + "хвост") == [("малохвост", False)]

    def test_без_меток_промпт_не_меняется(self):
        assert cache_parts("обычный промпт") == [("обычный промпт", False)]


class TestMapPrompt:
    def _prompt(self, i: int, chunk: str) -> str:
        return _MAP_TEMPLATE.format(i=i, n=5, chunk=chunk,
                                    context="КОНТЕКСТ КОМАНДЫ " * 200)

    def test_шапка_одинакова_у_всех_кусков(self):
        a, b = cache_parts(self._prompt(1, "речь " * 500)), \
               cache_parts(self._prompt(2, "другое " * 500))
        assert a[0][0] == b[0][0] and a[0][1] is True, "шапка обязана кэшироваться"
        assert a[1][0] == b[1][0] and a[1][1] is True, "контекст встречи — тоже"
        assert a[-1][0] != b[-1][0], "сам кусок переменный"

    def test_номер_куска_ушёл_из_шапки_но_остался_в_промпте(self):
        # Ради двух цифр в шапке пересчитывались все правила на каждом куске.
        clean = cache_strip(self._prompt(3, "речь"))
        assert "Это часть 3 из 5" in clean
        assert "часть 3" not in cache_parts(self._prompt(3, "речь"))[0][0]

    def test_метка_не_доходит_до_модели(self):
        assert CACHE_MARK not in cache_strip(self._prompt(1, "речь"))


class TestVerifyBudget:
    def test_батч_крупнее_прежних_пятнадцати(self):
        # Каждый батч несёт копию всей расшифровки — их число это прямо деньги.
        assert analyze._VERIFY_BATCH >= 30

    def test_потолок_ответа_растёт_вместе_с_батчем(self):
        # Корень К2 был в ПОТОЛКЕ ответа: батч без него вернёт обрезанный JSON.
        assert analyze._verify_max_tokens(analyze._VERIFY_BATCH) >= \
               150 * analyze._VERIFY_BATCH
        assert analyze._verify_max_tokens(5) < analyze._verify_max_tokens(40)


class TestRegenWindow:
    def test_окно_ограничено_и_реально_режет_расшифровку(self):
        w = analyze._regen_window(_Backend())
        assert w <= analyze._REGEN_WINDOW_CHARS
        # Часовая встреча — около 40 000 символов: окно обязано её урезать.
        assert w < 40000, "иначе окно вокруг темы не срабатывает никогда"

    def test_окно_берётся_вокруг_темы_а_не_с_начала(self):
        text = ("начало " * 3000) + "УНИКАЛЬНОЕСЛОВО про тарифы " + ("конец " * 3000)
        out = analyze._window_for_topic(
            text, {"topic": "УНИКАЛЬНОЕСЛОВО", "details": "тарифы"}, 2000)
        assert "УНИКАЛЬНОЕСЛОВО" in out and len(out) <= 2000


class TestGeminiImplicitCache:
    """У Gemini кэш НЕЯВНЫЙ: включён сам, срабатывает по совпадающему НАЧАЛУ
    запроса. Значит польза от порядка блоков в шаблоне карты есть и без
    какой-либо поддержки в коде — но только пока начало кусков совпадает."""

    def _clean(self, i: int, chunk: str) -> str:
        return cache_strip(_MAP_TEMPLATE.format(
            i=i, n=6, chunk=chunk,
            context="=== ПОСТОЯННЫЙ КОНТЕКСТ ===\nпроект, роли\n\n"))

    def test_у_кусков_одинаковое_начало(self):
        a, b = self._clean(1, "речь " * 500), self._clean(2, "другое " * 500)
        common = 0
        for x, y in zip(a, b):
            if x != y:
                break
            common += 1
        # Нижний порог неявного кэша — около 1024 токенов; 2500 символов
        # русского текста примерно столько и есть.
        assert common >= 2500, (
            f"общее начало всего {common} символов — неявный кэш не возьмётся")
        assert a[common:].startswith("1 из 6"), "расходиться должен номер куска"


class TestUsageLog:
    def test_строка_расхода_считает_долю_кэша(self, caplog):
        from app.llm import _log_usage
        with caplog.at_level("INFO"):
            _log_usage("gemini/x", {"promptTokenCount": 1000,
                                    "cachedContentTokenCount": 250,
                                    "candidatesTokenCount": 400})
        assert "из кэша 250, 25%" in caplog.text

    def test_пустой_расход_не_пишется(self, caplog):
        from app.llm import _log_usage
        with caplog.at_level("INFO"):
            _log_usage("gemini/x", None)
            _log_usage("gemini/x", {})
        assert caplog.text == ""
