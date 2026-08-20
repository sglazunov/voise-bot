"""Общие зависимости FastAPI.

Отдельный модуль, чтобы маршруты можно было разложить по роутерам, не замыкая
импорт на main.py: main импортирует роутеры, роутеры импортируют отсюда.
"""
from __future__ import annotations

from fastapi import HTTPException, Request
from pydantic import BaseModel


def current_user(request: Request) -> str:
    """Логин вошедшего. Ставится middleware-воротами до любого маршрута; если
    его нет — значит, ворота пропустили запрос без входа, и это 401."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "Требуется вход.")
    return user


class NotesBody(BaseModel):
    """Заметки участника: и у задачи распознавания, и у встречи."""

    notes: str = ""
