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


class TestРегистрИПрокрутка:
    """Жалоба «срабатывает только с большой буквы». Само сопоставление регистр
    снимает — строка и слово приводятся к нижнему. Ненадёжен был МЕХАНИЗМ:
    он считал количество совпадений и реагировал на рост. Чат в комнате общий
    и не чистится, а Телемост держит в DOM только видимую часть списка, поэтому
    при прокрутке счётчик скачет в обе стороны и рост нового сообщения теряется.
    """

    def _bot(self):
        from app.automation.recorder.browser import TelemostBot
        b = TelemostBot.__new__(TelemostBot)
        b._chat_last_peek = 0.0
        b._chat_last_n = -1
        b._chat_scopes = {}
        b._chat_reads = 0
        # Правило «время позже начала слежки» выключено (23:59) — эти тесты
        # проверяют остальные признаки; своё правило — TestВремяСообщения.
        b._chat_since_min = 23 * 60 + 59
        b._on_log = lambda *a: None
        return b

    def _feed(self, bot, lines):
        """Подсунуть боту текст страницы вместо реального чтения чата."""
        bot._read_all_text = lambda: "\n".join(lines)
        bot._chat_last_peek = 0.0          # снять задержку между чтениями
        return bot.maybe_chat_stop("стоп")

    def test_регистр_не_имеет_значения(self):
        for written in ("стоп", "Стоп", "СТОП"):
            b = self._bot()
            assert self._feed(b, ["Привет 10:00"]) is False       # базовый снимок
            assert self._feed(b, ["Привет 10:00", f"{written} 10:05"]) is True, written

    def test_старые_команды_в_истории_не_срабатывают(self):
        """В комнате уже лежат «стоп» с прошлых встреч — это не команда сейчас."""
        b = self._bot()
        assert self._feed(b, ["стоп 12:08", "стоп 17:42"]) is False

    def test_новое_сообщение_после_прокрутки_ловится(self):
        """Старая строка ушла из DOM, новая пришла — количество не изменилось,
        и прежний счётчик такое пропускал."""
        b = self._bot()
        self._feed(b, ["стоп 12:08", "стоп 17:42"])          # базовый снимок
        assert self._feed(b, ["стоп 17:42", "стоп 18:30"]) is True

    def test_повторное_чтение_того_же_чата_не_срабатывает(self):
        b = self._bot()
        self._feed(b, ["стоп 12:08"])
        assert self._feed(b, ["стоп 12:08"]) is False
        assert self._feed(b, ["стоп 12:08"]) is False


class TestИсторияЧатаИВиртуализация:
    """29.09.2026: бот вышел «по стоп-слову», которого никто не писал. В логе
    «видно 13 → 12 → 13»: виртуализированный список дорисовывал старые строки,
    базовая линия опускалась до 12, возврат к 13 выглядел ростом."""

    _bot = TestРегистрИПрокрутка._bot
    _feed = TestРегистрИПрокрутка._feed

    HISTORY = ["14 июля", "Ирина Бобылева", "Коллеги, вынуждена отключится 11:00",
               "21 июля", "стоп 11:05", "18 августа", "Зоя Р.", "стоп 11:00",
               "8 сентября", "Стоп 11:14", "стоп 11:14", "15 сентября", "стоп 11:00",
               "Сегодня", "Иван Р.", "Проработка, два этапа 10:32"]

    def test_колебание_счётчика_при_прокрутке_не_срабатывает(self):
        b = self._bot()
        assert self._feed(b, self.HISTORY) is False
        fewer = [ln for ln in self.HISTORY if ln != "стоп 11:05"]   # строка ушла из DOM
        assert self._feed(b, fewer) is False
        assert self._feed(b, self.HISTORY) is False, "вернулась старая строка — не команда"

    def test_старая_строка_дорисованная_прокруткой_не_срабатывает(self):
        b = self._bot()
        assert self._feed(b, self.HISTORY[5:]) is False           # начало истории не в DOM
        assert self._feed(b, self.HISTORY) is False, "дорисовались строки прошлых недель"

    def test_команда_под_сегодня_срабатывает(self):
        b = self._bot()
        assert self._feed(b, self.HISTORY) is False
        assert self._feed(b, self.HISTORY + ["Сергей Глазунов", "стоп 10:40"]) is True

    def test_без_разделителя_сегодня_работает_как_раньше(self):
        b = self._bot()
        assert self._feed(b, ["стоп 12:08"]) is False
        assert self._feed(b, ["стоп 12:08", "стоп 12:30"]) is True


class TestРазделСегодняПоявилсяПоХоду:
    """20.10.2026, «CRM — проектная встреча»: в логе «видно 10 → 0 → 1», бот
    видит «стоп» и сидит дальше. На первом чтении разделителя «Сегодня» не было
    (10 команд из истории), потом он появился, и новое «стоп» под ним (1)
    сравнивалось с теми же 10. Строки без времени («стоп»), поэтому и проверка
    по тексту не помогала: новое совпадало с историческими дословно."""

    _bot = TestРегистрИПрокрутка._bot
    _feed = TestРегистрИПрокрутка._feed

    HISTORY = ["21 июля", "Зоя Р.", "стоп", "18 августа", "Иван Р.", "стоп",
               "8 сентября", "Сергей Глазунов", "стоп", "стоп"]

    def test_стоп_под_появившимся_сегодня_срабатывает(self):
        b = self._bot()
        assert self._feed(b, self.HISTORY) is False                  # 4 из истории
        today = self.HISTORY + ["Сегодня", "Звонок начался"]
        assert self._feed(b, today) is False                         # «видно 0»
        assert self._feed(b, today + ["Сергей Глазунов", "стоп"]) is True

    def test_раздел_появился_вместе_с_командой(self):
        b = self._bot()
        hist = ["стоп 11:05", "стоп 11:14"]
        assert self._feed(b, hist) is False
        assert self._feed(b, hist + ["Сегодня", "стоп 11:42"]) is True

    def test_утренние_команды_под_сегодня_не_срабатывают(self):
        """Постоянная комната: «стоп» с утренней встречи бот уже видел — когда
        прокрутка дорисует разделитель над ними, это не новая команда."""
        b = self._bot()
        assert self._feed(b, ["стоп 10:58", "Иван Р.", "привет 11:30"]) is False
        assert self._feed(b, ["Сегодня", "стоп 10:58", "Иван Р.",
                              "привет 11:30"]) is False

    def test_раздел_то_пропадает_то_появляется(self):
        b = self._bot()
        today = self.HISTORY + ["Сегодня", "Звонок начался"]
        self._feed(b, self.HISTORY)
        self._feed(b, today)
        assert self._feed(b, self.HISTORY) is False                  # прокрутили вверх
        assert self._feed(b, today) is False
        assert self._feed(b, today + ["стоп"]) is True


class TestОдинаковыеСтопИВремя:
    """30.10.2026, постоянная комната: вся история чата — одни «стоп» (время
    отдельной строкой), и новое «стоп» дословно совпадает с десятком старых.
    Когда раздел текущего дня появлялся ВМЕСТЕ с командой, она считалась
    старой; когда список виртуализирован («одно пришло — одно ушло»), не рос
    и счётчик. Новизну теперь даёт ВРЕМЯ сообщения: под разделом текущего дня
    «стоп», написанный не раньше входа бота, — команда, даже если текст уже
    встречался. Разделитель — «Сегодня» ИЛИ дата текущего дня."""

    _feed = TestРегистрИПрокрутка._feed

    HIST = ["Сергей Беск", "стоп", "11:39",
            "18 сентября", "Сергей Беск", "стоп", "11:18",
            "9 октября", "Сергей Беск", "Стоп", "11:12", "стоп", "11:12",
            "23 октября", "Сергей Беск", "Стоп", "11:17", "стоп", "11:18"]

    def _bot(self, joined="11:00"):
        import datetime as dt
        b = TestРегистрИПрокрутка._bot(self)
        day = dt.datetime(2026, 10, 30, *map(int, joined.split(":")))
        b._clock = lambda: day.timestamp()        # «сейчас» для бота (дата!)
        b._chat_since_min = day.hour * 60 + day.minute   # с какой минуты следим
        return b

    def test_сегодня_и_стоп_в_одном_чтении_время_отдельной_строкой(self):
        b = self._bot()
        assert self._feed(b, self.HIST) is False
        assert self._feed(b, self.HIST + ["Сегодня", "Сергей Беск", "стоп", "11:26"]) is True

    def test_дата_текущего_дня_как_разделитель(self):
        """Телемост может подписать текущий день датой, а не словом «Сегодня».
        Одно старое «стоп» ушло из DOM, новое пришло — счётчик не вырос."""
        b = self._bot()
        assert self._feed(b, self.HIST) is False
        assert self._feed(b, self.HIST[4:] + ["30 октября", "Сергей Беск", "стоп", "11:26"]) is True

    def test_одно_пришло_одно_ушло(self):
        """Список виртуализирован: утреннее «стоп» ушло из DOM, новое пришло —
        текст и счётчик те же, отличает только время."""
        b = self._bot()
        assert self._feed(b, ["Сегодня", "Сергей Беск", "стоп", "10:40"]) is False
        assert self._feed(b, ["Сегодня", "Сергей Беск", "стоп", "11:26"]) is True

    def test_утренний_стоп_под_сегодня_не_срабатывает(self):
        b = self._bot()
        assert self._feed(b, self.HIST) is False
        assert self._feed(b, self.HIST + ["Сегодня", "Сергей Беск", "стоп", "10:40"]) is False
        assert self._feed(b, self.HIST + ["Сегодня", "Сергей Беск", "стоп", "10:40"]) is False

    def test_время_в_той_же_строке(self):
        b = self._bot()
        assert self._feed(b, self.HIST) is False
        assert self._feed(b, self.HIST + ["Сегодня", "Сергей Беск стоп 11:26"]) is True

    def test_без_разделителя_дня_время_не_верится(self):
        """Старая вёрстка без дат: «стоп 11:39» из истории неотличимо от
        сегодняшнего — правило по времени действует только под разделом дня."""
        b = self._bot()
        assert self._feed(b, ["Сергей Беск", "стоп", "11:39"]) is False
        assert self._feed(b, ["Сергей Беск", "стоп", "11:39"]) is False

    def test_первое_чтение_по_тексту_не_команда(self):
        """На первом чтении всё, что лежит в чате, — история; командой может
        быть только «стоп» со временем позже начала слежки (29.10)."""
        b = self._bot()
        assert self._feed(b, ["Сегодня", "Сергей Беск", "стоп", "10:40"]) is False
        b = self._bot()
        assert self._feed(b, ["Сергей Беск", "стоп", "11:26"]) is False   # без раздела дня
        b = self._bot()
        assert self._feed(b, ["Сегодня", "Сергей Беск", "стоп", "11:26"]) is True

    def test_в_логе_причина_новизны(self):
        b = self._bot(); logs = []; b._on_log = logs.append
        self._feed(b, self.HIST)
        self._feed(b, self.HIST + ["Сегодня", "Сергей Беск", "стоп", "11:26"])
        assert any("11:26" in m and "останавливаю" in m for m in logs), logs


class TestВремяСообщения:
    """29.10.2026, «Операционная встреча»: «видно 14 → 13 → 1», «появился
    раздел «Сегодня»», а «стоп 11:54» под ним бот не принял. Раздел «Сегодня»
    появляется в чате вместе с ПЕРВЫМ сообщением дня — то есть с самой
    командой, а строка «стоп» дословно совпадала с историческими, и правило
    «виденное раньше — старое» её глушило."""

    _feed = TestРегистрИПрокрутка._feed

    def _bot(self, since="11:00"):
        b = TestРегистрИПрокрутка._bot(self)
        h, m = since.split(":")
        b._chat_since_min = int(h) * 60 + int(m)
        return b

    HISTORY = ["21 июля", "Зоя Р.", "стоп", "11:05", "15 сентября", "Иван Р.",
               "стоп", "11:42"]

    def test_боевой_случай_29_10(self):
        b = self._bot(since="11:01")
        assert self._feed(b, self.HISTORY) is False
        assert self._feed(b, self.HISTORY[2:]) is False            # прокрутка
        assert self._feed(b, self.HISTORY + ["Сегодня", "стоп", "11:54"]) is True

    def test_время_в_той_же_строке(self):
        b = self._bot(since="11:01")
        self._feed(b, ["стоп 11:05"])
        assert self._feed(b, ["стоп 11:05", "Сегодня", "стоп ✓✓ 11:54"]) is True

    def test_утренняя_команда_под_сегодня_не_срабатывает(self):
        """Постоянная комната: «стоп» с утренней встречи стоит под «Сегодня»
        со временем РАНЬШЕ входа бота."""
        b = self._bot(since="11:01")
        assert self._feed(b, ["Сегодня", "стоп", "10:58"]) is False
        assert self._feed(b, ["Сегодня", "стоп", "10:58", "Иван", "ок", "11:20"]) is False

    def test_минута_входа_не_считается(self):
        b = self._bot(since="11:01")
        assert self._feed(b, ["Сегодня", "стоп", "11:01"]) is False

    def test_в_истории_без_сегодня_время_не_решает(self):
        """Без разделителя время не говорит, какой это день: «стоп 17:42»
        вчерашний."""
        b = self._bot(since="11:01")
        assert self._feed(b, ["стоп 17:42"]) is False
        assert self._feed(b, ["стоп 17:42", "привет 18:00"]) is False

    def test_чужое_время_следующей_строки_не_берётся(self):
        b = self._bot(since="11:01")
        assert self._feed(b, ["Сегодня", "стоп", "Иван Р. 11:30"]) is False

    def test_одна_команда_срабатывает_один_раз(self):
        b = self._bot(since="11:01")
        lines = ["Сегодня", "стоп", "11:54"]
        assert self._feed(b, lines) is True
        assert self._feed(b, lines) is False
