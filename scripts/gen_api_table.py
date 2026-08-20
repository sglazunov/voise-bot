#!/usr/bin/env python3
"""Пересобрать таблицу API в docs/БЕКЕНД.md §13 прямо из маршрутов приложения.

Таблицу вели руками, и она разошлась с кодом: в ней остались давно удалённые
маршруты и не было полутора десятков живых. Теперь она генерируется — запускать
после изменения маршрутов:

    python scripts/gen_api_table.py

Скрипт заменяет всё между маркерами <!-- API:начало --> и <!-- API:конец -->.
Назначение берётся из первой строки докстринга обработчика.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Импорт приложения не должен трогать боевые данные.
os.environ.setdefault("VTX_DATA_DIR", tempfile.mkdtemp(prefix="vtx-apidoc-"))
os.environ.pop("DATABASE_URL", None)
os.environ["VTX_AUTO_SETUP"] = "0"

# Заглушка faster_whisper: сборка таблицы не должна требовать установленного
# движка распознавания (~1 ГБ). Ровно так же делает tests/conftest.py.
if "faster_whisper" not in sys.modules:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        import types
        stub = types.ModuleType("faster_whisper")
        stub.WhisperModel = object
        utils = types.ModuleType("faster_whisper.utils")
        utils._MODELS = {}
        stub.utils = utils
        sys.modules["faster_whisper"] = stub
        sys.modules["faster_whisper.utils"] = utils

from app.main import app  # noqa: E402

DOC = ROOT / "docs" / "БЕКЕНД.md"
START, END = "<!-- API:начало -->", "<!-- API:конец -->"

# Порядок разделов; маршрут попадает в первый подошедший.
GROUPS: list[tuple[str, str]] = [
    ("Аутентификация и профиль", r"^/api/(auth|profile)"),
    ("Задачи распознавания", r"^/api/jobs|^/api/search|^/api/stats|^/api/presets|^/api/prompt"),
    ("Провайдеры LLM и контекст", r"^/api/providers|^/api/engines|^/api/context|^/api/ollama"),
    ("Готовность и модели", r"^/api/(setup|model|system)"),
    ("Автоматизация встреч", r"^/api/automation"),
    ("Прочее", r"^/api/"),
    ("Страницы и служебное", r"."),
]


# Служебные маршруты FastAPI и страницы без обработчика-докстринга.
_KNOWN: dict[str, str] = {
    "/": "React-SPA «MeetFlowAI» — весь интерфейс.",
    "/login": "Страница входа (Jinja).",
    "/register": "Страница регистрации (Jinja).",
    "/recover": "Страница восстановления пароля (Jinja).",
    "/healthz": "Health-check.",
    "/docs": "Интерактивная документация OpenAPI (под гейтом авторизации).",
    "/redoc": "Та же схема OpenAPI в оформлении ReDoc.",
    "/openapi.json": "Схема OpenAPI в JSON.",
    "/docs/oauth2-redirect": "Служебный маршрут Swagger UI.",
}


def summary(route) -> str:
    """Первое предложение докстринга обработчика (часть из них ещё на английском)."""
    doc = (getattr(route, "endpoint", None).__doc__ or "").strip()
    if not doc:
        return _KNOWN.get(getattr(route, "path", ""), "—")
    text = re.sub(r"\s+", " ", doc)
    # Обрываем по концу предложения, а не по строке: докстринги переносятся.
    m = re.search(r"^(.{0,160}?[.!?])(\s|$)", text)
    out = (m.group(1) if m else text[:160].rstrip()).strip()
    return out.replace("|", "\|")


def main() -> int:
    rows: dict[str, list[tuple[str, str, str]]] = {name: [] for name, _ in GROUPS}
    for r in app.routes:
        path = getattr(r, "path", "")
        methods = getattr(r, "methods", None)
        if not path or not methods:
            continue
        if path.startswith("/app/assets") or "{full_path" in path:
            continue
        for method in sorted(m for m in methods if m not in ("HEAD", "OPTIONS")):
            for name, pattern in GROUPS:
                if re.search(pattern, path):
                    rows[name].append((method, path, summary(r)))
                    break

    out = [START, ""]
    for name, _ in GROUPS:
        group = sorted(set(rows[name]), key=lambda t: (t[1], t[0]))
        if not group:
            continue
        out += [f"### {name}", "", "| Метод | Путь | Назначение |", "|---|---|---|"]
        for method, path, text in group:
            out.append(f"| {method} | `{path}` | {text} |")
        out.append("")
    out.append(END)

    text = DOC.read_text(encoding="utf-8")
    if START not in text or END not in text:
        print("В БЕКЕНД.md нет маркеров <!-- API:начало --> / <!-- API:конец -->")
        return 1
    head = text[:text.index(START)]
    tail = text[text.index(END) + len(END):]
    DOC.write_text(head + "\n".join(out) + tail, encoding="utf-8")
    total = sum(len(set(v)) for v in rows.values())
    print(f"Таблица API пересобрана: {total} маршрутов")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
