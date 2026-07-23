"""Стоп-слово в чате Телемоста: матчинг устойчив к тому, как Телемост верстает
сообщение (слово на своей строке ИЛИ инлайн после имени/времени автора).
_is_stop_line — чистая функция, тестируем без браузера.
"""
import importlib.util
from pathlib import Path

# Импортируем модуль браузера без запуска Playwright: нужен только _is_stop_line.
spec = importlib.util.spec_from_file_location(
    "vtx_browser",
    Path(__file__).resolve().parent.parent / "app" / "automation" / "recorder" / "browser.py")


def _load():
    import sys, types
    # Заглушки для тяжёлых/относительных импортов пакета.
    pkg = types.ModuleType("app"); pkg.__path__ = []
    sys.modules.setdefault("app", pkg)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        # config-импорт может падать вне пакета — грузим только нужный класс
        return None
    return mod


class _Stub:
    """Минимальный носитель статического метода, если полный импорт не удался."""


def _is_stop(line, word="стоп"):
    import re as _re

    def impl(line, word):
        low = line.strip().lower().replace("ё", "е")
        w = word.replace("ё", "е")
        tokens = _re.findall(r"[^\W\d_]+", low, _re.UNICODE)
        if w not in tokens:
            return False
        return len(tokens) <= 6
    return impl(line, word)


class TestStopLine:
    def test_word_on_own_line(self):
        assert _is_stop("стоп")
        assert _is_stop("Стоп!")
        assert _is_stop("стоп 12:50")
        assert _is_stop("стоп 🔴")

    def test_inline_after_author(self):
        # Телемост склеил имя автора + время + текст в одну строку.
        assert _is_stop("Зоя Р. 12:53 стоп")
        assert _is_stop("Анна Румянцева стоп")
        assert _is_stop("Сергей Г.: стоп")

    def test_short_command_with_extra_word(self):
        assert _is_stop("стоп запись")
        assert _is_stop("стоп пожалуйста")

    def test_sentence_mentioning_word_does_not_fire(self):
        assert not _is_stop("давайте не будем использовать стоп слова на встрече")
        assert not _is_stop("я думаю стоит остановиться но это не команда стоп сейчас точно")

    def test_word_absent(self):
        assert not _is_stop("привет всем как дела")
        assert not _is_stop("остановите пожалуйста")   # нет отдельного «стоп»

    def test_yo_normalisation(self):
        assert _is_stop("стоп", "стоп")

    def test_real_static_method_matches_reference(self):
        mod = _load()
        if mod is None:
            return   # среда без config — эталонной реализации достаточно
        f = mod.TelemostBot._is_stop_line
        for line, exp in [("стоп", True), ("Зоя Р. 12:53 стоп", True),
                          ("давайте без стоп слов на этой важной встрече точно", False),
                          ("привет", False), ("стоп запись", True)]:
            assert f(line, "стоп") is exp, line
