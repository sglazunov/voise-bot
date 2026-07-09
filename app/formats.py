"""Render transcript segments into txt / srt / json, with optional speaker labels."""
from __future__ import annotations

import json
from typing import List

from .transcribe import Segment


def _ts_srt(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ts_human(seconds: float) -> str:
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def to_txt(segments: List[Segment]) -> str:
    """Plain readable text. If speakers are present, group consecutive lines
    by speaker into a screenplay-style transcript."""
    lines: List[str] = []
    last_speaker = object()
    for seg in segments:
        if seg.speaker is not None:
            if seg.speaker != last_speaker:
                lines.append("")
                lines.append(f"[{_ts_human(seg.start)}] {seg.speaker}:")
                last_speaker = seg.speaker
            lines.append(seg.text)
        else:
            lines.append(f"[{_ts_human(seg.start)}] {seg.text}")
    return "\n".join(lines).strip() + "\n"


def to_plain(segments: List[Segment]) -> str:
    """Flowing, readable transcript WITHOUT per-segment timestamps — for the
    on-screen view and as clean input to the protocol LLM (fewer tokens, less
    noise). Speaker labels are kept (they help attribution): each speaker's turn
    becomes one paragraph «Имя: …». Without speakers, text is grouped into short
    paragraphs so it isn't one giant wall."""
    paras: List[str] = []
    has_speakers = any(getattr(s, "speaker", None) for s in segments)
    if has_speakers:
        cur = object()
        buf: List[str] = []

        def flush() -> None:
            if not buf:
                return
            label = f"{cur}: " if isinstance(cur, str) and cur.strip() else ""
            paras.append(label + " ".join(buf).strip())

        for seg in segments:
            if seg.speaker != cur:
                flush()
                buf = []
                cur = seg.speaker
            if seg.text.strip():
                buf.append(seg.text.strip())
        flush()
    else:
        buf = []
        for seg in segments:
            t = seg.text.strip()
            if t:
                buf.append(t)
            if len(buf) >= 5:            # ~5 сегментов на абзац — читабельно
                paras.append(" ".join(buf))
                buf = []
        if buf:
            paras.append(" ".join(buf))
    return "\n\n".join(p for p in paras if p).strip() + "\n"


def to_srt(segments: List[Segment]) -> str:
    blocks = []
    for i, seg in enumerate(segments, 1):
        prefix = f"{seg.speaker}: " if seg.speaker else ""
        blocks.append(
            f"{i}\n{_ts_srt(seg.start)} --> {_ts_srt(seg.end)}\n{prefix}{seg.text}\n"
        )
    return "\n".join(blocks)


def to_json(segments: List[Segment], meta: dict) -> str:
    return json.dumps(
        {"meta": meta, "segments": [s.to_dict() for s in segments]},
        ensure_ascii=False,
        indent=2,
    )
