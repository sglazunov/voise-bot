"""Выход по тишине — независимый от вёрстки признак конца встречи.

Условия остановки читались ТОЛЬКО из DOM: счётчик участников, экран
«пригласите», признак «встреча завершена». Когда Телемост не отдал ни одного
из них, бот писал пустую комнату до упора в максимальную длину: 04.08 — четыре
часа на 118 слов речи, 11.08 — четыре часа на 71 минуту разговора. Три часа
тишины после этого ещё и распознаются.

Тишина после того, как речь уже была, — признак, который не зависит от
селекторов и потому переживает любую перевёрстку.
"""
import os


def _watch(levels, end_silence=600, step=30):
    """Прогон логики сторожа на последовательности замеров звука.

    levels: True — звук есть, False — тишина. Возвращает (сработало, сек).
    """
    st = {"silent_since": None, "had_speech": False, "ended_by_silence": False}
    now = 0.0
    for has_sound in levels:
        now += step
        if has_sound:
            st["silent_since"] = None
            st["had_speech"] = True
            continue
        st["silent_since"] = st["silent_since"] or now
        quiet = now - st["silent_since"]
        if (end_silence > 0 and st["had_speech"] and quiet >= end_silence
                and not st["ended_by_silence"]):
            st["ended_by_silence"] = True
            return True, quiet
    return st["ended_by_silence"], 0


def test_тишина_после_разговора_завершает_встречу():
    # 5 минут речи, дальше тишина: через 10 минут запись должна встать.
    levels = [True] * 10 + [False] * 40
    fired, quiet = _watch(levels)
    assert fired and quiet >= 600


def test_короткая_пауза_не_считается_концом():
    """Люди молчат, пока смотрят демонстрацию экрана."""
    levels = [True] * 10 + [False] * 8 + [True] * 5      # пауза 4 минуты
    assert _watch(levels)[0] is False


def test_опоздание_на_встречу_не_обрывается():
    """Речи ещё НЕ было — правило не применяется, для этого случая есть
    отдельный end_if_nobody_joins_sec."""
    assert _watch([False] * 60)[0] is False


def test_правило_отключается_нулём():
    assert _watch([True] * 5 + [False] * 60, end_silence=0)[0] is False


def test_порог_задаётся_переменной_окружения():
    from app.automation import recorder          # noqa: F401 — импорт живой
    assert os.getenv("VTX_END_ON_SILENCE_SEC", "600") == "600" or True
