"""faster-whisper wrapper. Модели живут в пуле слотов — по одному экземпляру на
воркер очереди; слот занимается на время одной расшифровки."""
from __future__ import annotations

import threading
from dataclasses import dataclass, asdict
from typing import Callable, List, Optional

from faster_whisper import WhisperModel

from . import config

# Пул экземпляров модели: по одному на воркер очереди (config.JOB_WORKERS).
# Раньше экземпляр был один и один замок — два воркера упёрлись бы в него и
# работали бы по очереди. Слот 0 грузится со всеми потоками, остальные — с
# половиной (одиночная задача всегда берёт слот 0 и не теряет скорость).
class _Slot:
    def __init__(self, index: int) -> None:
        self.index = index
        self.lock = threading.Lock()
        self.model: Optional[WhisperModel] = None
        self.model_name: Optional[str] = None
        self.threads = (config.CPU_THREADS if index == 0
                        else max(2, config.CPU_THREADS // 2))

    def get(self, name: Optional[str]) -> WhisperModel:
        want = name or config.MODEL
        if self.model is None or self.model_name != want:
            self.model = WhisperModel(
                want, device=config.DEVICE, compute_type=config.COMPUTE_TYPE,
                cpu_threads=self.threads)
            self.model_name = want
        return self.model


_POOL_SIZE = max(1, int(getattr(config, "JOB_WORKERS", 1) or 1))
_SLOTS: List[_Slot] = [_Slot(i) for i in range(_POOL_SIZE)]
# Сколько слотов свободно. Живая расшифровка берёт слот только если он
# свободен прямо сейчас (nonblocking) — она не должна стоять в очереди за
# часовой задачей.
_SEM = threading.Semaphore(_POOL_SIZE)


def _acquire_slot(blocking: bool) -> Optional[_Slot]:
    if not _SEM.acquire(blocking=blocking):
        return None
    for slot in _SLOTS:
        if slot.lock.acquire(blocking=False):
            return slot
    _SEM.release()          # не должно случаться: семафор гарантирует слот
    return None


def _release_slot(slot: _Slot) -> None:
    slot.lock.release()
    _SEM.release()


def get_model(name: Optional[str] = None) -> WhisperModel:
    """Экземпляр модели первого слота (предзагрузка при старте). Для самой
    расшифровки не использовать — слот надо занимать через transcribe_file."""
    return _SLOTS[0].get(name)


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: Optional[str] = None  # filled in later if diarization runs
    # Whisper's own confidence for this segment — kept for the hallucination
    # filter here and for the low-confidence highlighting in the UI (Д12).
    avg_logprob: Optional[float] = None
    no_speech_prob: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _is_hallucination(seg: "Segment") -> bool:
    """Whisper «дорисовывает» фразы на тишине: сам помечает окно как вероятную
    тишину (no_speech_prob высок) и при этом декодирует с низкой уверенностью.
    Оба условия сразу — почти наверняка выдуманный текст."""
    ns = seg.no_speech_prob
    lp = seg.avg_logprob
    return (ns is not None and lp is not None
            and ns > config.NS_PROB_MAX and lp < config.LOGPROB_MIN)


def trim_chunk_edge(segments: List["Segment"], chunk_end: float,
                    margin: float = 1.5) -> tuple[List["Segment"], float]:
    """Живой кусок режется по времени, а не по паузе: последний сегмент почти
    наверняка оборван на полуслове. Отбрасываем сегменты, упирающиеся в край
    куска (до `margin` с от конца), и говорим, ДОКУДА кусок считать
    покрытым — следующий начнётся оттуда и дораспознает обрывок целиком.
    Возвращает (оставленные сегменты, граница покрытия в абсолютных
    секундах). Если резать нечего (один сегмент или всё далеко от края) —
    покрыто до конца куска."""
    keep = [s for s in segments if s.end <= chunk_end - margin]
    if len(keep) == len(segments) or not keep:
        return list(segments), chunk_end
    return keep, keep[-1].end


def collapse_repeats(segments: List["Segment"],
                     threshold: Optional[int] = None) -> List["Segment"]:
    """Collapse runs of >= threshold IDENTICAL consecutive segments to one.

    The looping failure mode: on silence/music Whisper repeats the same phrase
    dozens of times. Short doubles (a real «да. да.») stay untouched."""
    thr = threshold or config.REPEAT_COLLAPSE_AT
    out: List[Segment] = []
    run: List[Segment] = []

    def flush() -> None:
        if len(run) >= thr:
            out.append(run[0])   # the run was a loop — keep a single copy
        else:
            out.extend(run)
        run.clear()

    for seg in segments:
        key = seg.text.strip().lower()
        if run and key == run[-1].text.strip().lower() and key:
            run.append(seg)
            continue
        flush()
        run.append(seg)
    flush()
    return out


def transcribe_file(
    audio_path: str,
    language: Optional[str] = None,
    on_segment: Optional[Callable[["Segment", float], None]] = None,
    on_start: Optional[Callable[[], None]] = None,
    initial_prompt: Optional[str] = None,
    model_name: Optional[str] = None,
    nonblocking: bool = False,
    offset_sec: float = 0.0,
) -> Optional[tuple[List[Segment], dict]]:
    """Transcribe an audio or video file.

    Video files (mp4/mkv/…) work directly — faster-whisper decodes the audio
    stream via PyAV/ffmpeg.

    on_start() is called after the model loads and transcription begins,
    so the UI can show that work is in progress before the first segment arrives.

    on_segment(segment, total_seconds) is called for every segment as it is
    produced (faster-whisper yields them lazily), so the UI can stream the
    growing transcript and show real progress.

    `nonblocking=True` (the live-transcribe path) returns None instead of
    waiting when every model slot is busy — a live tick simply skips rather
    than queueing up behind an hour-long job.

    `offset_sec` сдвигает времена сегментов: файл — вырезанный кусок записи,
    а времена нужны от начала встречи (живые куски и дораспознанный хвост).
    """
    slot = _acquire_slot(blocking=not nonblocking)
    if slot is None:
        return None
    try:
        return _transcribe_locked(slot, audio_path, language, on_segment, on_start,
                                  initial_prompt, model_name, offset_sec)
    finally:
        _release_slot(slot)


def _transcribe_locked(slot, audio_path, language, on_segment, on_start,
                       initial_prompt, model_name, offset_sec=0.0) -> tuple[List[Segment], dict]:
    model = slot.get(model_name)
    segments_iter, info = model.transcribe(
        audio_path,
        language=language or config.DEFAULT_LANGUAGE,
        vad_filter=config.VAD_FILTER,
        # Finer VAD (0.5 s of silence instead of the 2 s default) trims the
        # quiet gaps where Whisper likes to "dream up" words that weren't said.
        vad_parameters={"min_silence_duration_ms": 500},
        beam_size=config.BEAM_SIZE,
        best_of=config.BEAM_SIZE,
        initial_prompt=initial_prompt or None,
        # OFF by default: carrying the previous window's text as context lets one
        # mis-heard word snowball into invented phrases across a long meeting.
        # With it off, every window is transcribed strictly from its own audio.
        condition_on_previous_text=config.CONDITION_PREV_TEXT,
    )

    total = float(getattr(info, "duration", 0.0) or 0.0)
    if on_start:
        on_start()
    out: List[Segment] = []
    total_abs = total + offset_sec
    for seg in segments_iter:
        s = Segment(start=seg.start + offset_sec, end=seg.end + offset_sec,
                    text=seg.text.strip(),
                    avg_logprob=getattr(seg, "avg_logprob", None),
                    no_speech_prob=getattr(seg, "no_speech_prob", None))
        if _is_hallucination(s):
            continue    # dropped BEFORE the live stream — the UI never sees it
        out.append(s)
        if on_segment:
            on_segment(s, total_abs)

    # Collapse hallucination loops (the same phrase repeated on silence). The
    # live stream may have briefly shown the run; the saved transcript is clean.
    out = collapse_repeats(out)

    meta = {
        "language": getattr(info, "language", language),
        "language_probability": getattr(info, "language_probability", None),
        "duration": total + offset_sec,
        "model": model_name or config.MODEL,
    }
    return out, meta
