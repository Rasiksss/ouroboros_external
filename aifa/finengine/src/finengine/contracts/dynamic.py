"""Результаты временных атомов гибридного планировщика.

Один класс на все схемы: хранилище Case восстанавливает payload по имени
класса (store/cases.py::_unpack_value), динамических классов быть не может.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from .base import Contract, NonEmptyStr, new_id

Origin = Literal["reported", "calculated", "user_provided", "generated", "external"]


class DynamicArtifact(Contract):
    evidence_id: str = Field(default_factory=new_id)
    output_type: NonEmptyStr               # "dyn:<name>" — по нему стыкуются атомы
    payload: dict[str, Any]                # валиден по output_schema атома-производителя
    origin: list[Origin]                   # ставит обёртка исполнителя, не код атома
    producer: NonEmptyStr                  # "eph.<name>@<version>"
    input_evidence_ids: list[str] = Field(default_factory=list)

    @field_validator("output_type")
    @classmethod
    def _dyn_only(cls, value: str) -> str:
        if not value.startswith("dyn:"):
            raise ValueError("DynamicArtifact.output_type обязан начинаться с 'dyn:'")
        return value


class EphemeralAtomSource(Contract):
    """Исходник временного атома. Хранится в Case обычным артефактом: даёт
    провенанс (результат ↔ код) и повторное использование атома в разговоре."""
    evidence_id: NonEmptyStr               # = версия атома (хэш)
    name: NonEmptyStr
    request: dict[str, Any]                # AtomRequest в JSON
    code: str = ""                         # пусто у атома-загрузки
