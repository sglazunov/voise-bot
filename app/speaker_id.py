"""Identify WHO was speaking from the recorded video, not just the audio.

Video conferencing UIs (Yandex Telemost in our case) highlight the ACTIVE
speaker's tile with a coloured (green) frame and print each participant's name
on their tile. That is a far more reliable source of "who said this" than
guessing from the raw transcript text — so we read it directly from the picture:

    1. sample frames from the recording (PyAV — same decoder as screen_ocr);
    2. in each frame find the tile with the green active-speaker frame
       (numpy colour mask + a rectangle-border check that rejects green
       clothing/backgrounds, which don't form a full frame around an empty tile);
    3. OCR the name label at the bottom of that tile (Tesseract, rus+eng);
    4. build a timeline of (start, end, name) and assign a name to every
       transcript segment by majority time overlap — exactly like diarize.py.

The result is written to `Segment.speaker`, the same channel diarization uses,
so every downstream consumer (txt/srt/json export, the protocol prompt, the
Word document) picks it up unchanged.

Everything is best-effort and gated behind readiness(): a missing dependency or
an un-recognisable frame only disables the feature, it never breaks the
transcription. Detection thresholds are env-tunable (VTX_SPEAKER_*) so the green
colour can be calibrated from a real recording without a code change.
"""
from __future__ import annotations

import difflib
import os
import re
from typing import List, Optional, Tuple

from . import names
from .transcribe import Segment

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}

# ---- tunables (override via env; calibrate the green from a real frame) ------
_SAMPLE_SEC = float(os.getenv("VTX_SPEAKER_SAMPLE_SEC", "1.5"))   # seconds between sampled frames
_MAX_FRAMES = int(os.getenv("VTX_SPEAKER_MAX_FRAMES", "3000"))     # safety cap on frames
_MIN_TILE = float(os.getenv("VTX_SPEAKER_MIN_TILE", "0.10"))       # min tile w/h as fraction of frame
_FILL_MAX = float(os.getenv("VTX_SPEAKER_FILL_MAX", "0.45"))       # max green fill inside bbox (border, not blob)
_EDGE_MIN = float(os.getenv("VTX_SPEAKER_EDGE_MIN", "0.35"))       # min coverage of each bbox edge by green
# Green highlight colour test (RGB). Defaults suit an emerald active-frame; a
# green shirt/background is rejected later by the rectangle-border check.
_G_MIN = int(os.getenv("VTX_SPEAKER_G_MIN", "110"))
_GR_DIFF = int(os.getenv("VTX_SPEAKER_GR_DIFF", "35"))
_GB_DIFF = int(os.getenv("VTX_SPEAKER_GB_DIFF", "20"))
_R_MAX = int(os.getenv("VTX_SPEAKER_R_MAX", "185"))
_B_MAX = int(os.getenv("VTX_SPEAKER_B_MAX", "185"))
_NAME_BAND = float(os.getenv("VTX_SPEAKER_NAME_BAND", "0.26"))     # bottom fraction of tile holding the name
# Верхняя полоса кадра, где Телемост держит плитки участников во время
# демонстрации экрана. Кандидат оттуда приоритетнее: ниже начинается чужой
# экран, и его зелёные кнопки OCR принимал за подсветку говорящего.
_TOP_STRIP = float(os.getenv("VTX_SPEAKER_TOP_STRIP", "0.30"))
# Минимальная доля кадра для плитки ВНЕ верхней полосы (режим сетки, когда
# никто не делится экраном). Строка меню столько не занимает.
_MIN_TILE_AREA = float(os.getenv("VTX_SPEAKER_MIN_TILE", "0.02"))
# Доля кадров, в которых подпись должна встретиться, чтобы считаться участником.
_MIN_SEEN_SHARE = float(os.getenv("VTX_PARTICIPANT_MIN_SHARE", "0.34"))


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXTS


def readiness() -> dict:
    """Report whether video speaker-ID can run, with per-requirement hints.

    Same external needs as screen OCR (Pillow + PyAV + Tesseract/rus) plus numpy
    (always present — it ships with faster-whisper)."""
    from . import screen_ocr
    checks = list(screen_ocr.readiness()["checks"])
    try:
        import numpy  # noqa: F401
        checks.append({"name": "Пакет numpy", "ok": True, "hint": ""})
    except Exception:
        checks.append({"name": "Пакет numpy", "ok": False,
                       "hint": "pip install numpy (в окружении .venv)"})
    return {"available": all(c["ok"] for c in checks), "checks": checks}


# ---- frame sampling --------------------------------------------------------
def _sample_frames(path: str, every_sec: float, max_frames: int):
    """Yield (timestamp_sec, rgb ndarray HxWx3, PIL.Image) sampled ~every_sec.

    Unlike screen_ocr we do NOT restrict to keyframes: who-is-speaking changes
    faster than slides, so we want a real frame near each sampled instant."""
    import av
    import numpy as np

    container = av.open(path)
    try:
        stream = container.streams.video[0]
    except (IndexError, KeyError):
        container.close()
        return

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
            arr = frame.to_ndarray(format="rgb24")
            yield t, arr, frame.to_image()
        except StopIteration:
            break
        except Exception:
            break
        count += 1
        t += every_sec
        if not duration:
            break
    container.close()


# ---- active-tile detection -------------------------------------------------
def _green_mask(arr):
    """Boolean HxW mask of Telemost's active-speaker green."""
    import numpy as np
    r = arr[..., 0].astype(np.int16)
    g = arr[..., 1].astype(np.int16)
    b = arr[..., 2].astype(np.int16)
    return ((g >= _G_MIN) & ((g - r) >= _GR_DIFF) & ((g - b) >= _GB_DIFF)
            & (r <= _R_MAX) & (b <= _B_MAX))


def _green_components(mask, step_target: int = 320):
    """Connected components of the green mask, as full-res bounding boxes.

    Needed because a real Telemost frame has MORE green than the highlight: the
    recording bot's avatar circle is green, someone's photo may be green. A
    global bounding box over all green pixels would merge them into nonsense —
    each blob must be judged on its own. Downsampling uses block-OR so the thin
    highlight border survives, then a simple BFS labels the blobs."""
    import numpy as np
    from collections import deque

    H, W = mask.shape
    step = max(1, int(max(H, W) / step_target))
    h2, w2 = H - H % step, W - W % step
    small = mask[:h2, :w2].reshape(h2 // step, step, w2 // step, step).any(axis=(1, 3))

    lbl = np.zeros(small.shape, dtype=np.int32)
    comps = []
    nh, nw = small.shape
    cur = 0
    for i in range(nh):
        for j in range(nw):
            if not small[i, j] or lbl[i, j]:
                continue
            cur += 1
            lbl[i, j] = cur
            q = deque([(i, j)])
            r0 = r1 = i
            c0 = c1 = j
            while q:
                y, x = q.popleft()
                r0, r1 = min(r0, y), max(r1, y)
                c0, c1 = min(c0, x), max(c1, x)
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    yy, xx = y + dy, x + dx
                    if 0 <= yy < nh and 0 <= xx < nw and small[yy, xx] and not lbl[yy, xx]:
                        lbl[yy, xx] = cur
                        q.append((yy, xx))
            comps.append((r0 * step, min((r1 + 1) * step - 1, H - 1),
                          c0 * step, min((c1 + 1) * step - 1, W - 1)))
    return comps


def _is_border_rect(mask, bbox, H, W) -> bool:
    """Does this blob look like a RECTANGLE BORDER around a non-green tile?
    Rejects solid blobs (green avatars/clothing) and scattered green."""
    r0, r1, c0, c1 = bbox
    h, w = r1 - r0 + 1, c1 - c0 + 1
    if h < _MIN_TILE * H or w < _MIN_TILE * W:
        return False
    sub = mask[r0:r1 + 1, c0:c1 + 1]
    # A solid blob (the bot's green avatar circle, a green shirt) fills its
    # bbox; a highlight frame is hollow.
    if sub.sum() / float(h * w) > _FILL_MAX:
        return False
    # All four edges must be substantially green — a full rectangle frame.
    bw = max(2, int(0.012 * min(H, W)))
    top = sub[:bw, :].any(axis=0).mean()
    bot = sub[-bw:, :].any(axis=0).mean()
    left = sub[:, :bw].any(axis=1).mean()
    right = sub[:, -bw:].any(axis=1).mean()
    return min(top, bot, left, right) >= _EDGE_MIN


def _active_bbox(arr) -> Optional[Tuple[int, int, int, int]]:
    """Bounding box (r0, r1, c0, c1) of the green active-speaker frame, or None.

    Looks at every green blob SEPARATELY (see _green_components) and returns the
    one shaped like a rectangle border — so the bot's green avatar or a green
    photo elsewhere in the grid can't break the detection.

    КОГДА ИДЁТ ДЕМОНСТРАЦИЯ ЭКРАНА плитки участников уезжают в узкую ПОЛОСУ
    СВЕРХУ, а всё остальное занимает чужой экран. На нём тоже находятся зелёные
    прямоугольники — кнопки и выделения в самом демонстрируемом приложении. По
    ним OCR читал пункты меню и записывал их в участники встречи: в боевом
    протоколе так появились «Удалить», «Мероприятия», «Группы», «Роли Людей».

    Поэтому кандидат из верхней полосы имеет приоритет. Если там ничего нет
    (сетка на весь экран, никто не делится), берём кандидата ниже — но только
    достаточно крупного: плитка человека занимает заметную площадь, а строка
    меню тонкая.
    """
    H, W = arr.shape[:2]
    mask = _green_mask(arr)
    if mask.sum() < 200:  # basically no green — no active highlight
        return None
    strip_bottom = H * _TOP_STRIP
    min_area = H * W * _MIN_TILE_AREA
    top_best, top_area = None, 0
    any_best, any_area = None, 0
    for bbox in _green_components(mask):
        if not _is_border_rect(mask, bbox, H, W):
            continue
        area = (bbox[1] - bbox[0]) * (bbox[3] - bbox[2])
        if bbox[0] < strip_bottom and area > top_area:
            top_best, top_area = bbox, area
        if area > any_area:
            any_best, any_area = bbox, area
    if top_best is not None:
        return top_best
    return any_best if any_area >= min_area else None


def _clean_line(line: str) -> str:
    """Normalise one OCR line into a plausible display name ('' if junk).

    Общие правила имён — в app/names.py: те же, что для списка участников.
    Раньше этот путь чистил подписи по-своему, и в протоколе список участников
    показывал «Зоя Р», а метки спикеров в тексте — «ЗояР». Заодно отсекается
    интерфейс, попадающий в кадр («Client», «Russian», «Ш Демонстрация»).
    """
    line = re.sub(r"[^0-9A-Za-zА-Яа-яЁё .\-]", " ", line)
    line = re.sub(r"\s+", " ", line).strip(" .-")
    letters = sum(ch.isalpha() for ch in line)
    if letters < 2 or len(line) > 40:
        return ""
    return names.normalise(line) if names.looks_like_name(line) else ""


def _clean_name(raw: str) -> str:
    """Turn OCR output of the name label into a plausible display name."""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return ""
    # The line with the most letters is the name (icons/mic glyphs OCR as junk).
    return _clean_line(max(lines, key=lambda ln: sum(ch.isalpha() for ch in ln)))


def _read_name(img, bbox) -> str:
    """OCR the name label at the bottom of the given tile."""
    import pytesseract
    from . import screen_ocr
    screen_ocr._point_pytesseract_at_binary()

    r0, r1, c0, c1 = bbox
    bw = max(2, int(0.012 * min(img.height, img.width)))
    tile_h = r1 - r0
    top = r1 - int(_NAME_BAND * tile_h)
    box = (max(0, c0 + bw), max(0, top), min(img.width, c1 - bw), min(img.height, r1 - bw))
    if box[2] - box[0] < 10 or box[3] - box[1] < 6:
        return ""
    crop = img.crop(box)
    # Upscale small crops so Tesseract has enough pixels for the name.
    if crop.width < 400:
        scale = max(1, 400 // max(1, crop.width))
        crop = crop.resize((crop.width * scale, crop.height * scale))
    try:
        raw = pytesseract.image_to_string(crop, lang="rus+eng", config="--psm 6")
    except Exception:
        return ""
    return _clean_name(raw)


def _canonicalise(names: List[str]) -> dict:
    """Map slightly-different OCR spellings of the same name to one canonical
    form (the most frequent spelling in each similarity cluster)."""
    from collections import Counter
    counts = Counter(n for n in names if n)
    canon: dict[str, str] = {}
    reps: List[str] = []
    # Process most-frequent first so the canonical form is the common spelling.
    for name, _ in counts.most_common():
        match = next((r for r in reps
                      if difflib.SequenceMatcher(None, r.lower(), name.lower()).ratio() >= 0.8), None)
        if match is None:
            reps.append(name)
            canon[name] = name
        else:
            canon[name] = match
    return canon


def scan_names(video_path: str, every_sec: float = 20.0,
               max_frames: int = 90) -> List[str]:
    """Quick pre-pass BEFORE transcription: collect the participants' names from
    the Telemost tiles (the green active-speaker frame + its label).

    The names are fed to Whisper as an initial prompt, so real names are WRITTEN
    AS ON SCREEN instead of being guessed by sound («Кирилл» stays «Кирилл», not
    «Кирил»/«Кириллл»). Coarse sampling keeps it fast on hour-long videos."""
    seen_raw: List[str] = []
    for _t, arr, img in _sample_frames(video_path, every_sec, max_frames):
        bbox = _active_bbox(arr)
        if bbox is None:
            continue
        name = _read_name(img, bbox)
        if name:
            seen_raw.append(name)
    if not seen_raw:
        return []
    canon = _canonicalise(seen_raw)
    out: List[str] = []
    for n in seen_raw:
        c = canon.get(n)
        if c and c not in out:
            out.append(c)
    return out


def scan_participants(video_path: str, every_sec: float = 60.0,
                      max_frames: int = 8, min_seen: int = 2) -> List[str]:
    """Read the name labels of EVERY tile in the call grid — the people actually
    connected to the call, exactly as Telemost prints them under the tiles.

    A few frames spread across the meeting are OCR'd whole (labels persist from
    frame to frame; OCR noise doesn't — so a name must show up in at least
    `min_seen` frames). The recording bot's own tile is dropped. The result is
    the AUTHORITATIVE participants list for the protocol: no guessing from
    speech."""
    import pytesseract
    from collections import Counter
    from . import screen_ocr
    screen_ocr._point_pytesseract_at_binary()

    counts: Counter = Counter()
    frames = 0
    for _t, _arr, img in _sample_frames(video_path, every_sec, max_frames):
        frames += 1
        big = img.resize((img.width * 2, img.height * 2))  # labels are tiny
        try:
            raw = pytesseract.image_to_string(big, lang="rus+eng", config="--psm 11")
        except Exception:
            continue
        seen_here = set()
        for ln in raw.splitlines():
            name = _clean_line(ln.strip())
            if name and sum(ch.isalpha() for ch in name) >= 3:
                seen_here.add(name)
        counts.update(seen_here)
    if not counts:
        return []
    # Живой участник виден почти всю встречу; случайный текст — эпизодически.
    # Порога «2 кадра из 8» не хватало: баннер «Групповой звонок завершился»
    # висит в конце и попадал ровно в пару последних кадров, а с ним пролезал
    # и мусор OCR с демонстрируемого экрана («Weenies Sad», «Hireeree Том»).
    # Требуем присутствия хотя бы в трети кадров — по форме такие строки от
    # имени не отличить, а по устойчивости отличить можно.
    need = max(min_seen, round(_MIN_SEEN_SHARE * frames)) if frames >= min_seen else 1
    stable = [n for n, c in counts.items() if c >= need]
    canon = _canonicalise(stable)
    out: List[str] = []
    for n in stable:
        c = canon.get(n)
        if not c or c in out:
            continue
        if re.search(r"\b(бот|bot)\b", c.lower()):  # the recorder itself
            continue
        out.append(c)
    return _merge_short_names(out)


def _merge_short_names(names: List[str]) -> List[str]:
    """Схлопнуть «Павел» и «Шавлак Павел» в одного человека.

    Телемост показывает подпись по-разному в зависимости от ширины плитки, и в
    протокол попадали оба варианта как два участника. Если однословное имя
    целиком входит в многословное — оставляем длинное: оно информативнее.
    """
    out: List[str] = []
    for n in sorted(names, key=lambda s: -len(s.split())):
        words = {w.lower().strip(".") for w in n.split()}
        if any(words <= {w.lower().strip(".") for w in kept.split()} for kept in out):
            continue          # уже есть более полный вариант этого имени
        out.append(n)
    # Возвращаем в исходном порядке — он отражает порядок появления на встрече.
    return [n for n in names if n in out]


def identify_speakers(video_path: str, segments: List[Segment]) -> List[Segment]:
    """Detect the active speaker per moment from the video and label each
    transcript segment in place with the speaker's name. Returns `segments`.

    Segments with no confidently detected speaker are left as-is (speaker=None)
    — we never guess a name."""
    # 1) Sample frames → per-frame (time, name-or-None). Only OCR when the
    #    active tile moves (speaker likely changed), to keep it fast.
    timeline: List[Tuple[float, Optional[str]]] = []
    prev_center: Optional[Tuple[float, float]] = None
    prev_name: Optional[str] = None
    for t, arr, img in _sample_frames(video_path, _SAMPLE_SEC, _MAX_FRAMES):
        bbox = _active_bbox(arr)
        if bbox is None:
            timeline.append((t, None))
            prev_center, prev_name = None, None
            continue
        r0, r1, c0, c1 = bbox
        center = ((r0 + r1) / 2.0, (c0 + c1) / 2.0)
        same_tile = (prev_center is not None and prev_name
                     and abs(center[0] - prev_center[0]) < 0.04 * arr.shape[0]
                     and abs(center[1] - prev_center[1]) < 0.04 * arr.shape[1])
        name = prev_name if same_tile else _read_name(img, bbox)
        timeline.append((t, name or None))
        prev_center, prev_name = center, (name or None)

    detected = [n for _, n in timeline if n]
    if not detected:
        return segments  # nothing recognised — leave transcript unlabelled

    # 2) Canonicalise spellings and compress into (start, end, name) intervals.
    canon = _canonicalise(detected)
    intervals: List[Tuple[float, float, str]] = []
    for i, (t, name) in enumerate(timeline):
        cname = canon.get(name) if name else None
        t_end = timeline[i + 1][0] if i + 1 < len(timeline) else t + _SAMPLE_SEC
        if cname is None:
            continue
        if intervals and intervals[-1][2] == cname and t - intervals[-1][1] <= _SAMPLE_SEC * 1.5:
            intervals[-1] = (intervals[-1][0], t_end, cname)
        else:
            intervals.append((t, t_end, cname))

    # 3) Assign each transcript segment the speaker it overlaps most.
    for seg in segments:
        best_name, best_overlap = None, 0.0
        for ts, te, name in intervals:
            overlap = min(seg.end, te) - max(seg.start, ts)
            if overlap > best_overlap:
                best_overlap, best_name = overlap, name
        if best_name is not None and best_overlap > 0:
            seg.speaker = best_name
    return segments


def debug_report(video_path: str, limit: int = 20) -> List[dict]:
    """Calibration helper: return the first `limit` frames where an active tile
    was detected, with its bbox and the OCR'd name. Use this on a real recording
    to verify detection / tune the VTX_SPEAKER_* thresholds."""
    out: List[dict] = []
    for t, arr, img in _sample_frames(video_path, _SAMPLE_SEC, _MAX_FRAMES):
        bbox = _active_bbox(arr)
        if bbox is None:
            continue
        out.append({"time": round(t, 1), "bbox": bbox, "name": _read_name(img, bbox)})
        if len(out) >= limit:
            break
    return out
