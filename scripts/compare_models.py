#!/usr/bin/env python3
"""Сравнить движки на ОДНОЙ И ТОЙ ЖЕ расшифровке — и выбрать по цифрам.

    docker compose exec app python scripts/compare_models.py <логин> [job_id] \
        [--models nvidia:a,nvidia:b,gemini]

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

import sys
import time

sys.path.insert(0, "/app")

from app import analyze, user_creds                     # noqa: E402
from app.jobs import STATUS_DONE, store                 # noqa: E402


def _transcript(user: str, job_id: str | None) -> tuple[str, str]:
    jobs = [j for j in store.list(user) if j.status == STATUS_DONE]
    if job_id:
        jobs = [j for j in jobs if j.id == job_id]
    if not jobs:
        raise SystemExit("Готовых задач не нашлось — укажите job_id явно.")
    job = jobs[0]
    for fmt in ("txt", "plain"):
        p = store.result_path(job.id, fmt)
        if p.exists():
            return job.filename, p.read_text(encoding="utf-8")
    raise SystemExit(f"У задачи {job.id} нет файла расшифровки.")


def _score(res: dict) -> tuple[int, int, int, int]:
    ver = (res.get("verification") or {})
    total = ok = 0
    for key in ("tasks", "minor_tasks", "done_tasks", "decisions"):
        for item in ver.get(key) or []:
            total += 1
            ok += 1 if (item or {}).get("ok") else 0
    return len(res.get("detailed") or []), len(res.get("tasks") or []), ok, total


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit("Укажите логин команды первым аргументом.")
    user = args[0]
    job_id = args[1] if len(args) > 1 else None

    models = None
    for i, a in enumerate(sys.argv):
        if a == "--models" and i + 1 < len(sys.argv):
            models = [m.strip() for m in sys.argv[i + 1].split(",") if m.strip()]
    if not models:
        raise SystemExit("Укажите движки: --models nvidia:модель,gemini")

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
            res["verification"] = analyze.verify_protocol(
                res, text, provider=m, keys=keys)
            topics, tasks, ok, total = _score(res)
            share = f"{ok}/{total}" + (f" ({round(100 * ok / total)}%)" if total else "")
            used = res.get("_model") or ""
            label = m if not used or used in m else f"{m} → {used}"
            print(f"{label[:44]:44} {topics:>5} {tasks:>7} {share:>14} {time.time() - t:>7.0f}c")
        except Exception as e:                          # noqa: BLE001
            print(f"{m[:44]:44} {'—':>5} {'—':>7} {'—':>14} {time.time() - t:>7.0f}c  "
                  f"{str(e)[:100]}")

    print("")
    print("Смотреть в первую очередь на «подтверждено»: это доля пунктов с "
          "дословной цитатой из расшифровки. Много тем при нулевом "
          "подтверждении — protocol, который придётся перепроверять целиком.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
