#!/usr/bin/env python3
"""Почему NVIDIA не отвечает: пробуем по одному способу и печатаем, что вышло.

Запуск в контейнере:
    docker compose exec app python scripts/nvidia_diag.py <логин> [--long]

Логин нужен, чтобы взять ключ команды из хранилища; без него берётся
NVIDIA_API_KEY из окружения. Ключ нигде не печатается.

Сначала идут БЫСТРЫЕ проверки (секунды): жив ли ключ и какие модели вообще
отвечают. Тяжёлая проверка на длинном промпте — только с флагом --long: она
занимает до нескольких минут, потому что 504 у шлюза и 529 «перегружены»
воспроизводятся именно на долгой генерации.
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
SHORT_WAIT = 45          # короткой пробе больше не нужно
LONG_WAIT = 240          # столько ждём первый кусок на длинном промпте
# Промпт нарочно длинный: 504 у шлюза возникает именно на долгой генерации.
LONG_PROMPT = ("Ниже — фрагмент рабочей встречи. Верни ТОЛЬКО JSON вида "
               '{"topics": [{"topic": "...", "details": "..."}]} — не менее '
               "пятнадцати тем, каждая с описанием на три-четыре предложения.\n\n"
               + ("Обсудили сроки, бюджет, дизайн, тесты, найм и релиз. " * 200))


def _key(user: str | None) -> str:
    if user:
        from app import user_creds
        entries = user_creds.load(user).get("nvidia") or []
        if entries:
            return entries[0].get("key") or ""
    return config.NVIDIA_API_KEY or ""


def _say(step: str, ok: bool, detail: str, secs: float) -> None:
    print(f"[{'OK  ' if ok else 'СБОЙ'}] {step}: {detail}   ({secs:.1f} c)")


def _stream(payload: dict, headers: dict, wait: int) -> str:
    return llm_nvidia._nvidia_stream(URL, payload, headers, timeout=wait,
                                     first_timeout=wait)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    long_run = "--long" in sys.argv
    user = args[0] if args else None
    key = _key(user)
    if not key:
        print("Ключ NVIDIA не найден. Укажите логин команды первым аргументом.")
        return 2
    print(f"Ключ: …{key[-6:]}   поток включён: {llm_nvidia._NVIDIA_STREAM}")

    t = time.time()
    try:
        models = llm_nvidia.nvidia_models(key)
        _say("каталог моделей", True,
             f"{len(models)} шт., первая — {models[0] if models else '—'}",
             time.time() - t)
    except Exception as e:                              # noqa: BLE001
        _say("каталог моделей", False, str(e)[:200], time.time() - t)
        return 1

    default = llm_nvidia.nvidia_default_model(key)
    headers = {"Authorization": f"Bearer {key}"}
    print(f"Модель по умолчанию: {default}")

    # Повторы на время проверки выключаем: нужен ПЕРВЫЙ ответ, а не итог
    # после пауз — иначе неясно, что именно ответил сервис.
    saved, llm_nvidia._STREAM_RETRIES = llm_nvidia._STREAM_RETRIES, 0
    try:
        print("")
        print(f"Короткая проба моделей (по {SHORT_WAIT} c на модель) — кто вообще отвечает:")
        short = {"messages": [{"role": "user", "content": "Ответь одним словом: привет"}],
                 "max_tokens": 16, "temperature": 0}
        probe = [default] + [m for m in models[:8] if m != default]
        free = []
        for mid in probe:
            t = time.time()
            try:
                _stream({**short, "model": mid}, headers, SHORT_WAIT)
                free.append(mid)
                _say(f"  {mid}", True, "отвечает", time.time() - t)
            except Exception as e:                      # noqa: BLE001
                _say(f"  {mid}", False, str(e)[:140], time.time() - t)

        print("")
        if free:
            print("Отвечают: " + ", ".join(free))
            if default not in free:
                print(f"Текущая модель ({default}) НЕ отвечает, а другие — да. "
                      "Смените её на странице «Нейросети» на любую из списка.")
        else:
            print("Не ответила ни одна модель — занят весь бесплатный сервис "
                  "NVIDIA либо ключ исчерпан. Протоколы будет собирать "
                  "запасной движок, это штатное поведение.")

        if not long_run:
            print("")
            print("Проверка на длинном промпте (та, где возникают 529 и 504) "
                  "не запускалась: добавьте --long, она занимает до "
                  f"{LONG_WAIT} c на шаг.")
            return 0

        base = {"model": default,
                "messages": [{"role": "user", "content": LONG_PROMPT}],
                "max_tokens": 4000, "temperature": 0.2}
        print("")
        print(f"Длинный промпт. Каждый шаг ждёт до {LONG_WAIT} c — это нормально, "
              "не прерывайте.")
        for step, payload in (
                ("поток + response_format",
                 {**base, "response_format": {"type": "json_object"}}),
                ("поток без response_format", dict(base))):
            t = time.time()
            try:
                text = _stream(payload, headers, LONG_WAIT)
                _say(step, True, f"получено {len(text)} символов", time.time() - t)
            except Exception as e:                      # noqa: BLE001
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
            _say(f"обычный запрос (таймаут {tmo} c)", True,
                 f"получено {n} символов", time.time() - t)
        except urllib.error.HTTPError as e:
            _say(f"обычный запрос (таймаут {tmo} c)", False,
                 f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}",
                 time.time() - t)
        except Exception as e:                          # noqa: BLE001
            _say(f"обычный запрос (таймаут {tmo} c)", False, str(e)[:200],
                 time.time() - t)
    finally:
        llm_nvidia._STREAM_RETRIES = saved

    print("")
    print("Читать так: 529 «Overloaded» — занят инференс NVIDIA, ключ и права "
          "ни при чём. Молчание до таймаута — тоже перегрузка, только без "
          "ответа. Сбоит лишь шаг с response_format — мешает он. Проходит "
          "поток, но не обычный запрос — так и задумано, длинный ответ живёт "
          "только в потоке.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
