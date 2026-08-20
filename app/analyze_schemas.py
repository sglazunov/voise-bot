"""Контракт ответов модели: Pydantic-схемы протокола и промежуточных заметок.

Вынесено из analyze.py — там это 70 строк деклараций без единой ветки логики,
которые приходилось пролистывать, чтобы добраться до самого разбора. Схемы
намеренно снисходительные: лишние поля игнорируются, всё имеет значение по
умолчанию — слабая модель должна деградировать, а не ронять разбор.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------- #
# Pydantic contract for the LLM answers. One schema serves every provider:
# Ollama gets it as a structured-output grammar (can't produce invalid JSON),
# cloud providers get their answer validated against it with one retry.
# Lenient by design (extra fields ignored, everything defaulted) — a weak
# model's imperfect answer should degrade, not explode.
# --------------------------------------------------------------------------- #
class TopicNote(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    t: str | None = None
    topic: str = ""
    details: str = ""
    quotes: list[str] = Field(default_factory=list)


class DecisionNote(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    t: str | None = None
    text: str = ""
    quote: str | None = None


class TaskNote(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    t: str | None = None
    task: str = ""
    owner: str | None = None
    owner_evidence: str | None = None
    done: bool = False


class MapNotes(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    time_range: str = ""
    participants: list[str] = Field(default_factory=list)
    topics: list[TopicNote] = Field(default_factory=list)
    decisions: list[DecisionNote] = Field(default_factory=list)
    tasks: list[TaskNote] = Field(default_factory=list)


# Final-protocol models are deliberately null-tolerant: a weak cloud model
# answering owner:null must be normalised to «—» downstream, not bounced into
# a needless retry round.
class ProtoParticipant(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    name: str | None = ""
    role: str | None = ""


class ProtoTopic(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    topic: str | None = ""
    details: str | None = ""


class ProtoTask(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    task: str | None = ""
    owner: str | None = "—"


class Protocol(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    participants: list[ProtoParticipant] = Field(default_factory=list)
    summary: str | None = ""
    detailed: list[ProtoTopic] = Field(default_factory=list)
    key_thoughts: list[str | None] = Field(default_factory=list)
    conclusions: list[str | None] = Field(default_factory=list)
    decisions: list[str | None] = Field(default_factory=list)
    done_tasks: list[ProtoTask] = Field(default_factory=list)
    tasks: list[ProtoTask] = Field(default_factory=list)
    minor_tasks: list[ProtoTask] = Field(default_factory=list)

