"""faster-whisper wrapper. The model is loaded once and reused for every job."""
from __future__ import annotations

import threading
from dataclasses import dataclass, asdict
from typing import Callable, List, Optional

from faster_whisper import WhisperModel

from . import config

_model: Optional[WhisperModel] = None
_model_name: Optional[str] = None
_model_lock = threading.Lock()
# One transcription at a time: the model instance is shared, and the job worker
# and the live-transcribe threads (Д10) must not run inference concurrently.
_transcribe_lock = threading.Lock()


def get_model(name: Optional[str] = None) -> WhisperModel:
    """Load the requested Whisper model, caching one at a time.

    Jobs may pick accuracy (small/medium/large-v3) per run; since only one
    transcription runs at a time, we keep a single model in memory and reload
    it when the requested size changes — bounding RAM to one model.
    """
    global _model, _model_name
    want = name or config.MODEL
    if _model is None or _model_name != want:
        with _model_lock:
            if _model is None or _model_name != want:
                _model = WhisperModel(
                    want,
                    device=config.DEVICE,
                    compute_type=config.COMPUTE_TYPE,
                    cpu_threads=config.CPU_THREADS,
                )
                _model_name = want
    return _model


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
    waiting when another transcription holds the model — a live tick simply
    skips rather than queueing up behind an hour-long job.
    """
    if not _transcribe_lock.acquire(blocking=not nonblocking):
        return None
    try:
        return _transcribe_locked(audio_path, language, on_segment, on_start,
                                  initial_prompt, model_name)
    finally:
        _transcribe_lock.release()


def _transcribe_locked(audio_path, language, on_segment, on_start,
                       initial_prompt, model_name) -> tuple[List[Segment], dict]:
    model = get_model(model_name)
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
    for seg in segments_iter:
        s = Segment(start=seg.start, end=seg.end, text=seg.text.strip(),
                    avg_logprob=getattr(seg, "avg_logprob", None),
                    no_speech_prob=getattr(seg, "no_speech_prob", None))
        if _is_hallucination(s):
            continue    # dropped BEFORE the live stream — the UI never sees it
        out.append(s)
        if on_segment:
            on_segment(s, total)

    # Collapse hallucination loops (the same phrase repeated on silence). The
    # live stream may have briefly shown the run; the saved transcript is clean.
    out = collapse_repeats(out)

    meta = {
        "language": getattr(info, "language", language),
        "language_probability": getattr(info, "language_probability", None),
        "duration": total,
        "model": model_name or config.MODEL,
    }
    return out, meta
