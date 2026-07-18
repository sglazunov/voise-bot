"""Д16: метрики эталонного теста — матчинг задач по схожести, owner-точность,
recall решений и покрытие тем (чистая функция evaluate_case)."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "run_eval", Path(__file__).parent / "eval" / "run_eval.py")
run_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_eval)
evaluate_case = run_eval.evaluate_case


REF = {
    "tasks": [{"task": "Выкатить теги на прод до среды", "owner": "Кирилл"},
              {"task": "Свести макеты карточки к пятнице", "owner": "Мария"},
              {"task": "Переименовать тег «новинка» в «новое»", "owner": None}],
    "done_tasks": [],
    "decisions": ["Оставить пять базовых фильтров"],
    "topics": ["Фильтры карточки товара"],
}


def _proto(tasks=None, minor=None, decisions=None, detailed=None):
    return {"tasks": tasks or [], "minor_tasks": minor or [],
            "done_tasks": [], "decisions": decisions or [],
            "detailed": detailed or []}


class TestTaskMatching:
    def test_paraphrased_tasks_match(self):
        m = evaluate_case(_proto(
            tasks=[{"task": "Выкатка тегов на прод (до среды)", "owner": "Кирилл"},
                   {"task": "Финальные макеты карточки — свести к пятнице", "owner": "Мария"}],
            minor=[{"task": "Переименование тега «новинка» → «новое»", "owner": ""}],
            decisions=["Решили оставить пять базовых фильтров"],
            detailed=[{"topic": "Карточка товара", "details": "сколько фильтров оставить"}]),
            REF)
        assert m["task_recall"] == 1.0
        assert m["owner_accuracy"] == 1.0     # пустой owner == эталонный null
        assert m["decisions_recall"] == 1.0
        assert m["topics_coverage"] == 1.0

    def test_missed_task_lowers_recall(self):
        m = evaluate_case(_proto(
            tasks=[{"task": "Выкатить теги на прод", "owner": "Кирилл"}]), REF)
        assert m["task_recall"] == round(1 / 3, 3)
        assert len(m["missed_tasks"]) == 2

    def test_wrong_owner_lowers_accuracy(self):
        m = evaluate_case(_proto(
            tasks=[{"task": "Выкатить теги на прод до среды", "owner": "Мария"}]), REF)
        assert m["owner_accuracy"] == 0.0
        assert m["wrong_owner"][0]["ref"] == "Кирилл"

    def test_invented_owner_on_unassigned_task_is_error(self):
        m = evaluate_case(_proto(
            tasks=[{"task": "Переименовать тег «новинка» в «новое»", "owner": "Виталий"}]), REF)
        assert m["owner_accuracy"] == 0.0     # эталон null, модель выдумала

    def test_unrelated_task_does_not_match(self):
        m = evaluate_case(_proto(
            tasks=[{"task": "Провести аудит SEO сайта", "owner": None}]), REF)
        assert m["task_recall"] == 0.0
        assert m["task_precision"] == 0.0

    def test_owner_matched_by_first_name(self):
        m = evaluate_case(_proto(
            tasks=[{"task": "Выкатить теги на прод до среды", "owner": "Кирилл С."}]), REF)
        assert m["owner_accuracy"] == 1.0
