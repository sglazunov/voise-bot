"""Вопрос по встрече: что уходит модели, когда ключевые слова вопроса
в расшифровке не нашлись (ревью RAG 30.10).

Было: окна расшифровки ранжировались по основам слов длиннее трёх букв, и при
нулевом совпадении в промпт всё равно попадало ПЕРВОЕ окно (40 строк) —
проверка «ничего не нашли» стояла после добавления. Модель отвечала по
случайному началу встречи. Отдельно: «что по ОД?» и «срок CRM?» не давали ни
одной основы (слова короче четырёх букв отбрасывались), то есть именно вопросы
про проекты с короткими названиями искали хуже всего.

Стало — три исхода:
  * нашлись окна по основам или по целым коротким словам → только они;
  * слова для поиска есть, совпадений нет → расшифровка целиком (в бюджет), и
    модель решает сама: общий вопрос («о чём говорили?») получает ответ, а
    вопрос про отсутствующую тему — честное «не обсуждалось»;
  * искать не по чему (одни служебные слова) → просьба уточнить, без вызова.
"""
from app import analyze


class _Fake:
    name = "fake"

    def __init__(self, captured: dict, answer: str = "ответ"):
        self.captured = captured
        self.answer = answer

    def complete(self, prompt, max_tokens=2000, force_json=True, **kw):
        self.captured["prompt"] = prompt
        self.captured["calls"] = self.captured.get("calls", 0) + 1
        return self.answer


def _transcript() -> str:
    return "\n".join(
        [f"[00:{i:02d}] реплика про погоду номер {i}" for i in range(50)]
        + ["[02:50] Мария: по ОД договорились — продукт отправляем в пятницу"]
        + [f"[03:{i:02d}] реплика про отпуск номер {i}" for i in range(50)]
        + ["[04:40] Иван: срок по CRM — конец месяца"])


def _patch(monkeypatch, captured, answer="ответ"):
    monkeypatch.setattr(analyze.llm, "get_provider_chain",
                        lambda *a, **k: _Fake(captured, answer))


def test_short_project_name_is_matched_as_whole_word(monkeypatch):
    captured = {}
    _patch(monkeypatch, captured)
    analyze.ask_meeting(_transcript(), "что по ОД?")
    prompt = captured["prompt"]
    assert "[02:50]" in prompt                 # окно с «ОД» найдено
    assert "[00:00]" not in prompt             # начало встречи не подсунуто
    assert "[03:30]" not in prompt             # «прОДукт»/«отпуск» — не «ОД»


def test_latin_abbreviation_is_matched(monkeypatch):
    captured = {}
    _patch(monkeypatch, captured)
    analyze.ask_meeting(_transcript(), "срок CRM?")
    assert "[04:40]" in captured["prompt"]
    assert "[00:00]" not in captured["prompt"]


def test_no_match_sends_whole_transcript_not_first_window(monkeypatch):
    captured = {}
    _patch(monkeypatch, captured)
    analyze.ask_meeting(_transcript(), "что решили по бюджету маркетинга?")
    prompt = captured["prompt"]
    # Раньше сюда попадали только первые 40 строк; теперь вся встреча.
    assert "[00:00]" in prompt and "[04:40]" in prompt
    assert "не обсуждалось" in prompt           # инструкция честно признать


def test_general_question_without_stems_gets_whole_transcript(monkeypatch):
    captured = {}
    _patch(monkeypatch, captured)
    analyze.ask_meeting(_transcript(), "о чём говорили?")
    assert "[04:40]" in captured["prompt"]


def test_question_without_searchable_words_does_not_call_model(monkeypatch):
    captured = {}
    _patch(monkeypatch, captured)
    ans = analyze.ask_meeting(_transcript(), "ну и что?")
    assert captured.get("calls", 0) == 0
    assert "уточните" in ans.lower()


def test_pick_windows_reports_outcome():
    text = _transcript()
    picked, how = analyze._pick_windows(text, "что по ОД?", 100_000)
    # Окна идут с перекрытием, реплика может попасть в два соседних — но в
    # КАЖДОМ выбранном окне она есть, лишних окон нет.
    assert how == "hit" and 1 <= len(picked) <= 2
    assert all("[02:50]" in c for c in picked)
    picked, how = analyze._pick_windows(text, "бюджет маркетинга", 100_000)
    assert how == "miss" and "\n".join(picked).count("[") > 60
    picked, how = analyze._pick_windows(text, "ну и что", 100_000)
    assert how == "empty" and picked == []
