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

    def to_dict(self) -> dict:
        return asdict(self)


def transcribe_file(
    audio_path: str,
    language: Optional[str] = None,
    on_segment: Optional[Callable[["Segment", float], None]] = None,
    on_start: Optional[Callable[[], None]] = None,
    initial_prompt: Optional[str] = None,
    model_name: Optional[str] = None,
) -> tuple[List[Segment], dict]:
    """Transcribe an audio or video file.

    Video files (mp4/mkv/…) work directly — faster-whisper decodes the audio
    stream via PyAV/ffmpeg.

    on_start() is called after the model loads and transcription begins,
    so the UI can show that work is in progress before the first segment arrives.

    on_segment(segment, total_seconds) is called for every segment as it is
    produced (faster-whisper yields them lazily), so the UI can stream the
    growing transcript and show real progress.
    """
    model = get_model(model_name)
    segments_iter, info = model.transcribe(
        audio_path,
        language=language or config.DEFAULT_LANGUAGE,
        vad_filter=config.VAD_FILTER,
        beam_size=config.BEAM_SIZE,
        initial_prompt=initial_prompt or None,
        condition_on_previous_text=True,
    )

    total = float(getattr(info, "duration", 0.0) or 0.0)
    if on_start:
        on_start()
    out: List[Segment] = []
    for seg in segments_iter:
        s = Segment(start=seg.start, end=seg.end, text=seg.text.strip())
        out.append(s)
        if on_segment:
            on_segment(s, total)

    meta = {
        "language": getattr(info, "language", language),
        "language_probability": getattr(info, "language_probability", None),
        "duration": total,
        "model": model_name or config.MODEL,
    }
    return out, meta
