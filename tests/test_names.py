"""Общие правила имён (app/names.py) — на дословных строках с боевых записей.

Почему модуль вообще появился: правила жили в jobs.py и применялись только к
списку участников, а speaker_id чистил подписи по-своему. В протоколе CRM от
28.07 это выглядело так: в разделе «Участники» — «Зоя Р», а в метках спикеров и
в тексте — «ЗояР». Теперь путь один, и тесты проверяют ОБА конца.
"""
from app import names
from app.speaker_id import _clean_line

# Список участников из протокола 28.07 — люди вперемешку с интерфейсом.
ROSTER = ["Александр Самсонов", "Ш Демонстрация", "Наталья П", "Зоя Р",
          "Client", "Russian", "Волонтёр", "Название", "ENG",
          "Спросить Алису А", "Люди"]
# Метки спикеров из той же расшифровки.
SPEAKERS = ["rear TL", "Е рр aa", "ЗояР", "ЗояР. 11 Сергей Глазунов",
            "Наталья П", "Сергей Глазунов", "константин П"]


class TestRoster:
    def test_остаются_только_люди(self):
        kept = [names.normalise(n) for n in ROSTER if names.looks_like_name(n)]
        assert kept == ["Александр Самсонов", "Наталья П", "Зоя Р"]

    def test_интерфейс_отсеивается_даже_с_приставкой(self):
        """«Ш Демонстрация» — та же кнопка, что и «Демонстрация»."""
        assert not names.looks_like_name("Ш Демонстрация")
        assert not names.looks_like_name("Демонстрация")


class TestSpeakerLabels:
    """Тот же результат должен получаться и на пути speaker_id."""

    def test_слипшийся_инициал_чинится(self):
        assert _clean_line("ЗояР") == "Зоя Р"

    def test_потерянная_заглавная_не_теряет_человека(self):
        """OCR иногда пишет имя со строчной — это человек, а не мусор."""
        assert _clean_line("константин П") == "Константин П"

    def test_осколки_ocr_отсеиваются(self):
        for junk in ("rear TL", "Е рр aa", "ЗояР. 11 Сергей Глазунов"):
            assert _clean_line(junk) == "", junk

    def test_нормальные_метки_не_портятся(self):
        assert _clean_line("Сергей Глазунов") == "Сергей Глазунов"
        assert _clean_line("Наталья П") == "Наталья П"


class TestОдинаковоНаОбоихПутях:
    def test_список_и_метки_дают_одно_написание(self):
        """Ради этого модуль и вынесен: «Зоя Р» в списке и «ЗояР» в тексте —
        это был один человек, показанный двумя способами."""
        for raw in ("ЗояР", "Зоя Р", "Зоя P"):
            assert names.normalise(raw) == "Зоя Р"
            assert _clean_line(raw) == "Зоя Р"


class TestНеСломать:
    def test_латинская_фамилия_цела(self):
        """Сводить латиницу к кириллице подряд нельзя."""
        assert names.normalise("Сергей Beck") == "Сергей Beck"
        assert names.looks_like_name("Сергей Beck")

    def test_обычные_имена(self):
        for n in ("НВ Светлана", "Дарья К", "Елизавета", "Андрей Журавль"):
            assert names.looks_like_name(n), n
            assert names.normalise(n) == n

    def test_глагол_не_имя(self):
        assert not names.looks_like_name("Развлекаешься")

    def test_ключ_дедупликации_склеивает_варианты(self):
        assert names.key("ЗояР") == names.key("Зоя P") == names.key("зоя р")


class TestМусорИзПротоколов31_07:
    """Что реально попало в «Участники» боевых протоколов за 31.07 — то есть
    уже ПОСЛЕ отсева фраз с экрана вроде «Определяется Как Происходит».
    Остались два класса: реплики вежливости и надписи капслоком."""

    def test_вежливость_не_участник(self):
        for w in ("Спасибо", "Пожалуйста", "Хорошо", "Понятно", "Алло"):
            assert not names.looks_like_name(w), w

    def test_надпись_капслоком_не_участник(self):
        for w in ("CBASU", "RIDES", "ЗАДАНИЕ", "РЕАСН", "BOHOK", "РЕШЕНИЕ"):
            assert not names.looks_like_name(w), w

    def test_аббревиатура_рядом_с_именем_не_страдает(self):
        """Отсев капслока — только для ОДИНОЧНОГО слова: «HR Светлана» и
        «НВ Светлана» — живые подписи, их терять нельзя."""
        for n in ("HR Светлана", "НВ Светлана", "Сергей Beck"):
            assert names.looks_like_name(n), n

    def test_интерфейс_браузера_не_участник(self):
        """Протоколы 07–10.08: при демонстрации экрана в список участников
        приезжала строка меню браузера."""
        for w in ("Chrome Файл", "Q Поиск", "JavaScript. Профе"):
            assert not names.looks_like_name(w), w

    def test_имя_капслоком_из_двух_слов_остаётся(self):
        """«КИРИЛЛ БУБНОВ» — живой человек, который так подписался. Правило
        про капслок нарочно бьёт только по ОДИНОЧНОМУ слову."""
        assert names.looks_like_name("КИРИЛЛ БУБНОВ")

    def test_короткие_инициалы_целы(self):
        """«Зоя Р» и «АС» короче четырёх букв — под правило не попадают."""
        assert names.looks_like_name("Зоя Р")


class TestУчастникиИзПротокола29_07:
    """Список участников встречи ЭМО 29.07 — что реально пришло из OCR."""

    RAW = ["Иван Ю", "Шавлак Павел", "Павел", "Групповой Звонок Завершился",
           "Анастасия Фомичева", "Мария Н", "НВ Светлана", "Зоя Р",
           "Weenies Sad", "Елизавета", "Hireeree Том", "Сергей Глазунов"]

    def test_системное_сообщение_телемоста_не_участник(self):
        assert not names.looks_like_name("Групповой Звонок Завершился")

    def test_один_человек_не_попадает_дважды(self):
        """Телемост подписывает плитку то «Павел», то «Шавлак Павел»."""
        from app.speaker_id import _merge_short_names
        kept = [n for n in self.RAW if names.looks_like_name(n)]
        merged = _merge_short_names(kept)
        assert "Шавлак Павел" in merged
        assert "Павел" not in merged
        assert sum(1 for m in merged if "Павел" in m) == 1

    def test_склейка_не_трогает_разных_людей(self):
        from app.speaker_id import _merge_short_names
        out = _merge_short_names(["Мария Н", "Иван Ю", "Сергей Глазунов"])
        assert len(out) == 3

    def test_порядок_появления_сохраняется(self):
        from app.speaker_id import _merge_short_names
        out = _merge_short_names(["Иван Ю", "Павел", "Шавлак Павел"])
        assert out == ["Иван Ю", "Шавлак Павел"]


class TestУчастникиИзПротокола10_08:
    """Список из боевого протокола часовой встречи с демонстрацией экрана.
    Плитка Телемоста режет подписи по ширине, и рядом с людьми оказались
    обрезки их же имён и надписи из соседних окон."""

    RAW = ['Виктор Мухин', 'Мария Н', 'Дарья К', 'Виктор Коробов', 'Елизавета',
           'Hite', 'Я Сергей Глазунов', 'Мельников Алексей', 'Сергей Беск',
           'Мухаммад Г', 'Mapua H', 'Х Серге', 'Mar', 'Гла', 'Cron', 'Telegr',
           'Кофе-брейк', 'КИРИЛЛ БУБНОВ']

    def test_надписи_окон_и_распорядка_отсеяны(self):
        for w in ("Cron", "Telegr", "Кофе-брейк"):
            assert not names.looks_like_name(w), w

    def test_обрезки_имён_склеиваются(self):
        """«Гла» при наличии «Сергей Глазунов» — не человек."""
        from app.speaker_id import _merge_short_names
        kept = _merge_short_names([n for n in self.RAW if names.looks_like_name(n)])
        for frag in ("Гла", "Mar", "Х Серге"):
            assert frag not in kept, frag

    def test_живые_люди_остались(self):
        from app.speaker_id import _merge_short_names
        kept = _merge_short_names([n for n in self.RAW if names.looks_like_name(n)])
        for real in ("Виктор Мухин", "Мария Н", "Мельников Алексей",
                     "КИРИЛЛ БУБНОВ", "Елизавета"):
            assert real in kept, real


class TestCanonical:
    """Искажённые OCR подписи → известные имена из контекста/карточки серии."""
    KNOWN = ["Кирилл Бубнов", "Мария Н", "Зоя Р", "Сергей Beck"]

    def test_glued_prefix_and_caps(self):
        from app import names
        assert names.canonical("ЗЖКИРИЛЛ БУБНОВ", self.KNOWN) == "Кирилл Бубнов"
        assert names.canonical("КИРИЛЛ БУБНОВ", []) == "Кирилл Бубнов"

    def test_latin_lookalike_caption(self):
        from app import names
        assert names.canonical("Mapua H", self.KNOWN) == "Мария Н"
        assert names.canonical("Зоя P", self.KNOWN) == "Зоя Р"

    def test_real_latin_surname_and_unknown_kept(self):
        from app import names
        assert names.canonical("Сергей Beck", self.KNOWN) == "Сергей Beck"
        assert names.canonical("Алсу Хусаинова", self.KNOWN) == "Алсу Хусаинова"
