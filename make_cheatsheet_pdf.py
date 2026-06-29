# -*- coding: utf-8 -*-
"""Generate a clean one-page PDF of the cheat sheet (Cyrillic via Arial)."""
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, HRFlowable)

pdfmetrics.registerFont(TTFont("Arial", r"C:\Windows\Fonts\arial.ttf"))
pdfmetrics.registerFont(TTFont("Arial-Bold", r"C:\Windows\Fonts\arialbd.ttf"))

ACCENT = colors.HexColor("#0e7490")
MUTED = colors.HexColor("#555f6b")
DARK = colors.HexColor("#1f2733")

title = ParagraphStyle("title", fontName="Arial-Bold", fontSize=18, leading=22,
                       textColor=DARK)
sub = ParagraphStyle("sub", fontName="Arial", fontSize=9, leading=12, textColor=MUTED)
h2 = ParagraphStyle("h2", fontName="Arial-Bold", fontSize=11.5, leading=14,
                    textColor=ACCENT, spaceBefore=8, spaceAfter=3)
body = ParagraphStyle("body", fontName="Arial", fontSize=9.5, leading=13,
                      textColor=DARK, alignment=TA_LEFT)
note = ParagraphStyle("note", fontName="Arial", fontSize=8.7, leading=12,
                      textColor=MUTED, leftIndent=6)
cell = ParagraphStyle("cell", fontName="Arial", fontSize=9, leading=12, textColor=DARK)
cellh = ParagraphStyle("cellh", fontName="Arial-Bold", fontSize=9, leading=12,
                       textColor=colors.white)

doc = SimpleDocTemplate("docs/ШПАРГАЛКА.pdf", pagesize=A4,
                        leftMargin=16*mm, rightMargin=16*mm,
                        topMargin=13*mm, bottomMargin=12*mm,
                        title="Шпаргалка — запись встреч и протоколы")
S = []
S.append(Paragraph("Шпаргалка — запись встреч и протоколы", title))
S.append(Paragraph("Коротко, на одну страницу. Подробности — в полном руководстве (docs/РУКОВОДСТВО.md).", sub))
S.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#d6dee6"),
                    spaceBefore=6, spaceAfter=2))

S.append(Paragraph("✓ Чтобы встречу записало автоматически", h2))
S.append(Paragraph("В задаче <b>Weeek</b> укажите две вещи:", body))
S.append(Paragraph("<b>1.</b> Ссылка на Телемост — в <b>кастом-поле</b> задачи (тип «ссылка»). "
                   "Можно и другие ссылки в задаче — бот возьмёт именно Телемост.", body))
S.append(Paragraph("<b>2.</b> Дата и время встречи — по ним бот заходит и начинает запись.", body))
S.append(Paragraph("Дальше всё делает бот: <b>заходит за пару минут до старта → пишет встречу → "
                   "кладёт в облако → распознаёт → собирает протокол (Word) → оставляет комментарий "
                   "со ссылкой в задаче.</b>", body))
S.append(Paragraph("→ Запись идёт, пока в комнате есть люди; когда остаются последние — завершается сама. "
                   "Можно остановить вручную кнопкой «Остановить запись» на странице /automation.", note))

S.append(Paragraph("✓ Чтобы записать встречу сразу", h2))
S.append(Paragraph("Страница <b>/automation</b> → у нужной встречи кнопка <b>«Записать сейчас»</b>.", body))

S.append(Paragraph("✓ Распознать готовый файл руками", h2))
S.append(Paragraph("Главная страница <b>/</b> (режим «Распознавание»):", body))
S.append(Paragraph("<b>1.</b> Перетащите аудио/видео. &nbsp; <b>2.</b> Выберите качество модели "
                   "(по умолчанию — норм). &nbsp; <b>3.</b> «Распознать» → текст появляется вживую.", body))
S.append(Paragraph("<b>4.</b> Скачайте TXT / SRT / JSON. &nbsp; <b>5.</b> Нужен протокол? "
                   "Кнопка «Сделать протокол» → получите Word.", body))
S.append(Paragraph("• Чтобы правильно писались имена и термины — впишите их в поле "
                   "«подсказка имён/терминов» перед распознаванием.", note))

S.append(Paragraph("✓ Где забрать результат", h2))
data = [
    [Paragraph("Что", cellh), Paragraph("Где", cellh)],
    [Paragraph("Видео встречи", cell), Paragraph("в облаке (ссылка в комментарии задачи Weeek)", cell)],
    [Paragraph("Текст / субтитры", cell), Paragraph("на странице задачи распознавания (кнопки скачивания)", cell)],
    [Paragraph("Протокол (Word)", cell), Paragraph("там же + ссылка в комментарии Weeek", cell)],
]
t = Table(data, colWidths=[42*mm, 120*mm])
t.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
]))
S.append(t)

S.append(Paragraph("! Если что-то не так", h2))
S.append(Paragraph("• <b>Встречу не записало</b> → в задаче нет ссылки на Телемост в кастом-поле "
                   "или не указано время. Проверьте оба пункта.", body))
S.append(Paragraph("• <b>Время съехало на пару часов</b> → проверьте часовой пояс в настройках (Europe/Moscow).", body))
S.append(Paragraph("• <b>Бот не зашёл / запись пустая</b> → это к администратору (настройка браузера-бота и звука).", body))
S.append(Spacer(1, 4))
S.append(Paragraph("Вопросы по настройке — к администратору сервиса.", sub))

doc.build(S)
print("OK: docs/ШПАРГАЛКА.pdf")
