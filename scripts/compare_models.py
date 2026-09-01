#!/usr/bin/env python3
"""Сравнить движки на ОДНОЙ И ТОЙ ЖЕ расшифровке — и выбрать по цифрам.

    docker compose exec app python scripts/compare_models.py <логин> [job_id] \
        [--models custom:модель,gemini]

Берёт расшифровку готовой задачи (по умолчанию — последней завершённой) и
прогоняет через каждый движок полный путь: сборка протокола плюс grounding.
Печатает то, по чему протокол и оценивают:

  темы          — насколько подробно разобрана встреча;
  задачи        — сколько поручений извлечено;
  подтверждено  — доля пунктов, к которым нашлась ДОСЛОВНАЯ цитата. Это главный
                  показатель: список, где каждый пункт помечен «проверьте»,
                  приходится перепроверять целиком и тем обесценивается.

Ничего не сохраняет и не отправляет — только считает.
"""
from __future__ import annotations

import pathlib
import sys
import time
import traceback

sys.path.insert(0, "/app")

from app import analyze, user_creds                     # noqa: E402
from app.jobs import STATUS_DONE, store                 # noqa: E402


def _transcript(user: str, job_id: str | None) -> tuple[str, str]:
    # Ищем по ВСЕМ задачам, а не по одному логину: задачи принадлежат команде,
    # и логин, под которым запускают скрипт, может ей не совпадать. Фильтр по
    # владельцу тут ничего не защищает — скрипт и так запускают на сервере.
    jobs = [j for j in store.list() if j.status == STATUS_DONE]
    if job_id:
        jobs = [j for j in jobs if j.id == job_id]
    if not jobs:
        allj = store.list()
        print(f"Готовых задач не нашлось. Всего задач в базе: {len(allj)}")
        for j in allj[:10]:
            print(f"  {j.id}  {j.status:10} владелец={j.owner}  {j.filename[:50]}")
        raise SystemExit("Укажите job_id явно или задайте --file <путь к .txt>.")
    # Берём ту, у которой расшифровка на диске: у старых задач её мог убрать
    # ретеншн, и падать из-за этого посреди списка незачем.
    for job in jobs:
        for fmt in ("txt", "plain"):
            f = store.result_path(job.id, fmt)
            if f.exists():
                return job.filename, f.read_text(encoding="utf-8")
    raise SystemExit("Ни у одной готовой задачи не осталось файла расшифровки.")


def _score(res: dict) -> tuple[int, int, int, int]:
    """Темы, задачи и доля пунктов с дословной цитатой.

    Разметка проверки — список словарей по индексу пункта, но не всякая модель
    отвечает ровно так: встречаются строки и None. Пропускаем такие, а не
    падаем: протокол уже собран, и терять из-за подсчёта весь прогон незачем.
    """
    ver = res.get("verification")
    if not isinstance(ver, dict):
        ver = {}
    total = ok = 0
    for key in ("tasks", "minor_tasks", "done_tasks", "decisions"):
        for item in ver.get(key) or []:
            total += 1
            if isinstance(item, dict) and item.get("ok"):
                ok += 1
    return len(res.get("detailed") or []), len(res.get("tasks") or []), ok, total


# Ключи, за которыми идёт ЗНАЧЕНИЕ. Без этого списка значение попадало в
# позиционные аргументы и молча становилось job_id: скрипт искал задачу с
# идентификатором «gemini,custom:…», не находил и отвечал «готовых задач не
# нашлось» при восьмидесяти восьми готовых задачах в базе.
_WITH_VALUE = ("--models", "--file")


def _parse(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    pos: list[str] = []
    opts: dict[str, str] = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in _WITH_VALUE and i + 1 < len(argv):
            opts[a] = argv[i + 1]
            i += 2
            continue
        if a.startswith("--"):
            opts[a] = ""
            i += 1
            continue
        pos.append(a)
        i += 1
    return pos, opts


def main() -> int:
    args, opts = _parse(sys.argv[1:])
    if not args:
        raise SystemExit("Укажите логин команды первым аргументом.")
    user = args[0]
    job_id = args[1] if len(args) > 1 else None

    models = [m.strip() for m in (opts.get("--models") or "").split(",") if m.strip()]
    if not models:
        raise SystemExit("Укажите движки: --models custom:модель,gemini")

    src = opts.get("--file")
    if src:
        name, text = src, pathlib.Path(src).read_text(encoding="utf-8")
    else:
        name, text = _transcript(user, job_id)
    keys = user_creds.load(user)
    print(f"Встреча: {name}")
    print(f"Расшифровка: {len(text)} символов")
    print("")
    print(f"{'движок':44} {'темы':>5} {'задачи':>7} {'подтверждено':>14} {'время':>8}")
    print("-" * 82)

    for m in models:
        t = time.time()
        try:
            res = analyze.analyze_transcript(text, provider=m, keys=keys)
            # verify_protocol возвращает ВЕСЬ протокол, а разметку кладёт в
            # result["verification"] сам. Присваивание возвращённого обратно в
            # это поле подменяло разметку списком задач — и «подтверждено»
            # выходило 0% у любого движка, включая заведомо рабочий Gemini.
            res = analyze.verify_protocol(res, text, provider=m, keys=keys)
            topics, tasks, ok, total = _score(res)
            share = f"{ok}/{total}" + (f" ({round(100 * ok / total)}%)" if total else "")
            used = res.get("_model") or ""
            label = m if not used or used in m else f"{m} → {used}"
            print(f"{label[:44]:44} {topics:>5} {tasks:>7} {share:>14} {time.time() - t:>7.0f}c")
            if not topics and not tasks:
                # Пустой протокол при честно потраченных минутах — это либо
                # модель не поняла задачу, либо ответ не разобрался. Без этой
                # подсказки в таблице просто нули, и непонятно, что случилось.
                print(f"      пусто. участники={len(res.get('participants') or [])} "
                      f"решения={len(res.get('decisions') or [])} "
                      f"итог={len(str(res.get('summary') or ''))} симв.")
                for k in ("_warning", "_fallback", "analysis_error"):
                    if res.get(k):
                        print(f"      {k}: {str(res[k])[:160]}")
        except Exception as e:                          # noqa: BLE001
            print(f"{m[:44]:44} {'—':>5} {'—':>7} {'—':>14} {time.time() - t:>7.0f}c  "
                  f"{type(e).__name__}: {str(e)[:90]}")
            # Место сбоя, а не только текст: «'str' object has no attribute
            # 'get'» без кадра ничего не говорит о том, где именно чинить.
            for fr in traceback.extract_tb(e.__traceback__)[-3:]:
                print(f"      {fr.filename.split('/')[-1]}:{fr.lineno} в {fr.name}(): {fr.line}")

    print("")
    print("Смотреть в первую очередь на «подтверждено»: это доля пунктов с "
          "дословной цитатой из расшифровки. Много тем при нулевом "
          "подтверждении — protocol, который придётся перепроверять целиком.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
