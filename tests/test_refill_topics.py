"""Пустые разделы протокола не доходят до человека.

Раньше раздел вида «Обсуждаются задачи и бэклог. Решается вопрос о добавлении
задач в бэклог» просто помечался как пустой — и всё равно попадал в документ.
Теперь он сначала переписывается по расшифровке, а если и после этого сказать
нечего (тему лишь упомянули) — удаляется.

Тесты фиксируют МЕХАНИКУ: движок фейковый, вызовы считаются.
"""
import pytest

from app import analyze
from app.analyze import AnalysisCancelled, refill_empty_topics

EMPTY = {"topic": "Бэклог и задачи",
         "details": "Обсуждаются задачи и бэклог. Решается вопрос о добавлении "
                    "задач в бэклог."}
GOOD = {"topic": "Прототип клиентской части",
        "details": "Договорились, что прототип делает Кирилл до 25 июля. "
                   "В Figma есть каркас карточки, но нет состояний ошибок."}
FILLED = {"topic": "Бэклог и задачи",
          "details": "Часть задач со стендапа не попадала в бэклог и терялась. "
                     "Договорились заводить задачу сразу на встрече, разбор "
                     "бэклога вынесли на пятницу 25 июля."}

TRANSCRIPT = "[00:01] Иван: задачи со стендапа теряются, давайте заводить сразу"


def _res(*topics):
    return {"detailed": [dict(t) for t in topics]}


def test_пустой_раздел_переписывается(monkeypatch):
    calls = []

    def fake(_text, block, **kw):
        calls.append(block["topic"])
        return dict(FILLED)

    monkeypatch.setattr(analyze, "regen_topic_details", fake)
    out = refill_empty_topics(_res(GOOD, EMPTY), TRANSCRIPT)
    assert calls == ["Бэклог и задачи"], "переписывать надо только пустой раздел"
    assert out["detailed"][1]["details"] == FILLED["details"]
    assert out["_refill"] == {"regenerated": 1, "dropped": 0}


def test_безнадёжный_раздел_удаляется(monkeypatch):
    """Если и после перегенерации пусто — темы в расшифровке просто нет."""
    monkeypatch.setattr(analyze, "regen_topic_details",
                        lambda _t, b, **kw: dict(EMPTY))
    out = refill_empty_topics(_res(GOOD, EMPTY), TRANSCRIPT)
    assert [b["topic"] for b in out["detailed"]] == [GOOD["topic"]]
    assert out["_refill"] == {"regenerated": 1, "dropped": 1}


def test_ошибка_движка_не_теряет_раздел(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("движок недоступен")

    monkeypatch.setattr(analyze, "regen_topic_details", boom)
    out = refill_empty_topics(_res(GOOD, EMPTY), TRANSCRIPT)
    assert len(out["detailed"]) == 2, "при сбое раздел остаётся как был"
    assert out["_refill"]["regenerated"] == 0


def test_лимит_вызовов_соблюдается(monkeypatch):
    calls = []
    monkeypatch.setattr(analyze, "regen_topic_details",
                        lambda _t, b, **kw: calls.append(1) or dict(FILLED))
    monkeypatch.setattr(analyze, "_MAX_TOPIC_REGENS", 2)
    out = refill_empty_topics(_res(*[EMPTY] * 5), TRANSCRIPT)
    assert len(calls) == 2, "лимит перегенераций не соблюдён"
    # Что не успели переписать — остаётся на месте, а не пропадает.
    assert len(out["detailed"]) == 5


def test_отмена_пробрасывается(monkeypatch):
    monkeypatch.setattr(analyze, "regen_topic_details",
                        lambda _t, b, **kw: dict(FILLED))
    with pytest.raises(AnalysisCancelled):
        refill_empty_topics(_res(EMPTY), TRANSCRIPT, cancel_check=lambda: True)


def test_движок_переиспользуется(monkeypatch):
    """Внутри разбора длинной встречи цепочка уже могла переключиться на облако.
    Создавать её заново — значит откатиться на медленный локальный движок."""
    seen = {}
    monkeypatch.setattr(analyze, "regen_topic_details",
                        lambda _t, b, **kw: seen.update(kw) or dict(FILLED))
    sentinel = object()
    refill_empty_topics(_res(EMPTY), TRANSCRIPT, backend=sentinel)
    assert seen.get("backend") is sentinel


def test_без_расшифровки_ничего_не_делаем(monkeypatch):
    """Перегенерировать не из чего — не трогаем протокол и не жжём вызовы."""
    monkeypatch.setattr(analyze, "regen_topic_details",
                        lambda *a, **kw: pytest.fail("движок вызывать нельзя"))
    out = refill_empty_topics(_res(EMPTY), "")
    assert len(out["detailed"]) == 1
