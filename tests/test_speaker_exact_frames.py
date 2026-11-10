"""Кто говорил: кадр берётся в момент реплики, а не на ключевом кадре.

Боевой случай (жалоба 10.11, «Аналитика тренажеров» 03.11): реплики Алексея
подписаны Виктором и наоборот. Разбор по записи: рекордер пишет 10 кадров в
секунду, у x264 ключевой кадр раз в 250 кадров — то есть раз в 25 секунд.
`_sample_frames` перематывал на момент t (`seek(backward=True)`) и брал
ПЕРВЫЙ декодированный кадр — а это ключевой кадр ДО t, до 25 секунд назад.
Все полтора-секундные выборки внутри этих 25 секунд видели один и тот же
кадр, и реплика доставалась тому, кто говорил на ключевом кадре.

Замер: метки боевого протокола совпали с симуляцией «один ключевой кадр на
25 с» в 479 фрагментах из 484, а с рамкой в момент реплики расходились в
каждом четвёртом.
"""
from types import SimpleNamespace

import numpy as np

from app import speaker_id as S
from app.transcribe import Segment


def _stream(times):
    return [(t, f"кадр@{t:g}") for t in times]


class TestВыборкаПоВремени:
    def test_берётся_первый_кадр_не_раньше_момента(self):
        frames = _stream([i / 10 for i in range(0, 61)])    # 6 с по 10 кадров
        got = list(S._pick_by_time(iter(frames), 1.5, 100))
        assert [t for t, _ in got] == [0.0, 1.5, 3.0, 4.5, 6.0]

    def test_время_выборки_это_время_кадра(self):
        """Кадры редкие (дыра в потоке) — время берётся у кадра, а не
        запрошенное: иначе подпись поедет на длину дыры."""
        frames = _stream([0.0, 0.1, 7.3, 7.4, 9.0])
        got = [t for t, _ in S._pick_by_time(iter(frames), 1.5, 100)]
        assert got == [0.0, 7.3, 9.0]

    def test_потолок_кадров(self):
        frames = _stream([i / 10 for i in range(1000)])
        assert len(list(S._pick_by_time(iter(frames), 1.5, 4))) == 4

    def test_кадры_без_времени_пропускаются(self):
        frames = [(None, "x"), (0.0, "a"), (None, "y"), (2.0, "b")]
        assert [f for _, f in S._pick_by_time(iter(frames), 1.5, 10)] == ["a", "b"]


class _Frame:
    """Кадр PyAV: время и картинка."""
    def __init__(self, t, arr):
        self.time = t
        self._arr = arr

    def to_ndarray(self, format=None):
        return self._arr

    def to_image(self):
        return None


class _Container:
    """Контейнер с GOP 25 с: seek встаёт на ключевой кадр ДО цели, как в
    настоящем PyAV."""
    def __init__(self, frames):
        self.frames = frames
        stream = SimpleNamespace(duration=None, time_base=None, thread_type=None,
                                 codec_context=SimpleNamespace())
        self.streams = SimpleNamespace(video=[stream])
        self.duration = int(frames[-1].time * 1_000_000)
        self._pos = 0

    def seek(self, ts, stream=None, backward=True):
        self._pos = 0                       # ключевой кадр — только первый

    def decode(self, stream):
        for f in self.frames[self._pos:]:
            yield f

    def close(self):
        pass


def test_точная_выборка_видит_смену_говорящего(monkeypatch):
    """60 с записи, ключевой кадр один — в начале. До 30-й секунды рамка у
    плитки слева, после — у плитки справа. Раньше вся запись выглядела как
    первый кадр."""
    import sys
    left = np.zeros((4, 4, 3), np.uint8)
    right = np.ones((4, 4, 3), np.uint8)
    frames = [_Frame(i / 10, left if i < 300 else right) for i in range(600)]
    fake_av = SimpleNamespace(open=lambda p: _Container(frames))
    monkeypatch.setitem(sys.modules, "av", fake_av)
    got = [(t, int(a[0, 0, 0])) for t, a, _ in S._sample_frames("v.mp4", 1.5, 3000, exact=True)]
    assert (28.5, 0) in got and (30.0, 1) in got and (58.5, 1) in got


def test_разметка_зовёт_точную_выборку(monkeypatch):
    seen = {}

    def fake(path, every, max_frames, exact=False):
        seen["exact"] = exact
        return iter([])
    monkeypatch.setattr(S, "_sample_frames", fake)
    S.identify_speakers("v.mp4", [Segment(0, 1, "x")])
    assert seen["exact"] is True


# --- Рамка у двоих сразу и мусор OCR --------------------------------------
H, W = 1080, 1920


def _frame(rects):
    a = np.zeros((H, W, 3), dtype=np.uint8)
    for (r0, r1, c0, c1) in rects:
        for t in range(4):
            a[r0 + t, c0:c1] = (60, 220, 90)
            a[r1 - t, c0:c1] = (60, 220, 90)
            a[r0:r1, c0 + t] = (60, 220, 90)
            a[r0:r1, c1 - t] = (60, 220, 90)
    return a


_LEFT = (80, 500, 100, 800)
_RIGHT = (80, 500, 1000, 1700)


def _run(monkeypatch, frames, reads, segs, known=None):
    """frames: [(t, [рамки])]; reads: {рамка: имя, которое прочтёт OCR}.
    Найденная рамка чуть шире нарисованной (поиск идёт по уменьшенной маске),
    поэтому OCR «читает» ту плитку, в чьей половине кадра рамка."""
    monkeypatch.setattr(S, "_sample_frames",
                        lambda *a, **k: iter([(t, _frame(r), None) for t, r in frames]))
    monkeypatch.setattr(S, "_read_name",
                        lambda img, bb: reads[_LEFT if bb[3] < W / 2 else _RIGHT])
    return S.identify_speakers("v.mp4", segs, known=known)


def test_рамка_у_двоих_сразу_не_подписывает(monkeypatch):
    """03.11: в 304 кадрах из 3000 рамка горела у обоих. Код брал плитку,
    найденную первой, — одного и того же человека."""
    segs = [Segment(0.0, 3.0, "a"), Segment(3.0, 6.0, "b")]
    _run(monkeypatch, [(0.0, [_LEFT]), (1.5, [_LEFT]),
                       (3.0, [_LEFT, _RIGHT]), (4.5, [_LEFT, _RIGHT])],
         {_LEFT: "Виктор К", _RIGHT: "Мельников Алексей"}, segs)
    assert segs[0].speaker == "Виктор К"
    assert segs[1].speaker is None


def test_мусор_ocr_вне_списка_участников_отбрасывается(monkeypatch):
    """«Geno», «Beano» в боевом протоколе: подпись, прочитанная один раз и
    не похожая ни на одного участника звонка, — не человек."""
    segs = [Segment(0.0, 3.0, "a"), Segment(3.0, 6.0, "b")]
    _run(monkeypatch, [(0.0, [_LEFT]), (1.5, [_LEFT]), (3.0, [_RIGHT]), (4.5, [_RIGHT])],
         {_LEFT: "Виктор К", _RIGHT: "Geno"}, segs,
         known=["Виктор К", "Мельников Алексей"])
    assert segs[0].speaker == "Виктор К"
    assert segs[1].speaker is None


def test_без_списка_участников_имена_не_режутся(monkeypatch):
    segs = [Segment(3.0, 6.0, "b")]
    _run(monkeypatch, [(3.0, [_RIGHT]), (4.5, [_RIGHT])], {_RIGHT: "Geno"}, segs)
    assert segs[0].speaker == "Geno"


def test_искажённая_подпись_сводится_к_участнику(monkeypatch):
    segs = [Segment(0.0, 3.0, "a")]
    _run(monkeypatch, [(0.0, [_LEFT]), (1.5, [_LEFT])], {_LEFT: "Мельников Алексеи"}, segs,
         known=["Мельников Алексей"])
    assert segs[0].speaker == "Мельников Алексей"


def test_задача_передаёт_участников_в_разметку():
    """Без этого сверка с участниками молча не работает."""
    import pathlib
    from app import jobs
    src = pathlib.Path(jobs.__file__).read_text(encoding="utf-8")
    call = src[src.index("speaker_id.identify_speakers("):][:300]
    assert "known=" in call and "video_participants" in call
