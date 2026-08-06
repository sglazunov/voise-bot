"""Пустая расшифровка не превращается в выдуманный протокол.

Боевой случай: из 45 протоколов 10 были собраны по записям, где речь не
распозналась вовсе. Протокол при этом выглядел достоверно — с темами, задачами
и фамилиями. Причина: в анализ уходит не только расшифровка, но и блоки
«УЧАСТНИКИ ЗВОНКА» (имена с плиток Телемоста) и «ПОСТОЯННЫЙ КОНТЕКСТ» (проекты
команды). Модель получала реальные имена и реальные темы — и дописывала встречу
целиком.

Тесты фиксируют МЕХАНИКУ: счёт слов игнорирует служебные блоки и таймкоды,
гейт срабатывает ДО обращения к движку, а настоящая встреча проходит.
"""
import pytest

from app import analyze
from app.analyze import (MIN_SPEECH_WORDS, NoTranscript, ensure_analysable,
                         speech_words)


# Ровно то, что уходило в модель при неудачной записи.
FAILED_RECORDING = """Транскрипция недоступна.

=== УЧАСТНИКИ ЗВОНКА (распознано с видео) ===
Кирилл Дроздков, Наталья, Сергей Глазунов

=== ПОСТОЯННЫЙ КОНТЕКСТ ===
Проект ЭМО: редизайн карточки товара, тренажёры, доступность интерфейса.
Кирилл — дизайн, Наташа — разработка функционала модуля.
"""


def _real_meeting(lines: int = 8) -> str:
    return "\n".join(
        f"[00:{i:02d}] Иван: обсудим сроки по выгрузке отчётов и кто возьмёт задачу"
        for i in range(lines))


def test_служебные_блоки_не_считаются_речью():
    """Имена и контекст — это подсказки, а не сказанное на встрече."""
    assert speech_words(FAILED_RECORDING) < 5


def test_таймкоды_не_считаются_речью():
    assert speech_words("[00:01]\n[00:02]\n[01:03]") == 0


def test_пустая_запись_блокируется():
    with pytest.raises(NoTranscript) as e:
        ensure_analysable(FAILED_RECORDING)
    # Сообщение должно объяснять причину и что делать, а не быть трейсбеком.
    assert "не распознано речи" in str(e.value)
    assert "звук" in str(e.value).lower()


def test_настоящая_встреча_проходит():
    ensure_analysable(_real_meeting())  # не бросает


def test_гейт_срабатывает_до_обращения_к_движку(monkeypatch):
    """Главное свойство: движок не должен вызываться вовсе — иначе останется
    шанс, что он сочинит протокол."""
    called = []
    monkeypatch.setattr(analyze.llm, "get_provider_chain",
                        lambda *a, **kw: called.append(1))
    with pytest.raises(NoTranscript):
        analyze.analyze_transcript(FAILED_RECORDING)
    assert not called, "движок не должен вызываться при пустой расшифровке"


def test_порог_настраивается(monkeypatch):
    """Порог держится в одном месте и его видно в сообщении."""
    with pytest.raises(NoTranscript) as e:
        ensure_analysable("слово " * (MIN_SPEECH_WORDS - 1))
    assert str(MIN_SPEECH_WORDS) in str(e.value)
    ensure_analysable("слово " * MIN_SPEECH_WORDS)  # ровно порог — уже можно


class TestМалоРечи:
    """Замер 06.08: встреча 04.08 10:00 — четыре часа записи, 118 слов речи
    (люди поздоровались и разошлись) — и полноценный с виду протокол на 241
    слово. Блокировать такое нельзя (человек хочет видеть, что было), но и
    выдавать за полноценный итог — обман."""

    def test_короткий_разговор_помечается(self):
        from app import analyze
        assert analyze.speech_words("привет " * 118) < analyze.THIN_SPEECH_WORDS

    def test_нормальная_встреча_не_помечается(self):
        from app import analyze
        assert analyze.speech_words("слово " * 3000) >= analyze.THIN_SPEECH_WORDS

    def test_порог_выше_порога_блокировки(self):
        """Иначе пометка недостижима: всё, что ниже, уже отклонено."""
        from app import analyze
        assert analyze.THIN_SPEECH_WORDS > analyze.MIN_SPEECH_WORDS
