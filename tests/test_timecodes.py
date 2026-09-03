"""Время в основании берётся из расшифровки по найденной цитате, а не от модели.

Замер по шести боевым протоколам (docs/ТЗ-ТАЙМКОДЫ.md): цитата находится в
расшифровке в 100 % случаев, а время модели точно лишь в 35 %, мимо больше
минуты — в 16 %, и каждое четвёртое время вообще отсутствует среди меток
расшифровки. Человек открывал запись на названной минуте и слышал другой
разговор.

Тесты закрепляют механику: индекс времени с наследованием метки, локатор всех
вхождений, выбор вхождения по подсказке модели и полный запрет модельного
времени в записи.
"""
from __future__ import annotations

import json
import random

from app import analyze, docx_export
from app.analyze import (_apply_transcript_times, _fmt_t, _norm_for_match,
                         _strip_labels, _TimeIndex, verify_protocol)

# Боевой формат `formats.to_txt`: метка «[чч:мм:сс]» стоит в шапке блока
# говорящего, реплики блока идут голыми строками.
PROD = "\n".join([
    "[00:04:46] Зоя Р:",
    "Надо обновить сервер и закрыть задачу по конструктору.",
    "Со следующей недели мы его точно сюда призовём.",
    "",
    "[00:05:20] Кирилл Бубнов:",
    "Хорошо, я тогда займусь этим вопросом сегодня вечером.",
    "",
    "[01:06:54] Кирилл Бубнов:",
    "Ну вообще здесь предполагалось пока вот она на главной.",
    "Со следующей недели мы его точно сюда призовём.",
])


def _ver_rec(quote: str, ok: bool = True, **extra) -> dict:
    rec = {"ok": ok, "quote": quote, "t": None, "source": "transcript",
           "owner_ok": False, "match": "verbatim"}
    rec.update(extra)
    return rec


def _apply(quote: str, ok: bool = True, **extra) -> dict:
    """Одна задача с цитатой → её запись проверки после подстановки времени."""
    res = {"tasks": [{"task": "Задача", "owner": ""}],
           "minor_tasks": [], "done_tasks": [], "decisions": []}
    ver = {"tasks": [_ver_rec(quote, ok, **extra)],
           "minor_tasks": [], "done_tasks": [], "decisions": []}
    _apply_transcript_times(res, ver, PROD)
    return ver["tasks"][0]


class TestИндексВремени:
    def test_построчная_нормализация_равна_нормализации_целиком(self):
        """Опора всей конструкции: индекс ищет по ТОЙ ЖЕ строке, по которой
        `_Fragment.match` признаёт дословность. Если свойство перестанет
        выполняться, появится «цитата подтверждена, а локатор её не нашёл»."""
        def prop(text: str) -> bool:
            whole = _norm_for_match(_strip_labels(text))
            per = " ".join(x for x in (_norm_for_match(_strip_labels(ln))
                                       for ln in text.splitlines()) if x)
            return whole == per

        assert prop(PROD)
        assert prop("[04:46] Зоя: надо обновить\n[05:01] и закрыть задачу")
        assert prop("речь без меток\n\nвторая строка")
        rnd = random.Random(7)
        words = "надо обновить сервер ёлки задача 42 привет — «да» тест-драйв".split()
        for _ in range(200):
            lines = []
            for _i in range(rnd.randint(1, 8)):
                r = rnd.random()
                if r < 0.4:
                    lines.append(f"[{rnd.randint(0, 2):02d}:{rnd.randint(0, 59):02d}:"
                                 f"{rnd.randint(0, 59):02d}] Имя Ф:")
                elif r < 0.5:
                    lines.append("")
                else:
                    lines.append(" ".join(rnd.choices(words, k=rnd.randint(1, 7))))
            assert prop("\n".join(lines))

    def test_строка_без_метки_наследует_метку_шапки_блока(self):
        # У «Хорошо, я тогда займусь…» своей метки нет — она в шапке блока.
        # Без наследования больше половины цитат остались бы без времени.
        assert _TimeIndex(PROD).locate("Хорошо, я тогда займусь этим вопросом") == [320]

    def test_цитата_через_границу_реплик_получает_время_первой(self):
        secs = _TimeIndex(PROD).locate("задачу по конструктору. Со следующей недели")
        assert secs == [286]

    def test_оба_формата_меток(self):
        # «[мм:сс]» — тестовые фикстуры, «[чч:мм:сс]» — бой.
        assert _TimeIndex("[04:46] Зоя: надо обновить сервер").locate("надо обновить сервер") == [286]
        assert _TimeIndex(PROD).locate("Ну вообще здесь предполагалось") == [4014]

    def test_формат_вывода_как_в_просмотрщике(self):
        # Т8: «мм:сс» до часа, «ч:мм:сс» начиная с часа. «73:01» не бывает.
        assert _fmt_t(286) == "04:46" and _fmt_t(9) == "00:09"
        assert _fmt_t(4014) == "1:06:54" and _fmt_t(3600) == "1:00:00"

    def test_короткая_цитата_не_ищется(self):
        # «Да, беру» повторяется на встрече десятками — время по ней случайное.
        assert _TimeIndex(PROD).locate("Хорошо") == []

    def test_расшифровка_без_меток_оставляет_пункты_без_времени(self):
        ix = _TimeIndex("Надо обновить сервер и закрыть задачу по конструктору.")
        assert ix.locate("обновить сервер и закрыть задачу") == []


class TestПодстановкаВремени:
    def test_время_метки_вместо_времени_модели(self):
        v = _apply("Надо обновить сервер и закрыть задачу", t_hint="96:34")
        assert v["t"] == "04:46" and "t_hint" not in v

    def test_несколько_вхождений_без_подсказки_первое(self):
        v = _apply("Со следующей недели мы его точно сюда призовём")
        assert v["t"] == "04:46" and v["t_hits"] == 2

    def test_несколько_вхождений_с_подсказкой_ближайшее(self):
        # Подсказка выбирает между НАСТОЯЩИМИ местами разговора, а не создаёт
        # время. Разбирается она нестрого: «66:54» — это ложный час, сложенный
        # моделью в минуты, и оба прочтения (66:54 и 06:54) считаются
        # правдоподобными, иначе подсказка увела бы выбор на час в сторону.
        q = "Со следующей недели мы его точно сюда призовём"
        assert _apply(q, t_hint="1:06:50")["t"] == "1:06:54"
        assert _apply(q, t_hint="66:54")["t"] == "1:06:54"
        assert _apply(q, t_hint="04:40")["t"] == "04:46"

    def test_цитата_из_заметок_остаётся_без_времени(self):
        # Т10: в заметках участника времени нет вовсе, модельное было выдумкой.
        v = _apply("Надо обновить сервер и закрыть задачу", source="notes", t_hint="04:46")
        assert v["t"] is None and "t_hits" not in v

    def test_опровергнутый_пункт_времени_не_получает(self):
        # `ok` снят в _refine_tasks («дальше в разговоре: нет, не надо») —
        # основания больше нет, значит и времени быть не должно.
        v = _apply("Надо обновить сервер и закрыть задачу", ok=False, t_hint="04:46")
        assert v["t"] is None

    def test_приблизительная_цитата_с_опечаткой_в_первом_слове(self):
        # Т12: якорь — самое редкое слово цитаты. Раньше кандидаты брались по
        # первым двум словам, и опечатка именно там уводила поиск.
        v = _apply("Нада обновить сервер и закрыть задачу по конструктору", match="approx")
        assert v["t"] == "04:46"

    def test_ненайденная_цитата_не_трогает_подтверждение(self):
        # Т9: восстановление времени не умеет опровергать пункты.
        v = _apply("Такого на этой встрече никто никогда не говорил вслух")
        assert v["t"] is None and v["ok"] is True and v["match"] == "verbatim"


class TestПунктБезЦитаты:
    """Т16: место в разговоре ищется на ПОНИЖЕННОМ пороге и подтверждением
    не становится. С порогом подтверждения добавка не дала бы ничего: сюда
    доходят ровно те пункты, на которых поиск опоры уже вернул пустоту."""

    TEXT = "\n".join([
        "[00:22:30] Зоя Р:",
        "Дальше у нас раздел «Мои наставники» и «Мои ученики», надо решать.",
        "[00:22:48] Кирилл Бубнов:",
        "Там пока непонятно, что показывать, давайте отложим до следующего раза.",
    ])

    def _run(self, task: str) -> dict:
        res = {"tasks": [{"task": task, "owner": "Кирилл"}],
               "minor_tasks": [], "done_tasks": [], "decisions": []}
        ver = {"tasks": [{"ok": False, "quote": "", "t": None, "source": None,
                          "owner_ok": False}],
               "minor_tasks": [], "done_tasks": [], "decisions": []}
        _apply_transcript_times(res, ver, self.TEXT)
        return ver["tasks"][0]

    # Пункт из «мёртвой зоны»: поиску опоры (0,6) он не даётся, поиску времени
    # (0,35) — даётся. Ровно ради таких пунктов порог и понижен.
    ПУНКТ = "Подготовить наполнение раздела «Мои ученики» и показать директору"

    def test_пункт_в_мёртвой_зоне_порогов(self):
        assert analyze._find_support(self.ПУНКТ, self.TEXT) is None
        assert analyze._find_support(self.ПУНКТ, self.TEXT,
                                     threshold=analyze._TIME_HINT_MIN)

    def test_место_нашлось_но_пункт_остался_непроверенным(self):
        v = self._run(self.ПУНКТ)
        assert v["t"] == "22:30" and v["t_approx"] is True
        # Т16.1: ни подтверждения, ни цитаты, ни ответственного из ниоткуда.
        assert v["ok"] is False and not v["quote"] and v.get("match") is None

    def test_то_же_сквозь_verify_protocol(self, monkeypatch):
        """Проверка целиком: движок цитат не нашёл, пункт остаётся в «требуют
        проверки», но ориентир по времени у него есть."""
        class Empty:
            name = "fake"

            def complete(self, *a, **k):
                return json.dumps({"items": []})

        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: Empty())
        res = {"participants": [], "summary": "s", "detailed": [], "key_thoughts": [],
               "conclusions": [], "decisions": [], "done_tasks": [], "minor_tasks": [],
               "tasks": [{"task": self.ПУНКТ, "owner": "Кирилл"}]}
        v = verify_protocol(res, self.TEXT)["verification"]["tasks"][0]
        assert v["ok"] is False and not v["quote"]
        assert v["t"] == "22:30" and v["t_approx"] is True

    def test_порог_поиска_ниже_порога_подтверждения(self):
        assert analyze._TIME_HINT_MIN < analyze._SUPPORT_MIN

    def test_чужой_пункт_остаётся_без_времени(self):
        # Т16.2: не нашлось — ожидаемый исход, а не дефект.
        assert self._run("Закупить кофемашину в переговорную на третьем этаже")["t"] is None


class TestРегрессияВремениМодели:
    """Главная регрессия: модельное время не попадает в протокол ни в одном
    случае. Расшифровка — в боевом формате, модель называет «96:34»."""

    class _Fake:
        name = "fake"

        def __init__(self, answer):
            self.answer = answer

        def complete(self, prompt, max_tokens=2000, force_json=True, **kw):
            return self.answer

    def test_модельное_время_в_запись_не_попадает(self, monkeypatch):
        answer = json.dumps({"items": [
            {"i": 1, "quote": "Надо обновить сервер и закрыть задачу по конструктору",
             "t": "96:34", "owner_ok": False},
            {"i": 2, "quote": "Ну вообще здесь предполагалось пока вот она на главной",
             "t": "66:54", "owner_ok": False},
        ]}, ensure_ascii=False)
        monkeypatch.setattr(analyze.llm, "get_provider_chain",
                            lambda *a, **k: self._Fake(answer))
        res = {"participants": [], "summary": "s", "detailed": [], "key_thoughts": [],
               "conclusions": [], "decisions": [], "done_tasks": [], "minor_tasks": [],
               "tasks": [{"task": "Обновить сервер и закрыть задачу по конструктору", "owner": ""},
                         {"task": "Оставить блок на главной", "owner": ""}]}
        out = verify_protocol(res, PROD)
        v = out["verification"]["tasks"]
        assert v[0]["t"] == "04:46" and v[1]["t"] == "1:06:54"
        # Ни одного времени, которого нет среди меток расшифровки, и ни одной
        # формы «66:54» — на этой встрече такой минуты не было.
        assert all(rec["t"] in ("04:46", "1:06:54") for rec in v)
        assert "t_hint" not in v[0] and "t_hint" not in v[1]


class TestПодписиОснования:
    """Т17/Т18: значка «≈» больше нет — три случая различаются словами.
    Значок не мог отличить «цитату перефразировали» от «основания нет вовсе»,
    а читателю протокола это разные вещи."""

    def _line(self, v: dict) -> str:
        from docx import Document
        from docx.shared import RGBColor

        par = Document().add_paragraph("Пункт протокола")
        docx_export._apply_verification(par, v, RGBColor)
        return par.text

    def test_дословная_приблизительная_и_без_основания(self):
        assert "Основание [04:46]: «надо обновить сервер»" in self._line(
            {"ok": True, "t": "04:46", "quote": "надо обновить сервер", "match": "verbatim"})
        assert "Основание [04:46], цитата приблизительная: «надо обновить сервер»" in self._line(
            {"ok": True, "t": "04:46", "quote": "надо обновить сервер", "match": "approx"})
        assert "Основание (из заметок участника): «надо обновить сервер»" in self._line(
            {"ok": True, "t": None, "quote": "надо обновить сервер", "source": "notes"})
        without = self._line({"ok": False, "t": "04:46", "quote": "", "t_approx": True})
        assert "Основания нет; в разговоре об этом — примерно [04:46]" in without

    def test_значка_приблизительности_больше_нет(self):
        for v in ({"ok": True, "t": "04:46", "quote": "цитата про сервер", "match": "approx"},
                  {"ok": False, "t": "04:46", "quote": "", "t_approx": True},
                  {"ok": False, "t": None, "quote": ""}):
            assert "≈" not in self._line(v)
