#!/usr/bin/env python3
"""Почему NVIDIA не отвечает: пробуем по одному способу и печатаем, что вышло.

Запуск в контейнере:
    docker compose exec app python scripts/nvidia_diag.py [логин]

Логин нужен, чтобы взять ключ команды из хранилища; без него берётся
NVIDIA_API_KEY из окружения. Ключ нигде не печатается.

Проверяем по очереди, чтобы отделить причины друг от друга:
  1. каталог моделей         — жив ли ключ вообще;
  2. поток + response_format — то, чем ходит рабочий код;
  3. поток без response_format — не в нём ли дело;
  4. обычный запрос          — тот самый, что отвечает 504.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/app")

from app import config, llm_nvidia                      # noqa: E402
from app.llm_nvidia import _nvidia_timeout, _safe_url   # noqa: E402

URL = "https://integrate.api.nvidia.com/v1/chat/completions"
# Промпт нарочно длинный: 504 у шлюза возникает именно на долгой генерации.
PROMPT = ("Ниже — фрагмент рабочей встречи. Верни ТОЛЬКО JSON вида "
          '{"topics": [{"topic": "...", "details": "..."}]} — не менее пятнадцати '
          "тем, каждая с подробным описанием на три-четыре предложения.\n\n"
          + ("Обсудили сроки, бюджет, дизайн, тесты, найм и релиз. " * 200))


def _key(user: str | None) -> str:
    if user:
        from app import user_creds
        entries = user_creds.load(user).get("nvidia") or []
        if entries:
            return entries[0].get("key") or ""
    return config.NVIDIA_API_KEY or ""


def _say(step: str, ok: bool, detail: str, secs: float) -> None:
    mark = "OK  " if ok else "СБОЙ"
    print(f"[{mark}] {step}: {detail}   ({secs:.1f} c)")


def main() -> int:
    user = sys.argv[1] if len(sys.argv) > 1 else None
    key = _key(user)
    if not key:
        print("Ключ NVIDIA не найден. Укажите логин команды первым аргументом.")
        return 2
    print(f"Ключ: …{key[-6:]}   поток включён: {llm_nvidia._NVIDIA_STREAM}   "
          f"таймаут куска: {llm_nvidia._NVIDIA_CHUNK_TIMEOUT} c")

    t = time.time()
    try:
        models = llm_nvidia.nvidia_models(key)
        _say("1. каталог моделей", True, f"{len(models)} шт., первая — {models[0] if models else '—'}",
             time.time() - t)
    except Exception as e:                              # noqa: BLE001
        _say("1. каталог моделей", False, str(e)[:200], time.time() - t)
        return 1

    model = llm_nvidia.nvidia_default_model(key)
    print(f"Модель по умолчанию: {model}")
    headers = {"Authorization": f"Bearer {key}"}
    base = {"model": model, "messages": [{"role": "user", "content": PROMPT}],
            "max_tokens": 4000, "temperature": 0.2}

    for step, payload in (
            ("2. поток + response_format", {**base, "response_format": {"type": "json_object"}}),
            ("3. поток без response_format", dict(base))):
        t = time.time()
        try:
            text = llm_nvidia._nvidia_stream(URL, payload, headers)
            _say(step, True, f"получено {len(text)} символов", time.time() - t)
        except Exception as e:                          # noqa: BLE001
            _say(step, False, str(e)[:300], time.time() - t)

    t = time.time()
    tmo = _nvidia_timeout(base["max_tokens"])
    try:
        body = json.dumps({**base, "response_format": {"type": "json_object"}}).encode()
        req = urllib.request.Request(URL, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        for k, v in headers.items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=tmo) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        n = len(out["choices"][0]["message"]["content"])
        _say(f"4. обычный запрос (таймаут {tmo} c)", True, f"получено {n} символов",
             time.time() - t)
    except urllib.error.HTTPError as e:
        _say(f"4. обычный запрос (таймаут {tmo} c)", False,
             f"HTTP {e.code} от {_safe_url(URL)}: {e.read().decode('utf-8', 'replace')[:200]}",
             time.time() - t)
    except Exception as e:                              # noqa: BLE001
        _say(f"4. обычный запрос (таймаут {tmo} c)", False, str(e)[:200], time.time() - t)

    print("\nЧитать так: если 2 сбоит, а 3 проходит — мешает response_format. "
          "Если сбоят оба, а 4 проходит — поток вообще не работает. "
          "Если всё сбоит — дело в ключе или в самой NVIDIA.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
