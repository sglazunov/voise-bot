#!/usr/bin/env python3
"""Сверить requirements.lock.txt с ПОЛНЫМ деревом зависимостей образа.

    pip install --dry-run --ignore-installed --only-binary=:all: \\
        --report /tmp/lock-report.json -r requirements.txt -c requirements.lock.txt
    python scripts/check_lock.py /tmp/lock-report.json

Зачем. CI ставит зависимости без движка распознавания (faster-whisper,
ctranslate2 и их поддерево — onnxruntime, tokenizers, huggingface_hub…), а
образ в CI не собирается. Ложный пин в этом поддереве проходил все тесты и
ломал только сборку на сервере (ревью 30.10, второй раунд). Резолв pip с
`-c lock` по всему requirements.txt падает сам, если пин противоречит
требованиям пакетов или такой версии нет; этот скрипт ловит остальное:

  • пакет из дерева без пина в lock — его версию выберет день сборки
    (класс сбоя PyAV 19);
  • версия в дереве ≠ версии в lock;
  • пин пакета, которого в дереве нет: `-c` такие молча игнорирует, и lock
    врёт о том, что стоит в образе.

Запускать там же, где образ: Linux x86_64, CPython 3.12 (job `lock` в CI) —
маркеры платформы pip считает по машине резолва.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent


def parse_lock(text: str) -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, sep, ver = line.partition("==")
        if not sep or not ver.strip() or any(c in ver for c in "<>=!~*,;"):
            raise ValueError(f"в lock только точные версии «пакет==версия»: {raw!r}")
        pins[canonicalize_name(name.strip().split("[", 1)[0])] = ver.strip()
    return pins


def problems(report: dict, pins: dict[str, str]) -> list[str]:
    resolved = {canonicalize_name(i["metadata"]["name"]): i["metadata"]["version"]
                for i in report.get("install", [])}
    out = []
    for name, ver in sorted(resolved.items()):
        if name not in pins:
            out.append(f"{name}=={ver} есть в дереве, но не закреплён в lock")
        elif pins[name] != ver:
            out.append(f"{name}: в дереве {ver}, в lock {pins[name]}")
    for name in sorted(set(pins) - set(resolved)):
        out.append(f"{name}=={pins[name]} закреплён в lock, но в дереве его нет")
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    report = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    lock = Path(argv[1]) if len(argv) > 1 else ROOT / "requirements.lock.txt"
    errs = problems(report, parse_lock(lock.read_text(encoding="utf-8")))
    for e in errs:
        print(f"lock: {e}", file=sys.stderr)
    if not errs:
        print(f"lock: {len(report.get('install', []))} пакетов дерева совпадают с lock")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
