"""Этап 7: пресеты (Д11), правки протокола и Q&A-ретрив (Д13), метрики Д12/Д15.
LLM фейковый или не нужен; проверяется механика.
"""
import json

from app import analyze
from app.analyze import preset_for_title, preset_rules
from app.jobs import store


class TestPresets:
    def test_auto_detect_by_title(self):
        assert preset_for_title("Еженедельная планёрка отдела") == "planerka"
        assert preset_for_title("Стендап разработчиков") == "planerka"
        assert preset_for_title("Демо для заказчика") == "demo"
        assert preset_for_title("1:1 Сергей/Мария") == "one_on_one"
        assert preset_for_title("Дизайн карточки товара") == "design"
        assert preset_for_title("Встреча по бюджету") == "universal"

    def test_rules_nonempty_for_builtins(self):
        for name in ("planerka", "design", "demo", "one_on_one"):
            assert len(preset_rules(name)) > 50
        assert preset_rules("universal") == ""
        assert preset_rules("несуществующий") == ""

    def test_custom_preset_wins(self):
        assert preset_rules("planerka", {"planerka": "свои правила"}) == "свои правила"


class TestUpdateAnalysis:
    def _job_with_analysis(self):
        job = store.create(filename="m.mp4", audio_path="/tmp/m.mp4",
                           language="ru", diarize=False, owner="alice")
        job.analysis = {
            "summary": "старое", "detailed": [{"topic": "Т1", "details": "Д1"}],
            "decisions": ["старое решение"], "key_thoughts": [], "conclusions": [],
            "tasks": [{"task": "A", "owner": "Кирилл"}], "minor_tasks": [],
            "done_tasks": [], "participants": [],
            "verification": {"mode": "strict", "tasks": [{"ok": True}]},
        }
        job.status = "done"
        # json result for docx rebuild
        store.result_path(job.id, "json").write_text(
            json.dumps({"meta": {}, "segments": []}), encoding="utf-8")
        return job

    def test_edits_applied_and_verification_dropped(self):
        job = self._job_with_analysis()
        store.update_analysis(job.id, {
            "summary": "новое резюме",
            "tasks": [{"task": "B", "owner": ""}],
            "decisions": ["новое решение", ""],
        })
        a = store.get(job.id).analysis
        assert a["summary"] == "новое резюме"
        assert a["tasks"] == [{"task": "B", "owner": ""}]
        assert a["decisions"] == ["новое решение"]
        assert a["detailed"] == [{"topic": "Т1", "details": "Д1"}]  # untouched
        assert "verification" not in a          # правки человека доверенные
        assert a["_edited"] is True
        assert job.docx_providers               # docx re-rendered

    def test_no_analysis_rejected(self):
        job = store.create(filename="x.mp4", audio_path="/tmp/x.mp4",
                           language="ru", diarize=False, owner="alice")
        try:
            store.update_analysis(job.id, {"summary": "x"})
            raise AssertionError("должно было отказать")
        except ValueError:
            pass


class TestAskRetrieval:
    def test_relevant_window_selected(self, monkeypatch):
        captured = {}

        class Fake:
            name = "fake"
            def complete(self, prompt, max_tokens=2000, force_json=True, **kw):
                captured["prompt"] = prompt
                return "Решили оставить пять фильтров [02:50]."
        monkeypatch.setattr(analyze.llm, "get_provider_chain", lambda *a, **k: Fake())
        transcript = "\n".join(
            [f"[00:{i:02d}] реплика про погоду номер {i}" for i in range(50)]
            + ["[02:50] Мария: решили — оставляем пять базовых фильтров карточки"]
            + [f"[03:{i:02d}] реплика про отпуск номер {i}" for i in range(50)])
        ans = analyze.ask_meeting(transcript, "что решили по фильтрам карточки?")
        assert "[02:50]" in ans
        assert "фильтр" in captured["prompt"]   # релевантное окно попало в промпт


class TestParticipantsFromTiles:
    def _job(self, tiles):
        job = store.create(filename="m.mp4", audio_path="/tmp/m.mp4",
                           language="ru", diarize=False, owner="alice")
        job.video_participants = tiles
        return job

    def test_mentioned_people_cannot_join_participants(self):
        job = self._job(["Сергей Глазунов", "Мария Н"])
        result = {"participants": [
            {"name": "Сергей Глазунов", "role": "тимлид"},
            {"name": "Мария Н", "role": ""},
            {"name": "Вася Пупкин", "role": "подрядчик"},   # о нём лишь ГОВОРИЛИ
        ]}
        out = store._enforce_participants(job, result)
        names = [p["name"] for p in out["participants"]]
        assert names == ["Сергей Глазунов", "Мария Н"]
        assert out["participants"][0]["role"] == "тимлид"   # роль сохранена

    def test_role_matched_by_partial_name(self):
        job = self._job(["Сергей Глазунов"])
        out = store._enforce_participants(job, {"participants": [
            {"name": "Сергей", "role": "ведущий встречи"}]})
        assert out["participants"] == [{"name": "Сергей Глазунов",
                                        "role": "ведущий встречи"}]

    def test_no_tiles_keeps_model_list(self):
        job = self._job([])
        result = {"participants": [{"name": "Спикер 1", "role": ""}]}
        assert store._enforce_participants(job, result) is result

    def test_tile_garbage_not_promoted(self):
        job = self._job(["Мария Н", "Стоп запись", "кирилл 6"])
        out = store._enforce_participants(job, {"participants": []})
        assert [p["name"] for p in out["participants"]] == ["Мария Н"]
