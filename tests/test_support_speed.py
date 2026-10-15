"""`_find_support` через словарь основ: тот же ответ, что у прямого перебора.

Прямой перебор (основа пункта × основа × окно) занимал 6 с на 78 пунктах
40-минутной встречи и держал GIL процесса сайта — все запросы ждали. Ускорение
не должно поменять НИ ОДНОГО ответа: от этого зависит доля подтверждённых
пунктов, а `VERIFY_VERSION` не поднимали."""
from __future__ import annotations

import random
import time

from app import analyze as A


def _reference(point_text, text, threshold=A._SUPPORT_MIN):
    """Прежняя реализация дословно — эталон."""
    pw = A._stems(point_text)
    if len(pw) < 3:
        return None

    def covered(a, b):
        return sum(1 for x in a if any(A._same_stem(x, y) for y in b))

    lines, stems_by_line = A._speech_lines(text)
    best, best_i = 0.0, -1
    for i in range(len(lines)):
        ws = set()
        for j in range(max(0, i - 2), min(len(lines), i + 3)):
            ws |= stems_by_line[j]
        score = covered(pw, ws) / len(pw)
        if score > best:
            best, best_i = score, i
    if best < threshold or best_i < 0:
        return None
    window = list(range(max(0, best_i - 2), min(len(lines), best_i + 3)))
    first = A._stems(point_text.split()[0]) if point_text.split() else set()
    li = max(window, key=lambda j: (covered(pw, stems_by_line[j])
                                    + 2 * covered(first, stems_by_line[j]), j))
    return {"quote": A._strip_labels(lines[li]).strip()[:300]}


_WORDS = ("сделать обновить сервер закрыть задачу конструктор плашечку плашка "
          "наставники интеграция модуль документ найти положить яндекс диск "
          "протокол встреча роли период тип привязан спринт проверить отправить "
          "скан договор клиент оплата счёт отчёт таблица выгрузка импорт вкладка "
          "логирование прототип пользователь база данных конфиги").split()


def _transcript(rnd, n):
    out = []
    for i in range(n):
        if i % 4 == 0:
            out.append(f"[00:{i // 60:02d}:{i % 60:02d}] {rnd.choice(['Зоя', 'Саша', 'Иван'])}:")
        out.append(" ".join(rnd.choice(_WORDS) for _ in range(rnd.randint(3, 14))))
    return "\n".join(out)


def test_ответы_совпадают_с_прямым_перебором():
    rnd = random.Random(15)
    for _ in range(4):
        text = _transcript(rnd, 220)
        for _ in range(40):
            point = " ".join(rnd.choice(_WORDS) for _ in range(rnd.randint(2, 9)))
            for th in (A._SUPPORT_MIN, A._TIME_HINT_MIN, 0.2):
                assert A._find_support(point, text, th) == _reference(point, text, th), point


def test_быстро_на_длинной_встрече():
    rnd = random.Random(7)
    text = _transcript(rnd, 2500)                 # час с лишним речи
    points = [" ".join(rnd.choice(_WORDS) for _ in range(7)) for _ in range(80)]
    A._find_support(points[0], text)              # разбор текста — в кэш
    t = time.perf_counter()
    for p in points:
        A._find_support(p, text, A._TIME_HINT_MIN)
    assert time.perf_counter() - t < 3.0
