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
import os
import re
from typing import List

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}


def _point_pytesseract_at_binary() -> None:
    """If Tesseract was installed after the server started, its PATH may be stale.
    Re-resolve the binary and point pytesseract at it, so OCR starts working
    without a restart."""
    try:
        import shutil
        exe = shutil.which("tesseract")
        if exe:
            import pytesseract
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
                               "sudo apt install tesseract-ocr tesseract-ocr-rus"})

    if tess_ok:
        try:
            import pytesseract
            langs = pytesseract.get_languages(config="")
            if "rus" in langs:
                checks.append({"name": "Русский язык OCR (rus)", "ok": True, "hint": ""})
            else:
                checks.append({"name": "Русский язык OCR (rus)", "ok": False,
                               "hint": "Доустановите русские данные Tesseract: "
                                       "sudo apt install tesseract-ocr-rus"})
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

    # Шаг растягиваем на ВСЮ длительность. Раньше он был фиксированным, и
    # покрытие упиралось в «шаг × максимум кадров»: участники читались только с
    # первых 8 минут, спикеры — с первых 75, текст с экрана — с первых 30. На
    # боевых четырёхчасовых записях вторая половина встречи оставалась без имён
    # и без содержимого экрана. Чаще заданного шага не берём — только реже.
    step = every_sec
    if duration and max_frames > 0:
        step = max(every_sec, duration / max_frames)

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
        t += step
        if not duration:
            break
    container.close()


def extract_screen_text(path: str, lang: str = "rus+eng",
                        every_sec: float = 5.0, max_frames: int = 360,
                        should_stop=None) -> List[dict]:
    """Return [{time, text}] of de-duplicated on-screen text from the video.

    `should_stop` проверяется между кадрами: распознавание экрана на часовом
    видео идёт десятки минут, и без этой проверки кнопка «Стоп» не действовала
    — пользователь жал её и ничего не происходило.
    """
    import pytesseract
    _point_pytesseract_at_binary()

    out: List[dict] = []
    for t, img in _sample_frames(path, every_sec, max_frames):
        if should_stop and should_stop():
            break                    # отдаём то, что успели распознать
        try:
            raw = pytesseract.image_to_string(img, lang=lang)
        except Exception:
            continue
        text = _clean(raw)
        if len(text) < 15:               # skip near-empty screens
            continue
        # Дедуп по ПОСЛЕДНИМ ПРИНЯТЫМ экранам, а не только по предыдущему:
        # переключение между двумя окнами (A→B→A→B) давало новую запись на
        # каждый кадр, и один и тот же документ уезжал в промпт десятки раз.
        if any(_similar(text, p["text"]) > 0.85 for p in out[-20:]):
            continue
        out.append({"time": round(t, 1), "text": text})
    return out


# Потолок блока «ТЕКСТ С ЭКРАНА». Без него часовая демонстрация кода/таблиц
# давала 0.5–1 МБ текста — в разы больше речи: OCR порождал собственные
# фрагменты анализа, окно регенерации темы ложилось на экран, а «спросить по
# встрече» цитировал экран как сказанное.
MAX_BLOCK_CHARS = int(os.getenv("VTX_OCR_MAX_CHARS", "12000"))
_MIN_BLOCK_CHARS = 2000


def limit_items(items: List[dict], max_chars: int | None = None,
                speech_chars: int | None = None) -> List[dict]:
    """Ужать список экранов под бюджет: не больше `max_chars` и не больше 40 %
    от объёма речи (но не меньше _MIN_BLOCK_CHARS). Экраны прореживаются
    РАВНОМЕРНО по времени, а не обрезаются с конца — иначе вторая половина
    встречи оставалась без слайдов."""
    limit = int(max_chars or MAX_BLOCK_CHARS)
    if speech_chars:
        limit = min(limit, max(_MIN_BLOCK_CHARS, int(0.4 * speech_chars)))
    items = [it for it in items if it.get("text")]
    if sum(len(it["text"]) for it in items) <= limit:
        return items
    # Каждый экран режем до средней доли бюджета; если экранов слишком много —
    # оставляем каждый k-й.
    per = max(300, limit // max(1, len(items)))
    if per < 300 or len(items) * 300 > limit:
        keep_n = max(1, limit // 300)
        step = max(1, len(items) // keep_n)
        items = items[::step][:keep_n]
        per = max(300, limit // max(1, len(items)))
    out = []
    for it in items:
        txt = it["text"]
        if len(txt) > per:
            txt = txt[:per].rsplit(" ", 1)[0] + " …"
        out.append({"time": it["time"], "text": txt})
    return out


def to_block(items: List[dict], speech_chars: int | None = None,
             max_chars: int | None = None) -> str:
    """Render screen-text items into a labelled text block for the protocol,
    limited by budget (see limit_items)."""
    items = limit_items(items, max_chars=max_chars, speech_chars=speech_chars)
    if not items:
        return ""
    lines = ["=== ТЕКСТ С ЭКРАНА (показ экрана: слайды/код/документы) ==="]
    for it in items:
        s = int(it["time"])
        ts = f"{s // 60:02d}:{s % 60:02d}"
        body = it["text"].replace("\n", " / ")
        lines.append(f"[{ts}] {body}")
    return "\n".join(lines)
