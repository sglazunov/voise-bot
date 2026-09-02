"""Память серий встреч: ключ серии, карточка, память о прошлой встрече, блок
для промпта (app/meeting_series.py)."""
from app import meeting_series as ms


def test_series_key_strips_date_time_and_extension():
    assert ms.series_key("28.08.2026, 16-00. - Встреча лидеров.mp4") == "встреча лидеров"
    assert ms.series_key("Встреча лидеров") == "встреча лидеров"
    assert ms.series_key("14.08.2026, 13-30. - Встреча лидеров - протокол.docx") == "встреча лидеров"
    assert ms.series_key("27.08.2026, 13-30. - Виртуальный помощник (2).mp4") == "виртуальный помощник"
    assert ms.series_key("ОД сайт, редизайн, магазин - проектная встреча") == \
        "од сайт редизайн магазин проектная встреча"
    assert ms.series_key("") == ""


def test_display_title_and_date():
    t = "28.08.2026, 16-00. - Встреча лидеров.mp4"
    assert ms.display_title(t) == "Встреча лидеров"
    assert ms.date_from_title(t) == "28.08.2026"
    assert ms.date_from_title("2026-08-28 16:00 — Гранты - внутренняя") == "28.08.2026"
    assert ms.date_from_title("Гранты - внутренняя") == ""


def test_find_matches_related_series_by_word_prefix():
    ms.upsert("alice", "Встреча лидеров", {"context": "Лидеры направлений."})
    key, e = ms.find("alice", "26.08.2026, 16-00. - Встреча лидеров - стратегия.mp4")
    assert key == "встреча лидеров" and e and e["context"] == "Лидеры направлений."
    # Не родственник: «встреча» — не граница слова в «встречалидеров».
    key2, e2 = ms.find("alice", "Планёрка дизайнеров")
    assert key2 == "планерка дизайнеров" and e2 is None


def _analysis():
    return {
        "summary": "Обсудили редизайн сайта и сроки запуска магазина.",
        "decisions": ["Запускаем магазин 15 сентября", "Логотип оставляем прежний"],
        "tasks": [{"task": "Подготовить макет главной", "owner": "Алсу"},
                  {"task": "Выдуманная задача", "owner": "—"}],
        "minor_tasks": [{"task": "Скинуть ссылку на Figma", "owner": ""}],
        "done_tasks": [{"task": "Календарь сверстан", "owner": "Сергей Бескопыльный"}],
        "participants": [{"name": "Зоя Р", "role": ""}, {"name": "Алсу", "role": "дизайнер"}],
        "verification": {
            "decisions": [{"ok": True}, {"ok": False}],
            "tasks": [{"ok": True}, {"ok": False}],
            "minor_tasks": [{"ok": True}],
            "done_tasks": [{"ok": True}],
        },
    }


def test_remember_keeps_only_verified_points_and_builds_block():
    key = ms.remember("alice", "24.08.2026, 09-00. - ОД сайт, редизайн, магазин.mp4",
                      _analysis(), job_id="j1")
    assert key == "од сайт редизайн магазин"
    e = ms.load("alice")[key]
    assert e["last"]["date"] == "24.08.2026"
    assert e["last"]["decisions"] == ["Запускаем магазин 15 сентября"]
    assert [t["task"] for t in e["last"]["tasks"]] == \
        ["Подготовить макет главной", "Скинуть ссылку на Figma"]
    assert e["last"]["tasks"][0]["owner"] == "Алсу"
    assert e["history"] and e["history"][-1]["job_id"] == "j1"

    blk = ms.block_for("alice", "31.08.2026, 09-00. - ОД сайт, редизайн, магазин.mp4")
    assert ms.LAST_HEADER in blk
    assert "Запускаем магазин 15 сентября" in blk
    assert "Выдуманная задача" not in blk
    assert "Подготовить макет главной — Алсу" in blk
    # Карточку человек не заполнял — блока серии нет, только память.
    assert ms.SERIES_HEADER not in blk


def test_remember_rebuild_overwrites_not_duplicates_and_older_does_not_win():
    title = "24.08.2026, 09-00. - Гранты - внутренняя.mp4"
    ms.remember("alice", title, _analysis(), job_id="j1")
    ms.remember("alice", title, _analysis(), job_id="j1")      # пересборка
    e = ms.load("alice")["гранты внутренняя"]
    assert len(e["history"]) == 1
    # Более свежая встреча
    ms.remember("alice", "31.08.2026, 10-00. - Гранты - внутренняя.mp4",
                {"summary": "новее", "decisions": ["Новое решение"]}, job_id="j2")
    # Пересборка СТАРОЙ не должна затереть память о новой
    ms.remember("alice", title, _analysis(), job_id="j1")
    e = ms.load("alice")["гранты внутренняя"]
    assert e["last"]["job_id"] == "j2" and e["last"]["summary"] == "новее"
    assert len(e["history"]) == 2


def test_upsert_context_pinned_preset_priority_and_prompt_block():
    ms.upsert("alice", "Встреча лидеров",
              {"context": "Проект: управление компанией. Зоя Р — руководитель.",
               "preset": "leaders", "priority": "urgent", "weeek_project_id": "42"})
    assert ms.pinned_preset("alice", "28.08.2026, 16-00. - Встреча лидеров.mp4") == "leaders"
    assert ms.priority_of("alice", "Встреча лидеров") == "urgent"
    blk = ms.block_for("alice", "Встреча лидеров")
    assert blk.startswith(ms.SERIES_HEADER)
    assert "Зоя Р — руководитель" in blk
    ui = ms.list_for_ui("alice")
    assert ui[0]["key"] == "встреча лидеров" and ui[0]["weeek_project_id"] == "42"
    assert ms.forget_last("alice", "встреча лидеров") is True
    assert ms.delete("alice", "встреча лидеров") is True
    assert ms.find("alice", "Встреча лидеров")[1] is None


def test_series_is_shared_within_team(client):
    """Карточка серии — общая для команды, как и ai_context."""
    from tests.conftest import register
    from app import security
    assert register(client, "admin1", "password123", phone="+7 999 111-11-11").status_code < 300
    code = security.invite_code_of("admin1")
    r = register(client, "member1", "password123", code=code, phone="+7 999 222-22-22")
    assert r.status_code < 300, r.text
    ms.upsert("admin1", "Встреча лидеров", {"context": "общая карточка"})
    assert ms.find("member1", "Встреча лидеров")[1]["context"] == "общая карточка"
