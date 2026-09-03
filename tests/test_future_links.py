"""Ссылки на видео и протокол — только у состоявшейся встречи.

Повторяющаяся встреча в Weeek уезжает на новую дату вместе со ЗНАЧЕНИЯМИ
кастом-полей — неважно, двигает Weeek дату у той же задачи или создаёт КОПИЮ с
новым идентификатором (заказчик подтвердил: копия, и создаётся она после
закрытия оригинала). Человек открывает будущую задачу, видит заполненные
«Видео встречи» и «Протокол встречи» и читает документ ЧУЖОГО проведения.
Заметить подмену нельзя: данные выглядят настоящими, дата внутри ссылки не
видна.

Правило, которое здесь проверяется, опирается не на происхождение задачи, а на
факт записи: ссылки пишутся ТОЛЬКО после состоявшегося проведения. Значит,
встреча ещё впереди + по её task_id у нас нет ни одного проведения = в полях
наследство, и его надо снять. У копии проведений нет по определению, а у
закрытого оригинала ссылки остаются навсегда — терять нечего.

Отдельная забота — поле «Встреча» со ссылкой на Телемост: затри его, и на
следующий звонок не попадёт ни бот, ни человек.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.automation import scheduler as sched_mod, weeek
from app.automation.scheduler import MeetingState, scheduler

VIDEO = "Видео встречи"
PROTO = "Протокол встречи"
TELEMOST = "https://telemost.yandex.ru/j/1234567890123456"

CFG = {"weeek_token": "tok", "timezone": "UTC",
       "weeek_video_field": VIDEO, "weeek_protocol_field": PROTO,
       "weeek_protected_fields": ["Встреча"]}


class FakeWeeek:
    """Живой Weeek целиком: задачи, GET, PUT и счётчик изменяющих запросов."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict] = {}
        self.writes: list[tuple] = []      # только изменяющие запросы
        self.reads = 0
        self.swallow = False               # PUT принят, а значение не изменилось

    def add(self, task_id, fields: dict, **extra) -> dict:
        task = {"id": task_id, "title": f"Планёрка {task_id}",
                "customFields": [{"id": f"{task_id}-{i}", "name": n, "value": v}
                                 for i, (n, v) in enumerate(fields.items())]}
        task.update(extra)
        self.tasks[str(task_id)] = task
        return task

    def value(self, task_id, name: str):
        return weeek.custom_field_text(self.tasks[str(task_id)], name)

    def request(self, method, path, token, params=None, body=None, timeout=30):
        tid = path.rsplit("/", 1)[-1]
        if method == "GET":
            self.reads += 1
            return {"task": copy.deepcopy(self.tasks[tid])}
        if method == "PUT":
            self.writes.append((tid, body))
            if not self.swallow:
                for fid, val in (body.get("customFields") or {}).items():
                    for cf in self.tasks[tid]["customFields"]:
                        if str(cf["id"]) == str(fid):
                            cf["value"] = val
            return {"success": True}
        raise AssertionError(f"неожиданный запрос {method} {path}")


def _meeting(api: FakeWeeek, task_id, minutes: float):
    """Встреча так, как её видит опрос: с датой и СЫРОЙ задачей на этот момент."""
    start = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    task = copy.deepcopy(api.tasks[str(task_id)])
    return SimpleNamespace(task_id=task_id, title=task["title"], start=start,
                           url=TELEMOST, raw=task)


@pytest.fixture
def api(monkeypatch):
    fake = FakeWeeek()
    monkeypatch.setattr(weeek, "_request", fake.request)
    monkeypatch.setattr(sched_mod.snapshots, "load", lambda user: {})
    scheduler._states.clear()
    yield fake
    scheduler._states.clear()


def _snapshots(monkeypatch, snaps: dict):
    monkeypatch.setattr(sched_mod.snapshots, "load", lambda user: snaps)


# --------------------------------------------------------------------------- #
# Основное правило
# --------------------------------------------------------------------------- #
def test_унаследованные_ссылки_снимаются(api):
    """Т1, Т4. Встреча впереди, проведений по задаче не было — значит в полях
    наследство прошлой встречи."""
    api.add(200, {"Встреча": TELEMOST, VIDEO: "https://cloud/прошлое.mp4",
                  PROTO: "https://cloud/прошлый.docx"})
    done, errors = scheduler._wipe_inherited("alice", CFG,
                                             [_meeting(api, 200, 60)])

    assert (done, errors) == (1, 0)
    assert api.value(200, VIDEO) == "" and api.value(200, PROTO) == ""
    assert len(api.writes) == 2                      # ровно по одной записи на поле


def test_ссылка_на_телемост_не_меняется(api):
    """Т3. Поле «Встреча» — единственный способ попасть на звонок."""
    api.add(200, {"Встреча": TELEMOST, VIDEO: "https://cloud/прошлое.mp4"})
    scheduler._wipe_inherited("alice", CFG, [_meeting(api, 200, 60)])

    assert api.value(200, "Встреча") == TELEMOST


def test_своё_проведение_не_трогается(api, monkeypatch):
    """Т2, Т5. По задаче есть карточка с облачной ссылкой — значит ссылки в
    полях писали мы, и это ссылки ЭТОГО проведения."""
    api.add(100, {VIDEO: "https://cloud/наше.mp4", PROTO: "https://cloud/наш.docx"})
    _snapshots(monkeypatch, {"alice:100:2026-09-01T10:00:00+00:00": {
        "state": "done", "cloud_url": "https://cloud/наше.mp4"}})

    done, errors = scheduler._wipe_inherited("alice", CFG,
                                             [_meeting(api, 100, 60)])

    assert (done, errors) == (0, 0)
    assert api.writes == []
    assert api.value(100, VIDEO) == "https://cloud/наше.mp4"


def test_копия_чистится_оригинал_нет(api, monkeypatch):
    """Главный сюжет заказчика: копия создаётся ПОСЛЕ закрытия оригинала, с
    новым id и скопированными полями. У оригинала ссылки остаются навсегда, у
    копии снимаются."""
    api.add(100, {VIDEO: "https://cloud/наше.mp4", PROTO: "https://cloud/наш.docx"})
    api.add(200, {VIDEO: "https://cloud/наше.mp4", PROTO: "https://cloud/наш.docx"})
    _snapshots(monkeypatch, {"alice:100:2026-09-01T10:00:00+00:00": {
        "state": "done", "cloud_url": "https://cloud/наше.mp4"}})

    scheduler._wipe_inherited("alice", CFG, [_meeting(api, 100, -60),
                                             _meeting(api, 200, 60 * 24 * 7)])

    assert api.value(100, VIDEO) == "https://cloud/наше.mp4"
    assert api.value(200, VIDEO) == "" and api.value(200, PROTO) == ""
    assert {t for t, _ in api.writes} == {"200"}


def test_копию_чистим_пока_оригинал_ещё_обрабатывается(api, monkeypatch):
    """Задачу закрыли раньше, чем бот дописал ссылку на протокол: копия уже
    появилась, а по оригиналу ещё идёт сборка (и поздняя дозагрузка записи).
    Одно другому мешать не должно — чистим только копию."""
    api.add(100, {VIDEO: "https://cloud/наше.mp4", PROTO: "https://cloud/прошлый.docx"})
    api.add(200, {VIDEO: "https://cloud/наше.mp4", PROTO: "https://cloud/прошлый.docx"})
    st = MeetingState(key="alice:100:x", task_id=100, title="Планёрка",
                      url=TELEMOST, start=datetime.now(timezone.utc),
                      owner="alice", state="analyzing",
                      cloud_url="https://cloud/наше.mp4")
    scheduler._states[st.key] = st

    scheduler._wipe_inherited("alice", CFG, [_meeting(api, 100, -30),
                                             _meeting(api, 200, 60 * 24 * 7)])

    assert api.value(100, PROTO) == "https://cloud/прошлый.docx"  # допишет доставка
    assert api.value(200, PROTO) == ""
    assert {t for t, _ in api.writes} == {"200"}


def test_идущая_запись_не_трогается(api):
    """Т7. Бот уже в звонке (заходит за пару минут до начала), ссылка вот-вот
    появится — очистка на полпути стёрла бы её же."""
    api.add(300, {VIDEO: "https://cloud/прошлое.mp4"})
    st = MeetingState(key="alice:300:x", task_id=300, title="Планёрка",
                      url=TELEMOST, start=datetime.now(timezone.utc),
                      owner="alice", state="recording")
    scheduler._states[st.key] = st

    done, _ = scheduler._wipe_inherited("alice", CFG, [_meeting(api, 300, 2)])

    assert (done, api.writes) == (0, [])


def test_прошедшая_встреча_не_трогается(api):
    """Т6. Встреча уже прошла: в полях либо наши свежие ссылки, либо
    прикреплённые руками — в обоих случаях не наследство."""
    api.add(400, {VIDEO: "https://cloud/вчера.mp4"})
    done, _ = scheduler._wipe_inherited("alice", CFG, [_meeting(api, 400, -5)])

    assert (done, api.writes) == (0, [])


def test_задача_без_даты_не_трогается(api):
    """Т8. Такую карточку человек заполняет руками, а прежний ночной проход
    стирал её каждую ночь."""
    api.add(500, {VIDEO: "https://вручную/видео"})
    m = _meeting(api, 500, 60)
    m.start = None
    done, _ = scheduler._wipe_inherited("alice", CFG, [m])

    assert (done, api.writes) == (0, [])


def test_закрытая_задача_не_трогается(api):
    """Т9. У закрытой задачи поля — документ прошедшей встречи."""
    api.add(600, {VIDEO: "https://cloud/прошлое.mp4"}, isCompleted=True)
    done, _ = scheduler._wipe_inherited("alice", CFG, [_meeting(api, 600, 60)])

    assert (done, api.writes) == (0, [])


def test_решение_не_писать_сохраняет_ссылки(api):
    """Т10. Записи не будет — прошлые ссылки останутся единственным
    содержимым задачи."""
    api.add(700, {VIDEO: "https://cloud/прошлое.mp4",
                  "Запись встречи": False})
    done, _ = scheduler._wipe_inherited("alice", CFG, [_meeting(api, 700, 60)])

    assert (done, api.writes) == (0, [])


def test_выключенное_поле_не_чистится(api):
    """Т12. Раз мы в поле не пишем, то и лежит там не наше."""
    api.add(800, {VIDEO: "https://cloud/прошлое.mp4",
                  PROTO: "https://вручную/протокол.docx"})
    cfg = {**CFG, "weeek_set_protocol_field": False}
    scheduler._wipe_inherited("alice", cfg, [_meeting(api, 800, 60)])

    assert api.value(800, VIDEO) == ""
    assert api.value(800, PROTO) == "https://вручную/протокол.docx"


# --------------------------------------------------------------------------- #
# Защита поля со ссылкой на Телемост
# --------------------------------------------------------------------------- #
def test_защищённое_поле_по_имени(api):
    """Настройку перепутали и «Видео встречи» указывает на «Встреча». Поле
    ищется по ЧАСТИЧНОМУ вхождению имени, так что промахнуться легко."""
    api.add(900, {"Встреча": TELEMOST})
    cfg = {**CFG, "weeek_video_field": "Встреча"}

    done, errors = scheduler._wipe_inherited("alice", cfg, [_meeting(api, 900, 60)])

    assert (done, errors) == (0, 1)
    assert api.writes == [] and api.value(900, "Встреча") == TELEMOST


def test_защищённое_поле_по_ссылке_на_телемост(api):
    """Вторая страховка: поле переименовали и в список защищённых добавить
    забыли, но ссылка на Телемост в значении видна всегда."""
    api.add(901, {VIDEO: TELEMOST})
    done, errors = scheduler._wipe_inherited("alice", CFG, [_meeting(api, 901, 60)])

    assert (done, errors) == (0, 1)
    assert api.writes == [] and api.value(901, VIDEO) == TELEMOST


def test_защищённое_поле_не_пишется_и_при_доставке(api):
    """Тот же запрет на записи результата: ссылку на видео нельзя положить
    поверх ссылки на Телемост."""
    api.add(902, {"Встреча": TELEMOST})
    res = weeek.set_custom_field("tok", 902, "Встреча", "https://cloud/новое.mp4",
                                 protected=["Встреча"])

    assert res["ok"] is False and res["protected"] is True
    assert api.writes == [] and api.value(902, "Встреча") == TELEMOST


def test_защита_включена_по_умолчанию(api):
    """Настройку могли не сохранить (старый файл настроек) — поле «Встреча»
    обязано уцелеть и без списка."""
    api.add(903, {"Встреча": TELEMOST})
    res = weeek.clear_custom_field("tok", 903, "Встреча")

    assert res["ok"] is False and res["protected"] is True
    assert api.writes == []


# --------------------------------------------------------------------------- #
# Стоимость и устойчивость
# --------------------------------------------------------------------------- #
def test_пустые_поля_не_стоят_запросов(api):
    """Т16. Значения полей уже прочитаны опросом, писать в пустое поле незачем —
    ни одного запроса вообще."""
    api.add(1000, {"Встреча": TELEMOST, VIDEO: "", PROTO: ""})
    done, errors = scheduler._wipe_inherited("alice", CFG,
                                             [_meeting(api, 1000, 60)])

    assert (done, errors) == (0, 0)
    assert api.writes == [] and api.reads == 0


def test_повторный_проход_не_пишет(api):
    """Т17. Второй проход подряд не делает ни одного изменяющего запроса."""
    api.add(1100, {VIDEO: "https://cloud/прошлое.mp4",
                   PROTO: "https://cloud/прошлый.docx"})
    scheduler._wipe_inherited("alice", CFG, [_meeting(api, 1100, 60)])
    writes_after_first = len(api.writes)

    scheduler._wipe_inherited("alice", CFG, [_meeting(api, 1100, 60)])

    assert writes_after_first == 2 and len(api.writes) == 2


def test_поля_нет_в_задаче_это_не_ошибка(api):
    """Т18. Weeek может вовсе не отдавать пустое поле — ровно то же состояние
    наступает ПОСЛЕ удачной очистки."""
    api.add(1200, {"Встреча": TELEMOST})
    done, errors = scheduler._wipe_inherited("alice", CFG,
                                             [_meeting(api, 1200, 60)])

    assert (done, errors) == (0, 0) and api.writes == []


def test_самопроверка_ловит_проглоченную_очистку(api):
    """Заказчик просил убедиться, что поле ДЕЙСТВИТЕЛЬНО опустело. Weeek
    отвечает 200 и на то, что молча игнорирует; без перечитывания мы считали
    бы, что убрали ссылку, а человек продолжал бы видеть её в задаче."""
    api.add(1300, {VIDEO: "https://cloud/прошлое.mp4"})
    api.swallow = True

    done, errors = scheduler._wipe_inherited("alice", CFG,
                                             [_meeting(api, 1300, 60)])

    assert (done, errors) == (0, 1)
    res = weeek.clear_custom_field("tok", 1300, VIDEO)
    assert res["ok"] is False and res["verified"] is False
    assert "осталось" in res["error"]


def test_очистка_проверяется_перечитыванием(api):
    """Обратная сторона той же проверки: удачная очистка отмечена verified."""
    api.add(1301, {VIDEO: "https://cloud/прошлое.mp4"})
    res = weeek.clear_custom_field("tok", 1301, VIDEO)

    assert res == {"ok": True, "verified": True, "was": "https://cloud/прошлое.mp4",
                   "field_id": "1301-0"}


def test_сбой_weeek_виден_вызывающему(api, monkeypatch):
    """`wipe_stale_links` возвращала None, и о провале никто не знал."""
    api.add(1400, {VIDEO: "https://cloud/прошлое.mp4"})

    def boom(method, path, token, params=None, body=None, timeout=30):
        if method == "PUT":
            raise weeek.WeeekError("Weeek API 429: too many requests")
        return api.request(method, path, token, params, body, timeout)

    monkeypatch.setattr(weeek, "_request", boom)
    res = sched_mod.delivery.wipe_stale_links(1400, CFG, lambda _m: None,
                                              task=api.tasks["1400"])

    assert res["ok"] is False and res["cleared"] == [] and res["errors"]


def test_за_один_проход_не_больше_лимита(api, monkeypatch):
    """Т21. Проход идёт в главном цикле планировщика: сотня записей подряд
    заняла бы его минутами и собрала бы 429. Остальное — следующему опросу."""
    monkeypatch.setattr(sched_mod, "_WIPE_MAX_PER_POLL", 3)
    meetings = []
    for tid in range(2000, 2010):
        api.add(tid, {VIDEO: "https://cloud/прошлое.mp4"})
        meetings.append(_meeting(api, tid, 60))

    done, errors = scheduler._wipe_inherited("alice", CFG, meetings)

    assert (done, errors) == (3, 0)
    assert len({t for t, _ in api.writes}) == 3


# --------------------------------------------------------------------------- #
# Проводка: чистку делает обычный опрос, а не только ночной проход
# --------------------------------------------------------------------------- #
def test_опрос_снимает_унаследованные_ссылки(api, monkeypatch):
    """Т4: не позже одного интервала опроса после того, как задача с будущей
    датой и непустыми полями впервые попалась на глаза. Раньше ссылки жили до
    ночи перед встречей — это и была жалоба."""
    api.add(3000, {"Встреча": TELEMOST, VIDEO: "https://cloud/прошлое.mp4"})
    monkeypatch.setattr(sched_mod.weeek, "upcoming_meetings",
                        lambda *a, **k: [_meeting(api, 3000, 60 * 24)])

    scheduler._poll("alice", CFG)

    assert api.value(3000, VIDEO) == ""
    assert api.value(3000, "Встреча") == TELEMOST


def test_отложенная_доставка_пишет_в_свою_задачу():
    """Копия задачи создаётся после закрытия оригинала — и может появиться
    раньше, чем бот допишет ссылку на протокол. Поздняя доставка обязана
    уходить в ТУ задачу, по которой шла запись (st.task_id), а не в
    свежесозданную копию."""
    import inspect
    src = inspect.getsource(sched_mod.Scheduler._late_upload)
    assert "st.task_id" in src and "m.task_id" not in src
    # Ссылку на протокол пишет сама задача распознавания — по id, снятому в
    # момент запуска записи.
    run_src = inspect.getsource(sched_mod.Scheduler._run)
    assert "deliver_weeek_task=str(st.task_id)" in run_src


def test_список_защищённых_полей_строкой(api):
    """Настройку могли поправить руками и написать строкой. Перебор строки даёт
    БУКВЫ — защищённым оказалось бы каждое поле, и бот молча перестал бы писать
    ссылки вовсе."""
    api.add(904, {VIDEO: "https://cloud/прошлое.mp4", "Встреча": TELEMOST})
    cfg = {**CFG, "weeek_protected_fields": "Встреча; Созвон"}

    done, errors = scheduler._wipe_inherited("alice", cfg, [_meeting(api, 904, 60)])

    assert (done, errors) == (1, 0)
    assert api.value(904, VIDEO) == "" and api.value(904, "Встреча") == TELEMOST


def test_похожее_имя_поля_не_считается_защищённым(api):
    """Защита по имени — ТОЧНОЕ совпадение, а не вхождение.

    Первая версия сравнивала вхождением, и защищённое «Встреча» поглощало
    «Видео встреча» (другой падеж) и «Встреча — видео». Бот МОЛЧА переставал
    писать ссылки на запись: ошибки нет, поле пустое, человек ничего не знает.
    От переименования поля страхует проверка по содержимому (тест выше), а по
    имени бьём только точно.
    """
    from app.automation.weeek import _protection_reason
    for name in ("Видео встреча", "Встреча — видео", "Видео встречи"):
        assert _protection_reason({"name": name, "value": ""}, ["Встреча"]) is None, name
    assert _protection_reason({"name": "Встреча", "value": ""}, ["Встреча"])
    assert _protection_reason({"name": " встреча ", "value": ""}, ["Встреча"])
