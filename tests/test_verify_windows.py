"""Оконный режим проверки цитат (`VTX_VERIFY_WINDOWS`, фаза 2 п. 6 плана RAG).

Каждый батч проверки нёс ВЕСЬ фрагмент расшифровки (3,7× текста встречи,
31–57 % входа). В оконном режиме модель видит только окна вокруг строк, где
лежат якорные слова пунктов батча; всё, что идёт ПОСЛЕ ответа модели —
сверка цитаты, уместность, восстановление времени — работает по полному
фрагменту, как и раньше. Флаг выключен по умолчанию: эффект на качество
(`facts_coverage` в tests/eval) без живого ключа не измерить.

Фикстуры — в боевом формате `formats.to_txt`: метка «[чч:мм:сс]» стоит в
шапке блока говорящего, реплики идут голыми строками.
"""
from __future__ import annotations

import json
import random
import time

from app import analyze
from app.analyze import _verify_windows, verify_protocol


class FakeBackend:
    name = "fake"

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def complete(self, prompt, max_tokens=2000, force_json=True, **kw):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError("больше ответов не заготовлено")
        return self.answers.pop(0)


def _ev(items):
    return json.dumps({"items": items}, ensure_ascii=False)


# Слова-наполнитель: ни одно не делит основу с пунктами протокола ниже
# (`_same_stem` сравнивает и по префиксу от четырёх букв — «задание» сошло
# бы за «задачу», поэтому таких слов здесь нет).
_FILLER = ("погода отпуск море горы билеты поезд гостиница ужин кофе музыка "
           "кино книга выставка велосипед парк собака кошка пирог чай сахар "
           "лимон яблоко груша вишня дорога трамвай автобус метро окно дверь "
           "лампа стол стул диван ковёр шторы").split()
_NAMES = ("Зоя Р", "Кирилл Бубнов", "Мария Петрова", "Иван С")

# Строки с основаниями пунктов — по одной на блок 10, 30 и 50 из 60.
ANCHOR_ZOYA = "Надо обновить сервер и закрыть задачу по конструктору."
ANCHOR_KIRILL = "Я возьму ревизию фильтров каталога до пятницы."
ANCHOR_MARIA = "Решили — оставляем базовые фильтры в каталоге."
ANCHORS = {10: ("Зоя Р", ANCHOR_ZOYA), 30: ("Кирилл Бубнов", ANCHOR_KIRILL),
           50: ("Мария Петрова", ANCHOR_MARIA)}


def _build(n_blocks: int = 60, seed: int = 3) -> str:
    rnd = random.Random(seed)
    out = []
    for b in range(n_blocks):
        sec = 15 + b * 55
        name = ANCHORS[b][0] if b in ANCHORS else rnd.choice(_NAMES)
        out.append(f"[{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}] {name}:")
        for k in range(3):
            if b in ANCHORS and k == 1:
                out.append(ANCHORS[b][1])
            else:
                out.append(" ".join(rnd.choice(_FILLER) for _ in range(rnd.randint(5, 11))) + ".")
        out.append("")
    return "\n".join(out).strip()


TRANSCRIPT = _build()
FAR_FIRST = TRANSCRIPT.splitlines()[1]           # первая реплика блока 0
FAR_LAST = [ln for ln in TRANSCRIPT.splitlines() if ln.strip()][-1]   # блок 59


def _result():
    return {
        "participants": [], "summary": "s", "detailed": [],
        "key_thoughts": [], "conclusions": [],
        "decisions": ["Оставляем базовые фильтры в каталоге"],
        "done_tasks": [],
        "tasks": [{"task": "Обновить сервер и закрыть задачу по конструктору", "owner": "Зоя Р"},
                  {"task": "Ревизия фильтров каталога", "owner": "Кирилл Бубнов"}],
        "minor_tasks": [],
    }


def _answers():
    # Время модель называет заведомо неверное — в запись обязано попасть
    # время метки той реплики, где нашлась цитата (восстановление по
    # полному фрагменту, а не по окну).
    return [_ev([
        {"i": 1, "quote": ANCHOR_ZOYA, "t": "96:34", "owner_ok": False},
        {"i": 2, "quote": "Я возьму ревизию фильтров каталога", "t": "01:13:01", "owner_ok": True},
        {"i": 3, "quote": "оставляем базовые фильтры в каталоге", "t": "00:10", "owner_ok": False},
    ])]


def _run(monkeypatch, windows: bool, result=None, answers=None, text=TRANSCRIPT):
    backend = FakeBackend(answers if answers is not None else _answers())
    monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: backend)
    monkeypatch.setattr(analyze, "_VERIFY_WINDOWS", windows)
    res = verify_protocol(result or _result(), text)
    return res, backend


class TestПромпт:
    def test_флаг_выключен_в_промпте_весь_фрагмент(self, monkeypatch):
        """Умолчание — поведение прежнее: фрагмент целиком, без пометок."""
        _res, backend = _run(monkeypatch, windows=False)
        assert len(backend.prompts) == 1
        assert TRANSCRIPT in backend.prompts[0]
        assert "отрывки расшифровки" not in backend.prompts[0]

    def test_флаг_включён_промпт_короче_и_содержит_окна_якорей(self, monkeypatch):
        _res_off, off = _run(monkeypatch, windows=False)
        _res_on, on = _run(monkeypatch, windows=True)
        p = on.prompts[0]
        assert len(p) < len(off.prompts[0]) * 0.6
        assert TRANSCRIPT not in p
        assert "отрывки расшифровки" in p and "\n…\n" in p
        lines = [ln for ln in TRANSCRIPT.splitlines() if ln.strip()]
        for anchor in (ANCHOR_ZOYA, ANCHOR_KIRILL, ANCHOR_MARIA):
            i = lines.index(anchor)
            # сама якорная строка и соседи с обеих сторон — на месте
            assert anchor in p
            assert lines[i - 3] in p and lines[i + 3] in p
            # и шапка блока с меткой времени — модель должна видеть «[чч:мм:сс]»
            hdr = next(lines[j] for j in range(i, -1, -1) if lines[j].startswith("["))
            assert hdr in p
        # далёкие реплики (блок 0 и блок 59) не отправлены
        assert FAR_FIRST not in p and FAR_LAST not in p

    def test_пункт_без_якорей_батч_получает_полный_фрагмент(self, monkeypatch):
        """Пункт, которому якорей не нашлось, не должен терять шанс: модель
        ищет только там, где показали, и окна для него — заведомый отказ."""
        result = _result()
        result["tasks"].append({"task": "Купить слона в Африке", "owner": ""})
        answers = [_ev([])]
        _res, backend = _run(monkeypatch, windows=True, result=result, answers=answers)
        assert TRANSCRIPT in backend.prompts[0]
        assert "отрывки расшифровки" not in backend.prompts[0]

    def test_пункт_без_основ_слов_тоже_полный_фрагмент(self, monkeypatch):
        result = _result()
        result["decisions"].append("Да")
        _res, backend = _run(monkeypatch, windows=True, result=result, answers=[_ev([])])
        assert TRANSCRIPT in backend.prompts[0]

    def test_окна_шире_70_процентов_полный_фрагмент(self, monkeypatch):
        """Экономии нет — риска не берём."""
        monkeypatch.setattr(analyze, "_VERIFY_WINDOW_LINES", 60)
        _res, backend = _run(monkeypatch, windows=True)
        assert TRANSCRIPT in backend.prompts[0]
        assert "отрывки расшифровки" not in backend.prompts[0]

    def test_заметки_участника_не_режутся(self, monkeypatch):
        notes = "Договорились: ревизия фильтров каталога на Кирилле.\n" * 3
        backend = FakeBackend([_ev([]), _ev([])])
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: backend)
        monkeypatch.setattr(analyze, "_VERIFY_WINDOWS", True)
        verify_protocol(_result(), TRANSCRIPT, user_notes=notes)
        assert notes.strip() in backend.prompts[0]


class TestРезультат:
    def test_цитата_из_окна_подтверждается_и_время_то_же_что_без_окон(self, monkeypatch):
        """Сверка и восстановление времени идут по ПОЛНОМУ фрагменту: итог
        в двух режимах совпадает по ok/quote/t на каждом пункте."""
        res_off, _ = _run(monkeypatch, windows=False)
        res_on, _ = _run(monkeypatch, windows=True)
        v_on, v_off = res_on["verification"], res_off["verification"]
        # блоки 10/30/50: 15 + b·55 с → 09:25, 27:45, 46:05 — время шапки
        # блока, а не «96:34»/«01:13:01»/«00:10» от модели
        assert v_on["tasks"][0]["ok"] and v_on["tasks"][0]["t"] == "09:25"
        assert v_on["tasks"][1]["ok"] and v_on["tasks"][1]["t"] == "27:45"
        assert v_on["decisions"][0]["ok"] and v_on["decisions"][0]["t"] == "46:05"
        for key in analyze._VERIFIED_LISTS:
            assert [(r["ok"], r["quote"], r["t"]) for r in v_on[key]] == \
                   [(r["ok"], r["quote"], r["t"]) for r in v_off[key]], key
        assert res_on["tasks"][1]["owner"] == "Кирилл Бубнов"
        assert res_on["tasks"][0]["owner"] == ""           # owner_ok=false — снят

    def test_цитата_вне_окна_сверяется_по_полному_фрагменту(self, monkeypatch):
        """Окна ограничивают только то, что видит модель. Если она (из кэша,
        из памяти) привела настоящую реплику, которой в окнах не было, —
        проверка по полному фрагменту её принимает, как и раньше."""
        result = _result()
        far = FAR_LAST.rstrip(".")
        result["decisions"].append("Решение про " + " ".join(far.split()[:3]))
        answers = [_ev([{"i": 4, "quote": far, "t": "00:00", "owner_ok": False}])]
        res, backend = _run(monkeypatch, windows=True, result=result, answers=answers)
        p = backend.prompts[0]
        if "отрывки расшифровки" in p:          # окна были — далёкая строка не показана
            assert FAR_LAST not in p
        v = res["verification"]["decisions"][1]
        assert v["ok"] and v["quote"] == far


class TestСтатистика:
    def test_stats_при_выключенном_флаге(self, monkeypatch):
        res, _ = _run(monkeypatch, windows=False)
        st = res["verification"]["stats"]
        assert st["windowed"] is False
        assert st["window_share"] == 1.0
        assert st["version"] == analyze.VERIFY_VERSION

    def test_stats_при_включённом_флаге(self, monkeypatch):
        res, backend = _run(monkeypatch, windows=True)
        st = res["verification"]["stats"]
        assert st["windowed"] is True
        assert 0 < st["window_share"] < 0.7
        # VERIFY_VERSION не поднят — правила сверки те же; суффикс отделяет
        # оконные протоколы в метриках
        assert st["version"] == analyze.VERIFY_VERSION + "+windows"
        assert analyze.VERIFY_VERSION == st["version"].split("+")[0]

    def test_share_усредняется_по_батчам(self, monkeypatch):
        """Один батч с окнами, другой — полный (пункт без якорей): среднее."""
        monkeypatch.setattr(analyze, "_VERIFY_BATCH", 3)
        result = _result()
        # пункты идут tasks → minor_tasks → done_tasks → decisions: батч 1 —
        # две задачи и первое решение, батч 2 — решение без якорей
        result["decisions"].append("Купить слона в Африке")
        _res, backend = _run(monkeypatch, windows=True, result=result,
                             answers=[_ev([]), _ev([])])
        assert len(backend.prompts) == 2
        assert "отрывки расшифровки" in backend.prompts[0]
        assert TRANSCRIPT in backend.prompts[1]
        st = _res["verification"]["stats"]
        sent = backend.prompts[0].split("Фрагмент расшифровки:\n", 1)[1]
        sent = sent.split("\n", 1)[1]                  # без строки-пометки про отрывки
        win = len(sent) / len(TRANSCRIPT)
        assert abs(st["window_share"] - (win + 1.0) / 2) < 0.002


class TestПоискОкон:
    def test_окна_сливаются_и_идут_по_тексту(self):
        shown, share = _verify_windows([ANCHOR_ZOYA, ANCHOR_MARIA, ANCHOR_KIRILL], TRANSCRIPT)
        assert 0 < share < 0.7
        parts = shown.split("\n…\n")
        assert len(parts) == 3
        assert ANCHOR_ZOYA in parts[0] and ANCHOR_KIRILL in parts[1] and ANCHOR_MARIA in parts[2]
        # две соседние строки с основаниями → одно окно, а не два
        shown2, _ = _verify_windows([ANCHOR_ZOYA, ANCHOR_ZOYA + " сервер"], TRANSCRIPT)
        assert shown2.count("…") == 0

    def test_окну_без_шапки_дописывается_шапка_блока(self, monkeypatch):
        """Метка времени и имя стоят только в шапке блока; окно, начавшееся
        ниже, их не содержит — модели нечем ставить `t` и `owner_ok`."""
        monkeypatch.setattr(analyze, "_VERIFY_WINDOW_LINES", 1)
        text = "\n".join([
            "[00:04:46] Зоя Р:",
            "Погода сегодня отличная, поедем на море.",
            "Билеты на поезд уже куплены.",
            "Гостиница у самого парка.",
            "Ужин в кафе возле метро.",
            "Кофе с лимоном и пирог.",
            "Надо обновить сервер и закрыть задачу по конструктору.",
            "Вишня и яблоки в саду.",
            "Трамвай идёт до выставки.",
            "Книга лежит на столе.",
            "Лампа над диваном.",
            "",
            "[00:05:20] Кирилл Бубнов:",
            "Шторы и ковёр купили.",
        ] + ["Окно в парк, музыка, чай."] * 20)
        shown, _share = _verify_windows([ANCHOR_ZOYA], text)
        assert shown.startswith("[00:04:46] Зоя Р:\n")
        assert shown.count("[00:04:46]") == 1
        assert "Погода сегодня" not in shown           # сама шапка — не всё начало блока
        assert ANCHOR_ZOYA in shown

    def test_редкие_слова_весят_больше(self):
        """Пункт из общих слов и одного редкого: якорь — там, где редкое."""
        rnd = random.Random(5)
        lines = ["[00:00:10] Зоя Р:"]
        for _ in range(200):
            lines.append("сделать проверить отправить " + " ".join(rnd.choice(_FILLER) for _ in range(4)))
        lines[120] = "сделать и отправить скан договора клиенту."
        text = "\n".join(lines)
        shown, share = _verify_windows(["Сделать, проверить и отправить скан договора"], text)
        assert "скан договора" in shown
        assert share < 0.7

    def test_быстро_на_длинной_расшифровке(self):
        """Поиск окон идёт в воркере, который делит GIL с сайтом (урок
        «Проверка протокола держала GIL»): по готовым индексам основ, без
        квадратичных переборов — доли секунды на тысячах строк."""
        rnd = random.Random(11)
        words = _FILLER + ("сервер задача конструктор фильтр каталог ревизия "
                           "договор скан клиент оплата отчёт таблица").split()
        out = []
        for i in range(3200):
            if i % 4 == 0:
                sec = i * 2
                out.append(f"[{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}] "
                           f"{rnd.choice(_NAMES)}:")
            out.append(" ".join(rnd.choice(words) for _ in range(rnd.randint(4, 14))))
        text = "\n".join(out)
        points = [" ".join(rnd.choice(words) for _ in range(7)) for _ in range(50)]
        analyze._speech_lines(text)                     # разбор текста — в кэш, как на бою
        t = time.perf_counter()
        for _ in range(3):                              # три батча по 50 пунктов
            _verify_windows(points, text)
        elapsed = time.perf_counter() - t
        assert elapsed < 1.0, elapsed
