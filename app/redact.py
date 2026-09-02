"""Маскирование секретов в тексте, который уходит в модель и в протокол.

Зачем: 27.08.2026 на встрече «Виртуальный помощник — с заказчиком» разработчик
показал экран с файлом `.env`. OCR прочитал его целиком, модель добросовестно
пересказала «ключи Google OAuth, Yandex API, OpenAI, DeepSeek и Langfuse…», и
всё это ушло в .docx, в облако и в Weeek. Секреты на экране — не содержание
встречи, а утечка.

Правило простое: всё, что похоже на ключ/токен/пароль, заменяется на «[скрыто:
тип]» ещё ДО того, как текст увидит модель. Имя переменной остаётся — по нему
понятно, что показывали, значение — нет.
"""
from __future__ import annotations

import re

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Приватные ключи целиком (PEM).
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "[скрыто: приватный ключ]"),
    # Строки подключения с паролем: postgres://user:pass@host → пароль скрыт.
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@]+(@)"),
     r"\1[скрыто]\2"),
    # Пары «имя=значение» / «имя: значение», где имя выдаёт секрет. Имя
    # остаётся, значение скрывается. Ловит .env, yaml, json, ini.
    (re.compile(
        r"(?i)\b((?:[A-Z0-9_]*(?:PASSWORD|PASSWD|PWD|SECRET|TOKEN|API[_\-]?KEY|APIKEY|"
        r"ACCESS[_\-]?KEY|PRIVATE[_\-]?KEY|CLIENT[_\-]?SECRET|AUTH|CREDENTIAL)[A-Z0-9_]*)"
        r"\s*[:=]\s*[\"']?)([^\s\"',;]{6,})"),
     r"\1[скрыто]"),
    # Известные префиксы ключей провайдеров.
    (re.compile(r"\bsk-(?:proj-|ant-|or-)?[A-Za-z0-9_\-]{16,}"), "[скрыто: api-ключ]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), "[скрыто: google-ключ]"),
    (re.compile(r"\bgsk_[A-Za-z0-9]{20,}"), "[скрыто: api-ключ]"),
    (re.compile(r"\bpplx-[A-Za-z0-9]{20,}"), "[скрыто: api-ключ]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), "[скрыто: github-токен]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "[скрыто: slack-токен]"),
    (re.compile(r"\bya29\.[A-Za-z0-9_\-]{20,}"), "[скрыто: oauth-токен]"),
    (re.compile(r"\by[0-3]_[A-Za-z0-9_\-]{30,}"), "[скрыто: yandex-токен]"),
    (re.compile(r"\bAQVN[A-Za-z0-9_\-]{30,}"), "[скрыто: yandex-ключ]"),
    (re.compile(r"\bt1\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}"), "[скрыто: iam-токен]"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,45}\b"), "[скрыто: telegram-токен]"),
    # JWT: три base64url-части через точку.
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
     "[скрыто: jwt]"),
    # Длинные hex/base64-строки без пробелов — типичный вид ключа. Порог
    # высокий, чтобы не задеть обычные слова и URL.
    (re.compile(r"(?<![A-Za-z0-9/+=_\-])[A-Fa-f0-9]{32,}(?![A-Za-z0-9/+=_\-])"),
     "[скрыто: hex]"),
    (re.compile(r"(?<![A-Za-z0-9/+=_\-])(?=[A-Za-z0-9+/_\-]*\d)(?=[A-Za-z0-9+/_\-]*[A-Za-z])"
                r"[A-Za-z0-9+/_\-]{40,}={0,2}(?![A-Za-z0-9/+=_\-])"),
     "[скрыто: токен]"),
]


def redact(text: str | None) -> tuple[str, int]:
    """Вернуть (текст с замаскированными секретами, сколько замен сделано)."""
    if not text:
        return "", 0
    out = text
    total = 0
    for pat, repl in _PATTERNS:
        out, n = pat.subn(repl, out)
        total += n
    return out, total


def redact_text(text: str | None) -> str:
    return redact(text)[0]
