"""Generate a Word .docx report from a transcription job."""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional


def _task_parts(item) -> tuple[str, str]:
    """Return (text, owner) for a task, tolerating plain strings or dicts."""
    if isinstance(item, dict):
        return (str(item.get("task") or "").strip(), str(item.get("owner") or "").strip())
    return (str(item).strip(), "")


def _vinfo(analysis: dict, key: str, idx: int) -> Optional[dict]:
    """Verification record for a list item (Д5); None on old protocols."""
    try:
        return (analysis.get("verification") or {})[key][idx]
    except (KeyError, IndexError, TypeError):
        return None


_GRAY = (0x8A, 0x8A, 0x8A)
_UNVERIFIED_MARK = "  ⚠ проверьте — не нашлось дословного подтверждения"


def _apply_verification(par, v, RGBColor) -> None:
    """Gray-out an unverified bullet and add the grounding quote of a verified
    one as a small footnote line under the item."""
    if v is None:
        return
    if not v.get("ok"):
        for r in par.runs:
            r.font.color.rgb = RGBColor(*_GRAY)
        warn = par.add_run(_UNVERIFIED_MARK)
        warn.italic = True
        warn.font.color.rgb = RGBColor(*_GRAY)
    elif v.get("quote"):
        t = f" [{v['t']}]" if v.get("t") else ""
        src = " (из заметок участника)" if v.get("source") == "notes" else ""
        note = par.add_run(f"\nОснование{t}{src}: «{v['quote']}»")
        note.italic = True
        note.font.color.rgb = RGBColor(*_GRAY)


def _fmt_time(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def generate_report(
    out_path: Path,
    filename: str,
    segments: list,
    analysis: dict,
    duration: Optional[float] = None,
    date_str: Optional[str] = None,
) -> None:
    """Write a Word document with summary, key thoughts, tasks and full transcript."""
    try:
        from docx import Document
        from docx.shared import Pt, RGBColor, Inches
        from docx.enum.text import WD_ALIGN_PARAGRAPH
    except ImportError:
        raise RuntimeError(
            "Пакет python-docx не установлен. Выполните: pip install python-docx"
        )

    doc = Document()

    # ---- page margins (narrower for readability) ----------------------------
    for section in doc.sections:
        section.top_margin = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin = Inches(1.2)
        section.right_margin = Inches(1.2)

    # ---- default paragraph font ---------------------------------------------
    normal = doc.styles["Normal"]
    normal.font.name = "Arial"
    normal.font.size = Pt(11)

    # ---- title block --------------------------------------------------------
    title_p = doc.add_heading("Протокол встречи", level=0)
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = sub.add_run(Path(filename).stem)
    run.bold = True
    run.font.size = Pt(12)

    meta_line = doc.add_paragraph()
    meta_line.alignment = WD_ALIGN_PARAGRAPH.CENTER
    shown_date = date_str or datetime.datetime.now().strftime("%d.%m.%Y")
    dur_str = f"  ·  Длительность: {_fmt_time(duration)}" if duration else ""
    # Движок — прямо в документе. Раньше он был только в имени файла, и понять
    # «этот протокол собрал DeepSeek или всё-таки Groq?» по открытому документу
    # было нельзя. Важно как раз при откате: выбрали один движок, ответил другой.
    prov = str(analysis.get("_provider") or "").strip()
    model = str(analysis.get("_model") or "").strip()
    eng = f"{prov} · {model}" if prov and model else (prov or model)
    eng_str = f"  ·  Движок: {eng}" if eng else ""
    meta_run = meta_line.add_run(f"Дата: {shown_date}{dur_str}{eng_str}")
    meta_run.font.color.rgb = RGBColor(0x80, 0x80, 0x80)
    meta_run.font.size = Pt(10)

    # Сработал откат на другой движок — говорим об этом прямо: выбор движка
    # иначе выглядит проигнорированным.
    fb = analysis.get("_fallback")
    if isinstance(fb, list) and fb:
        fbp = doc.add_paragraph()
        fbp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        fr = fbp.add_run("Выбранный движок не ответил, протокол собран запасным. "
                         + "; ".join(str(x)[:160] for x in fb[:2]))
        fr.font.color.rgb = RGBColor(0xB0, 0x50, 0x00)
        fr.font.size = Pt(9)

    # Мало речи — предупреждаем в самом верху. Иначе протокол на 240 слов,
    # собранный по 118 словам разговора, выглядит как полноценный итог встречи,
    # которая на деле не состоялась.
    thin = analysis.get("_thin_speech")
    if isinstance(thin, int):
        warn = doc.add_paragraph()
        warn.alignment = WD_ALIGN_PARAGRAPH.CENTER
        wr = warn.add_run(
            f"⚠ В записи мало речи — распознано слов: {thin}. Похоже, встреча "
            "не состоялась или запись шла без звука. Выводы ниже опираются на "
            "короткий разговор и могут быть неполными.")
        wr.font.color.rgb = RGBColor(0xB0, 0x50, 0x00)
        wr.font.size = Pt(10)
        wr.bold = True

    doc.add_paragraph()  # spacer

    # ---- Участники ----------------------------------------------------------
    participants = analysis.get("participants", [])
    if participants:
        doc.add_heading("Участники", level=1)
        for pt in participants:
            name = (pt.get("name") or "").strip() if isinstance(pt, dict) else str(pt).strip()
            role = (pt.get("role") or "").strip() if isinstance(pt, dict) else ""
            if not name:
                continue
            bp = doc.add_paragraph(style="List Bullet")
            bp.add_run(name).bold = True
            if role:
                r = bp.add_run(f" — {role}")
                r.italic = True
                r.font.color.rgb = RGBColor(0x60, 0x60, 0x60)

    # ---- О чём шёл разговор -------------------------------------------------
    doc.add_heading("О чём шёл разговор", level=1)
    p = doc.add_paragraph(analysis.get("summary", ""))
    p.paragraph_format.space_after = Pt(12)

    # ---- Подробный разбор по темам -----------------------------------------
    detailed = analysis.get("detailed", [])
    if detailed:
        doc.add_heading("Подробный разбор", level=1)
        for block in detailed:
            topic = (block.get("topic") or "").strip()
            details = (block.get("details") or "").strip()
            if topic:
                doc.add_heading(topic, level=2)
            if details:
                dp = doc.add_paragraph(details)
                dp.paragraph_format.space_after = Pt(10)

    # ---- Ключевые мысли -----------------------------------------------------
    doc.add_heading("Ключевые мысли", level=1)
    thoughts = analysis.get("key_thoughts", [])
    if thoughts:
        for thought in thoughts:
            bp = doc.add_paragraph(style="List Bullet")
            bp.add_run(thought)
    else:
        doc.add_paragraph("—").paragraph_format.space_after = Pt(6)

    # ---- Выводы -------------------------------------------------------------
    conclusions = analysis.get("conclusions", [])
    if conclusions:
        doc.add_heading("Выводы", level=1)
        for c in conclusions:
            bp = doc.add_paragraph(style="List Bullet")
            bp.add_run(c)

    # ---- Решения / договорённости ------------------------------------------
    decisions = analysis.get("decisions", [])
    if decisions:
        doc.add_heading("Решения и договорённости", level=1)
        for di, d in enumerate(decisions):
            bp = doc.add_paragraph(style="List Bullet")
            bp.add_run(d)
            _apply_verification(bp, _vinfo(analysis, "decisions", di), RGBColor)

    # ---- Сделано (выполненные задачи) --------------------------------------
    done_tasks = analysis.get("done_tasks", [])
    if done_tasks:
        doc.add_heading("Сделано (выполненные задачи)", level=1)
        for ti, item in enumerate(done_tasks):
            text, owner = _task_parts(item)
            bp = doc.add_paragraph(style="List Bullet")
            bp.add_run(f"☑ {text}")
            if owner:
                r = bp.add_run(f"  — {owner}")
                r.italic = True
                r.font.color.rgb = RGBColor(0x60, 0x60, 0x60)
            _apply_verification(bp, _vinfo(analysis, "done_tasks", ti), RGBColor)

    # ---- Задачи (нужно сделать) --------------------------------------------
    # Подтверждённые цитатой и неподтверждённые разведены по разным блокам.
    # Причина: на боевой встрече из 8 задач 6 оказались без дословного
    # основания — модель сформулировала их «по мотивам». Вперемешку это
    # обесценивает весь список: читателю приходится перепроверять каждую
    # строку. Порознь основной список остаётся доверенным, а спорное не
    # теряется — уходит в блок «Требуют проверки».
    tasks = analysis.get("tasks", [])
    sure, unsure = [], []
    for i, item in enumerate(tasks):
        (sure if (_vinfo(analysis, "tasks", i) or {}).get("ok") else unsure).append((i, item))
    # Пока grounding не отработал (старые протоколы) — verification пуст, и всё
    # попадёт в «требуют проверки». Это неверно: там просто нет проверки.
    if not (analysis.get("verification") or {}).get("tasks"):
        sure, unsure = [(i, t) for i, t in enumerate(tasks)], []

    doc.add_heading("Задачи (нужно сделать)", level=1)
    if sure:
        table = doc.add_table(rows=1, cols=3)
        table.style = "Table Grid"
        for cell, title in zip(table.rows[0].cells, ("№", "Задача", "Ответственный")):
            cell.paragraphs[0].add_run(title).bold = True
        for n, (i, item) in enumerate(sure, 1):
            text, owner = _task_parts(item)
            row = table.add_row().cells
            row[0].paragraphs[0].add_run(str(n))
            tp = row[1].paragraphs[0]
            tp.add_run(text)
            _apply_verification(tp, _vinfo(analysis, "tasks", i), RGBColor)
            row[2].paragraphs[0].add_run(owner or "—")
        doc.add_paragraph().paragraph_format.space_after = Pt(6)
    else:
        doc.add_paragraph("Задач с дословным подтверждением не найдено."
                          ).paragraph_format.space_after = Pt(6)

    if unsure:
        doc.add_heading("Требуют проверки", level=1)
        p = doc.add_paragraph()
        r = p.add_run("Эти пункты ИИ сформулировал по смыслу обсуждения, но "
                      "дословного основания в расшифровке не нашлось. "
                      "Проверьте их перед тем, как заводить в работу.")
        r.italic = True
        r.font.size = Pt(10)
        r.font.color.rgb = RGBColor(0x80, 0x80, 0x80)
        for i, item in unsure:
            text, owner = _task_parts(item)
            bp = doc.add_paragraph(style="List Bullet")
            bp.add_run(f"☐ {text}")
            if owner:
                ro = bp.add_run(f"  — {owner}")
                ro.italic = True
                ro.font.color.rgb = RGBColor(0x60, 0x60, 0x60)
        doc.add_paragraph().paragraph_format.space_after = Pt(6)

    # ---- Мелкие задачи и доработки -----------------------------------------
    minor = analysis.get("minor_tasks", [])
    if minor:
        doc.add_heading("Мелкие задачи и доработки", level=1)
        for mi, item in enumerate(minor):
            text, owner = _task_parts(item)
            bp = doc.add_paragraph(style="List Bullet")
            bp.add_run(f"☐ {text}")
            _apply_verification(bp, _vinfo(analysis, "minor_tasks", mi), RGBColor)
            if owner:
                r = bp.add_run(f"  — {owner}")
                r.italic = True
                r.font.color.rgb = RGBColor(0x60, 0x60, 0x60)

    # ---- Full transcript on new page ----------------------------------------
    doc.add_page_break()
    doc.add_heading("Полная транскрипция", level=1)

    if segments:
        for seg in segments:
            start = seg.get("start", 0)
            text = seg.get("text", "").strip()
            speaker = seg.get("speaker")
            if not text:
                continue
            line_p = doc.add_paragraph()
            ts_run = line_p.add_run(f"[{_fmt_time(start)}]  ")
            ts_run.font.color.rgb = RGBColor(0x00, 0x70, 0xC0)
            ts_run.font.size = Pt(10)
            if speaker:
                spk_run = line_p.add_run(f"{speaker}: ")
                spk_run.bold = True
            line_p.add_run(text)
            line_p.paragraph_format.space_after = Pt(4)
    else:
        doc.add_paragraph("Транскрипция недоступна.")

    doc.save(str(out_path))
