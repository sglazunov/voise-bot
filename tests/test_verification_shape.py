"""Разметка проверки может прийти не по схеме — протокол терять из-за этого нельзя.

Grounding просит модель вернуть по каждому пункту запись {ok, quote, t, source,
owner_ok}. Схему соблюдают не все: в боевом протоколе от Yandex Cloud вместо
словарей пришли строки. Дальше по пути стоят экспорт в Word и снятие
неподтверждённых ответственных — и оба падали на этом, теряя уже собранный
протокол целиком.
"""
from __future__ import annotations

import pytest

from app.analyze import _strip_unfounded_owners
from app.docx_export import _vinfo


class TestРазборЗаписи:
    def test_словарь_возвращается(self):
        a = {"verification": {"tasks": [{"ok": True, "quote": "цитата"}]}}
        assert _vinfo(a, "tasks", 0)["quote"] == "цитата"

    @pytest.mark.parametrize("rec", ["строка", 42, None, ["список"]])
    def test_не_словарь_равнозначен_отсутствию(self, rec):
        """Иначе на нём падает весь экспорт в Word."""
        a = {"verification": {"tasks": [rec]}}
        assert _vinfo(a, "tasks", 0) is None

    def test_пустая_разметка(self):
        assert _vinfo({}, "tasks", 0) is None
        assert _vinfo({"verification": None}, "tasks", 5) is None


class TestОтветственные:
    def _res(self):
        return {"tasks": [{"task": "сделать", "owner": "Пётр"}]}

    def test_подтверждённый_остаётся(self):
        res = self._res()
        _strip_unfounded_owners(res, {"tasks": [{"ok": True, "owner_ok": True}]})
        assert res["tasks"][0]["owner"] == "Пётр"

    def test_неподтверждённый_снимается(self):
        res = self._res()
        _strip_unfounded_owners(res, {"tasks": [{"ok": False, "owner_ok": False}]})
        assert res["tasks"][0]["owner"] == ""

    def test_кривая_запись_ответственного_не_трогает(self):
        """Снимать ответственного из-за ФОРМЫ разметки — молчаливая порча
        протокола: пункт остался бы без исполнителя без всякой причины."""
        res = self._res()
        _strip_unfounded_owners(res, {"tasks": ["не по схеме"]})
        assert res["tasks"][0]["owner"] == "Пётр"

    def test_короткая_разметка_не_роняет(self):
        """Модель вернула меньше записей, чем пунктов."""
        res = {"tasks": [{"task": "раз", "owner": "А"}, {"task": "два", "owner": "Б"}]}
        _strip_unfounded_owners(res, {"tasks": [{"ok": True, "owner_ok": True}]})
        assert [t["owner"] for t in res["tasks"]] == ["А", "Б"]
