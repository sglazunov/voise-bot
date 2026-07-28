"""Д3+Д4: чистая расшифровка — авто-initial_prompt (имена/проекты/термины,
приоритет именам, лимит ~224 токена) и анти-галлюцинации Whisper (фильтр
no_speech_prob/avg_logprob, схлопывание повторов, метрики в сегменте).
"""
from app import ai_context
from app.jobs import Job, store
from app.transcribe import Segment, _is_hallucination, collapse_repeats


def _seg(text, ns=None, lp=None, start=0.0):
    return Segment(start=start, end=start + 1, text=text,
                   no_speech_prob=ns, avg_logprob=lp)


# --------------------------------------------------------------------------- #
# Д4
# --------------------------------------------------------------------------- #
class TestHallucinationFilter:
    def test_silent_and_unconfident_is_dropped(self):
        assert _is_hallucination(_seg("Спасибо за просмотр!", ns=0.9, lp=-1.5))

    def test_confident_speech_kept_even_if_quietish(self):
        assert not _is_hallucination(_seg("реальная фраза", ns=0.7, lp=-0.3))

    def test_low_logprob_alone_is_kept(self):
        # Тихая, но реальная речь часто имеет низкий logprob — не режем без
        # второго признака (высокой вероятности тишины).
        assert not _is_hallucination(_seg("тихая фраза", ns=0.2, lp=-1.8))

    def test_old_segments_without_metrics_pass(self):
        assert not _is_hallucination(_seg("старый сегмент"))


class TestCollapseRepeats:
    def test_loop_of_five_collapses_to_one(self):
        segs = [_seg("Продолжение следует...", start=i) for i in range(5)]
        out = collapse_repeats(segs, threshold=3)
        assert len(out) == 1 and out[0].start == 0

    def test_double_is_kept(self):
        segs = [_seg("да", start=0), _seg("да", start=1), _seg("хорошо", start=2)]
        out = collapse_repeats(segs, threshold=3)
        assert [s.text for s in out] == ["да", "да", "хорошо"]

    def test_separated_repeats_untouched(self):
        segs = [_seg("ок", 0), _seg("пауза", 1), _seg("ок", 2), _seg("пауза", 3),
                _seg("ок", 4)]
        out = collapse_repeats(segs, threshold=3)
        assert len(out) == 5

    def test_metrics_survive_in_to_dict(self):
        d = _seg("x", ns=0.1, lp=-0.2).to_dict()
        assert d["no_speech_prob"] == 0.1 and d["avg_logprob"] == -0.2


# --------------------------------------------------------------------------- #
# Д3
# --------------------------------------------------------------------------- #
class TestKnownNames:
    def test_pairs_and_mid_sentence_names(self):
        ai_context.save("alice", {"global": (
            "Команда: Сергей Глазунов — тимлид, дизайном занимается Мария, "
            "бэкенд ведёт Кирилл. Начало предложения не имя."), "projects": []})
        names = ai_context.known_names("alice")
        assert "Сергей Глазунов" in names
        assert "Мария" in names and "Кирилл" in names
        assert "Начало" not in names  # sentence-start capital is not a name

    def test_empty_context(self):
        assert ai_context.known_names("nobody") == []


class TestAutoInitialPrompt:
    def _job(self, **kw):
        return Job(id="t1", filename="f.mp4", audio_path="", language="ru",
                   diarize=False, owner="alice", **kw)

    def test_priority_user_prompt_then_names_then_terms(self):
        ai_context.save("alice", {
            "global": "работает Сергей Глазунов и коллега Мария",
            "projects": [{"name": "Weeek-бот", "text": ""}]})
        p = store._auto_initial_prompt(self._job(
            initial_prompt="Моя ручная подсказка.",
            glossary="конид=Коновалов\nтелемост=Телемост"))
        assert p.startswith("Моя ручная подсказка.")
        assert "Сергей Глазунов" in p and "Мария" in p
        assert "Weeek-бот" in p
        assert "Коновалов" in p and "Телемост" in p

    def test_budget_cap_prefers_names_over_terms(self):
        ai_context.save("alice", {
            "global": "работает Сергей Глазунов и коллега Мария", "projects": []})
        huge_gloss = "\n".join(f"слово{i}=Термин{i:03d}Оченьдлинный" for i in range(200))
        p = store._auto_initial_prompt(self._job(glossary=huge_gloss))
        assert len(p) <= 224 * 3 + 40          # ~224 tokens (+labels)
        assert "Сергей Глазунов" in p          # names survived the squeeze
        assert "Термин199" not in p            # the tail of terms did not

    def test_tile_names_from_past_videos_included(self):
        job = store.create(filename="a.mp4", audio_path="/tmp/a.mp4",
                           language="ru", diarize=False, owner="alice")
        job.video_participants = ["Пётр Иванов"]
        job.status = "done"
        p = store._auto_initial_prompt(self._job())
        assert "Пётр Иванов" in p

    def test_never_raises_without_context(self):
        assert isinstance(store._auto_initial_prompt(self._job()), str)

    def test_tile_garbage_filtered_out(self):
        job = store.create(filename="a.mp4", audio_path="/tmp/a.mp4",
                           language="ru", diarize=False, owner="alice")
        job.video_participants = [
            "Мельников Алексей", "Стоп запись", "Войду, чтобы написать сообщение",
            "кирилл 6", "Удалено 3 сообщения", "Сегодня", "НВ Светлана",
            "https app.weeek.net ws 849372 t", "Мария Н"]
        p = store._auto_initial_prompt(self._job())
        assert "Мельников Алексей" in p and "НВ Светлана" in p and "Мария Н" in p
        for garbage in ("Стоп запись", "Войду", "кирилл 6", "Удалено", "Сегодня",
                        "weeek.net"):
            assert garbage not in p


class TestLooksLikeName:
    def test_shapes(self):
        ok = ["Сергей Глазунов", "Мария Н", "НВ Светлана", "Сергей Beck", "Елизавета"]
        bad = ["чат", "Стоп запись", "кирилл 6", "Войду, чтобы написать сообщение",
               "https app.weeek.net ws", "8 чат", "По макетам пока не понятно",
               "Сегодня", "Вчера", "Демонстрация", "English"]
        for s in ok:
            assert store._looks_like_name(s), s
        for s in bad:
            assert not store._looks_like_name(s), s


class TestTileNamesOcrCleanup:
    """Чистка подписей с плиток Телемоста.

    Все строки ниже — из списка участников боевого протокола (операционная
    23.07): там рядом стояли «Зоя Р», «Зоя P» и «ЗояР» — один человек тремя
    строками, — а также значки интерфейса, прочитанные OCR как буква «W».
    """

    def _names(self, captions):
        from app.jobs import _tile_names

        class _J:
            video_participants = captions

        return _tile_names(_J())

    def test_латинский_двойник_не_двоит_человека(self):
        names = self._names(["Зоя Р", "Зоя P", "ЗояР"])
        assert names == ["Зоя Р"], names

    def test_значок_интерфейса_снимается(self):
        assert self._names(["Виталий Овчаренко W"]) == ["Виталий Овчаренко"]
        assert self._names(["Павел. W"]) == ["Павел"]

    def test_слипшийся_инициал_отделяется(self):
        assert self._names(["МариянН"]) == ["Мариян Н"]

    def test_глагол_не_имя(self):
        assert self._names(["Развлекаешься"]) == []

    def test_настоящие_имена_не_портятся(self):
        src = ["Андрей Журавль", "НВ Светлана", "Дарья К", "Сергей Beck"]
        assert self._names(src) == src
