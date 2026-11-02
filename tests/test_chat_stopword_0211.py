"""02.11.2026, «ЭМО Встреча с ОО Ключевые курсы»: бот вышел только по
ВТОРОМУ «стоп». Журнал карточки: «видно 9 → 8 → 1», «появился раздел
текущего дня», «видно 2», «стало больше — останавливаю».

Первое «стоп» пришло вместе с разделом «Сегодня» (раздел появляется с
первым сообщением дня), время этого сообщения бот не прочитал — и без
времени принял команду за историю: строка дословно совпала со старыми.

Правило: если при входе бота в чате ещё не было сообщений за сегодня
(список доказанно у самого низа, последний разделитель — прошлая дата),
то всё, что позже окажется под разделом текущего дня, написано при боте, и
«стоп» там — команда, даже без читаемого времени.

Время в тестах: «сегодня» = 02.11.2026, бот вошёл в 11:00, сейчас 11:51.
"""
from __future__ import annotations

import datetime as dt

from app.automation.recorder.browser import TelemostBot


def _bot(joined="11:00", now="11:51", day=(2026, 11, 2), pinned=True):
    b = TelemostBot.__new__(TelemostBot)
    b._chat_last_peek = 0.0
    b._chat_last_n = -1
    b._chat_scopes = {}
    b._chat_reads = 0
    h, m = map(int, now.split(":"))
    b._clock = lambda: dt.datetime(*day, h, m).timestamp()
    jh, jm = map(int, joined.split(":"))
    b._chat_since_min = jh * 60 + jm
    b.log = []
    b._on_log = b.log.append
    b._pin_chat_bottom = lambda w: pinned
    return b


def _feed(b, lines):
    b._read_all_text = lambda: "\n".join(lines)
    b._chat_last_peek = 0.0
    return b.maybe_chat_stop("стоп")


SEP = TelemostBot._FRAME_SEP
# История постоянной комнаты: одни «стоп» под датами прошлых недель, время
# сообщения — отдельной строкой после текста.
HIST = ["16 октября", "Иван Р.", "стоп", "11:26",
        "23 октября", "Сергей Глазунов", "стоп", "11:52",
        "30 октября", "Зоя Р.", "стоп", "11:40"]


class TestПервыйСтопБезВремени:
    def test_первое_стоп_под_появившимся_разделом_останавливает(self):
        b = _bot()
        assert _feed(b, HIST) is False
        assert _feed(b, HIST) is False
        # Время у нового сообщения не прочиталось (вёрстка не та, что ждём).
        assert _feed(b, HIST + ["Сегодня", "Сергей Глазунов", "стоп"]) is True
        assert any("сегодняшних сообщений не было" in x for x in b.log), b.log

    def test_раздел_датой_текущего_дня(self):
        b = _bot()
        assert _feed(b, HIST) is False
        assert _feed(b, HIST + ["2 ноября", "Сергей Глазунов", "стоп"]) is True

    def test_время_в_чужом_поясе_не_мешает(self):
        """Мессенджер может показывать время в поясе аккаунта: «08:51» при
        входе в 11:00 по часам контейнера. Раздел дня появился ПОСЛЕ
        доказательства — сообщение написано при боте."""
        b = _bot()
        assert _feed(b, HIST) is False
        assert _feed(b, HIST + ["Сегодня", "Сергей Глазунов", "стоп", "08:51"]) is True

    def test_в_отдельном_фрейме(self):
        b = _bot()
        main = ["Чаты", "Олег: привет"]
        assert _feed(b, main + [SEP + "https://telemost.yandex.ru/meet#0"] + HIST) is False
        assert _feed(b, main + [SEP + "https://telemost.yandex.ru/meet#0"] + HIST
                     + ["Сегодня", "Сергей Глазунов", "стоп"]) is True


class TestБезДоказательстваНеОстанавливает:
    """Ложная остановка хуже пропуска: без доказанно пустого дня — прежнее
    поведение."""

    def test_список_не_у_низа(self):
        b = _bot(pinned=False)
        assert _feed(b, HIST) is False
        assert _feed(b, HIST + ["Сегодня", "Сергей Глазунов", "стоп"]) is False

    def test_при_входе_раздел_дня_уже_был(self):
        """Утро той же комнаты: «стоп» прошлой встречи сегодня уже лежит —
        день не пуст, доказательства нет."""
        b = _bot()
        today = HIST + ["Сегодня", "Анна", "стоп"]
        assert _feed(b, today) is False
        assert _feed(b, today) is False

    def test_при_входе_дат_не_видно(self):
        """Окно без единого разделителя: какой это день — неизвестно."""
        b = _bot()
        bare = ["Иван Р.", "стоп", "11:26", "Зоя Р.", "стоп", "11:40"]
        assert _feed(b, bare) is False
        assert _feed(b, ["Сегодня", "Сергей Глазунов", "стоп"]) is False

    def test_доказательство_одного_фрейма_не_действует_в_другом(self):
        b = _bot()
        a = SEP + "https://telemost.yandex.ru/a#0"
        c = SEP + "https://telemost.yandex.ru/c#0"
        assert _feed(b, [a] + HIST + [c, "Чат"]) is False
        assert _feed(b, [a] + HIST + [c, "Сегодня", "Анна", "стоп"]) is False

    def test_строка_сегодня_в_окне_отменяет_доказательство(self):
        """«Сегодня⏎в 11 созвон» — разбор признал строку текстом сообщения, а
        не разделителем, и окно вышло «прошлым». Если разбор ошибся, утро
        комнаты сочлось бы пустым днём — поэтому любая строка «Сегодня» в
        окне доказательство отменяет."""
        b = _bot()
        win = HIST + ["31 октября", "Иван Р.", "Сегодня", "в 11 созвон", "11:20"]
        assert _feed(b, win) is False
        assert _feed(b, win + ["Сегодня", "Сергей Глазунов", "стоп"]) is False

    def test_прокрутка_вверх_не_команда(self):
        """После доказательства прокрутка показала старую неделю — там
        прошлая дата, команды нет."""
        b = _bot()
        assert _feed(b, HIST) is False
        assert _feed(b, ["9 октября", "Анна", "стоп", "11:03"] + HIST[:4]) is False


class TestДиагностикаВремени:
    def test_строки_рядом_с_командой_без_времени_в_журнале(self):
        """Следующая вёрстка должна быть видна из журнала карточки, без
        доступа к серверу: у «стоп» под разделом дня нет времени — в журнал
        идут соседние строки."""
        b = _bot(pinned=False)
        assert _feed(b, HIST + ["Сегодня", "Сергей Глазунов", "стоп", "✓✓"]) is False
        note = [x for x in b.log if "время сообщения не нашёл" in x]
        assert len(note) == 1, b.log
        assert "Сергей Глазунов" in note[0] and "✓✓" in note[0]
        assert _feed(b, HIST + ["Сегодня", "Сергей Глазунов", "стоп", "✓✓"]) is False
        assert len([x for x in b.log if "время сообщения не нашёл" in x]) == 1
