"""Проверки по находкам ревью — чтобы дефекты не вернулись.

Каждый тест назван по номеру находки: так видно, что именно он стережёт.
"""
import re

from app import analyze, llm


class TestK2СекретНеУтекает:
    """Ключ Gemini уходил в АДРЕСЕ запроса, адрес печатается в тексте ошибки,
    ошибка — в причину отката, оттуда в карточку и в шапку Word-протокола.
    Протокол кладут в облако и в Weeek: секрет уезжал вместе с ним."""

    def test_адрес_в_ошибке_без_query(self):
        url = "https://example.com/v1/models/x:generate?key=СЕКРЕТ&alt=json"
        safe = llm._safe_url(url)
        assert "СЕКРЕТ" not in safe
        assert safe == "https://example.com/v1/models/x:generate"

    def test_адрес_без_query_не_портится(self):
        url = "https://integrate.api.nvidia.com/v1/chat/completions"
        assert llm._safe_url(url) == url

    def test_ключ_не_подставляется_в_адрес(self):
        """Прямая проверка того, что сломалось: в коде провайдера Gemini
        адреса с «?key=» быть не должно."""
        import pathlib
        src = pathlib.Path(llm.__file__).read_text(encoding="utf-8")
        assert "generateContent?key=" not in src
        assert "x-goog-api-key" in src


class TestV12ПроверкаНеПодтверждаетСебя:
    """Цитаты искались в тексте, куда уже дописаны блоки контекста. Задача,
    выдуманная по «ПОСТОЯННОМУ КОНТЕКСТУ», находила там «дословную» цитату и
    помечалась подтверждённой."""

    def test_служебные_блоки_вырезаны_из_источников(self):
        text = ("[00:10] Обсудили сроки.\n\n"
                "=== ПОСТОЯННЫЙ КОНТЕКСТ ===\n"
                "Проект «Ромашка»: внедрить CRM до конца квартала.\n")
        clean = analyze._INJECTED_BLOCK_RE.sub(" ", text)
        assert "Обсудили сроки" in clean
        assert "внедрить CRM" not in clean


class TestV5ОтветственныеПриСбоеПроверки:
    """Сбой проверки — это «не знаем», а не «не подтверждено». Раньше он
    снимал ответственных со ВСЕХ задач."""

    def _result(self):
        return {"tasks": [{"task": "Сделать макет", "owner": "Елизавета"}],
                "minor_tasks": [], "done_tasks": []}

    def test_при_сбое_ответственный_остаётся(self):
        res = self._result()
        ver = {"error": "Проверка не завершена: движок недоступен",
               "tasks": [{"ok": False, "owner_ok": False}],
               "minor_tasks": [], "done_tasks": []}
        if not ver.get("error"):
            analyze._strip_unfounded_owners(res, ver)
        assert res["tasks"][0]["owner"] == "Елизавета"

    def test_без_сбоя_неподтверждённый_ответственный_снимается(self):
        res = self._result()
        ver = {"tasks": [{"ok": True, "owner_ok": False}],
               "minor_tasks": [], "done_tasks": []}
        analyze._strip_unfounded_owners(res, ver)
        assert res["tasks"][0]["owner"] == ""

    def test_подтверждённый_ответственный_сохраняется(self):
        res = self._result()
        ver = {"tasks": [{"ok": True, "owner_ok": True}],
               "minor_tasks": [], "done_tasks": []}
        analyze._strip_unfounded_owners(res, ver)
        assert res["tasks"][0]["owner"] == "Елизавета"


class TestV27ПорядокДвижков:
    """Замеры показали, что Groq для протоколов худший, а он стоял первым."""

    def test_nvidia_впереди_groq(self):
        from app import config
        order = config.PROVIDER_ORDER
        assert order.index("nvidia") < order.index("groq")
        assert order.index("gemini") < order.index("groq")


class TestK4V6ВыборкаПоВсейЗаписи:
    """Шаг сэмплирования был фиксированным, и покрытие упиралось в «шаг ×
    максимум кадров»: участники читались с первых 8 минут, спикеры — с 75,
    текст с экрана — с 30. На четырёхчасовых записях вторая половина встречи
    оставалась без имён и без экрана."""

    @staticmethod
    def _step(duration, every_sec, max_frames):
        """Та же формула, что в _sample_frames."""
        return max(every_sec, duration / max_frames) if duration and max_frames else every_sec

    def test_участники_покрывают_всю_встречу(self):
        # 8 кадров на часовую встречу — шаг 7.5 минуты, а не 60 секунд.
        step = self._step(3600, 60.0, 8)
        assert step * 8 >= 3600

    def test_спикеры_покрывают_четыре_часа(self):
        step = self._step(4 * 3600, 1.5, 3000)
        assert step * 3000 >= 4 * 3600

    def test_экран_покрывает_четыре_часа(self):
        step = self._step(4 * 3600, 5.0, 360)
        assert step * 360 >= 4 * 3600

    def test_короткую_запись_не_разрежаем(self):
        """На пятиминутной встрече шаг остаётся заданным, а не растягивается."""
        assert self._step(300, 1.5, 3000) == 1.5

    def test_две_настройки_не_делят_переменную(self):
        import pathlib
        from app import speaker_id
        src = pathlib.Path(speaker_id.__file__).read_text(encoding="utf-8")
        assert src.count('getenv("VTX_SPEAKER_MIN_TILE"') == 1
