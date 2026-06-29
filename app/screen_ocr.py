"""Optional: capture what is SHOWN on screen in a video (slides, code, docs).

Samples frames from the video (via PyAV — no external ffmpeg needed), runs OCR
on each (Tesseract), de-duplicates near-identical consecutive screens, and
returns timestamped on-screen text. That text is fed into the protocol analysis
so the document also reflects what was demonstrated, not only what was said.

This reads TEXT on screen — it does not "understand" live demos. Quality depends
on Tesseract and the source resolution. Everything is gated behind readiness()
so a missing dependency only disables the feature, never breaks transcription.
"""
from __future__ import annotations

import difflib
import re
from typing import List

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}


def _point_pytesseract_at_binary() -> None:
    """If Tesseract isn't on PATH but was installed (e.g. via winget from the
    UI), point pytesseract at the discovered binary so OCR works without a
    process restart (the running server's PATH won't have picked it up)."""
    try:
        import shutil
        if shutil.which("tesseract"):
            return
        import pytesseract
        from .deps_setup import find_tesseract
        exe = find_tesseract()
        if exe:
            pytesseract.pytesseract.tesseract_cmd = exe
    except Exception:
        pass


def readiness() -> dict:
    """Report whether screen OCR can run, with per-requirement hints."""
    _point_pytesseract_at_binary()
    checks = []
    try:
        import PIL  # noqa: F401
        checks.append({"name": "Пакет Pillow", "ok": True, "hint": ""})
    except Exception:
        checks.append({"name": "Пакет Pillow", "ok": False,
                       "hint": "pip install Pillow (в окружении .venv)"})
    try:
        import av  # noqa: F401
        checks.append({"name": "Декодер видео (av)", "ok": True, "hint": ""})
    except Exception:
        checks.append({"name": "Декодер видео (av)", "ok": False,
                       "hint": "pip install av"})

    tess_ok = False
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        tess_ok = True
        checks.append({"name": "Программа Tesseract", "ok": True, "hint": ""})
    except Exception:
        checks.append({"name": "Программа Tesseract", "ok": False,
                       "hint": "Установите Tesseract: "
                               "winget install UB-Mannheim.TesseractOCR "
                               "(при установке отметьте русский язык)."})

    if tess_ok:
        try:
            import pytesseract
            langs = pytesseract.get_languages(config="")
            if "rus" in langs:
                checks.append({"name": "Русский язык OCR (rus)", "ok": True, "hint": ""})
            else:
                checks.append({"name": "Русский язык OCR (rus)", "ok": False,
                               "hint": "Доустановите русские данные Tesseract "
                                       "(rus.traineddata) — в инсталляторе "
                                       "UB-Mannheim отметьте Russian."})
        except Exception:
            checks.append({"name": "Русский язык OCR (rus)", "ok": False,
                           "hint": "Не удалось проверить языки Tesseract."})

    return {"available": all(c["ok"] for c in checks), "checks": checks}


def is_video(path: str) -> bool:
    import os
    return os.path.splitext(path)[1].lower() in VIDEO_EXTS


def _clean(text: str) -> str:
    lines = []
    for ln in text.splitlines():
        ln = ln.strip()
        # Drop lines that are mostly noise (too few letters/digits).
        letters = sum(ch.isalnum() for ch in ln)
        if len(ln) >= 3 and letters >= max(2, len(ln) // 3):
            lines.append(re.sub(r"\s+", " ", ln))
    return "\n".join(lines).strip()


def _similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _sample_frames(path: str, every_sec: float, max_frames: int):
    """Yield (timestamp_sec, PIL.Image) sampled ~every_sec by seeking."""
    import av

    container = av.open(path)
    try:
        stream = container.streams.video[0]
    except (IndexError, KeyError):
        container.close()
        return
    stream.codec_context.skip_frame = "NONKEY"  # keyframes are enough for slides

    duration = 0.0
    if stream.duration and stream.time_base:
        duration = float(stream.duration * stream.time_base)
    elif container.duration:
        duration = float(container.duration) / 1_000_000.0

    t = 0.0
    count = 0
    while count < max_frames:
        if duration and t > duration:
            break
        try:
            if stream.time_base:
                container.seek(int(t / stream.time_base), stream=stream, backward=True)
            frame = next(container.decode(stream))
            yield t, frame.to_image()
        except (StopIteration, Exception):
            break
        count += 1
        t += every_sec
        if not duration:
            break
    container.close()


def extract_screen_text(path: str, lang: str = "rus+eng",
                        every_sec: float = 5.0, max_frames: int = 360) -> List[dict]:
    """Return [{time, text}] of de-duplicated on-screen text from the video."""
    import pytesseract
    _point_pytesseract_at_binary()

    out: List[dict] = []
    prev = ""
    for t, img in _sample_frames(path, every_sec, max_frames):
        try:
            raw = pytesseract.image_to_string(img, lang=lang)
        except Exception:
            continue
        text = _clean(raw)
        if len(text) < 15:               # skip near-empty screens
            continue
        if _similar(text, prev) > 0.85:  # skip unchanged slide
            continue
        out.append({"time": round(t, 1), "text": text})
        prev = text
    return out


def to_block(items: List[dict]) -> str:
    """Render screen-text items into a labelled text block for the protocol."""
    if not items:
        return ""
    lines = ["=== ТЕКСТ С ЭКРАНА (показ экрана: слайды/код/документы) ==="]
    for it in items:
        s = int(it["time"])
        ts = f"{s // 60:02d}:{s % 60:02d}"
        body = it["text"].replace("\n", " / ")
        lines.append(f"[{ts}] {body}")
    return "\n".join(lines)
