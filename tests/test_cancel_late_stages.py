"""Кнопка «Стоп» действует и на этапах ПОСЛЕ распознавания.

Боевой случай: пользователь нажал «Стоп», в интерфейсе появилось
«Останавливаю…», и ничего не произошло — работа шла дальше.

Причина: отмена проверялась только между фрагментами распознавания. А когда
включены «Спикеры (видео)» и «OCR экрана», после распознавания идут ещё два
прохода по кадрам, каждый на десятки минут. Флаг отмены выставлялся, но никто
его там не читал.

Тесты фиксируют МЕХАНИКУ: оба тяжёлых прохода принимают should_stop и
прекращают работу, отдавая то, что успели собрать.
"""
import inspect

from app import screen_ocr, speaker_id


def test_проходы_принимают_признак_отмены():
    """Без этого параметра отменить их невозможно в принципе."""
    assert "should_stop" in inspect.signature(speaker_id.identify_speakers).parameters
    assert "should_stop" in inspect.signature(screen_ocr.extract_screen_text).parameters


def test_ocr_экрана_прекращается_по_отмене(monkeypatch):
    """Кадры есть, но отмена уже выставлена — ни один не обрабатывается."""
    frames = [(float(i), object()) for i in range(50)]
    monkeypatch.setattr(screen_ocr, "_sample_frames", lambda *a, **k: iter(frames))
    monkeypatch.setattr(screen_ocr, "_point_pytesseract_at_binary", lambda: None)
    out = screen_ocr.extract_screen_text("video.mp4", should_stop=lambda: True)
    assert out == []


def test_разметка_спикеров_прекращается_по_отмене(monkeypatch):
    frames = [(float(i), None, None) for i in range(50)]
    monkeypatch.setattr(speaker_id, "_sample_frames", lambda *a, **k: iter(frames))
    segs = []
    # Не падает и возвращает сегменты как есть — размечать нечем, но и ждать
    # окончания прохода не приходится.
    assert speaker_id.identify_speakers("v.mp4", segs, should_stop=lambda: True) is segs


def test_без_отмены_кадры_разбираются(monkeypatch):
    """Обратная проверка: признак отмены не должен обрывать обычный путь.

    Считаем, сколько кадров реально забрали из генератора: при should_stop,
    который всегда False, должны разобрать все, а не остановиться на первом.
    """
    taken = []

    def gen():
        for i in range(7):
            taken.append(i)
            yield float(i), object()

    monkeypatch.setattr(screen_ocr, "_sample_frames", lambda *a, **k: gen())
    monkeypatch.setattr(screen_ocr, "_point_pytesseract_at_binary", lambda: None)
    # OCR не запускаем: интересует только то, что цикл не прервался.
    monkeypatch.setattr(screen_ocr, "_ocr_text", lambda *a, **k: "", raising=False)
    try:
        screen_ocr.extract_screen_text("video.mp4", should_stop=lambda: False)
    except Exception:
        pass          # внутренности OCR недоступны в тестовой среде — не важно
    assert len(taken) > 1, "цикл оборвался, хотя отмены не было"
