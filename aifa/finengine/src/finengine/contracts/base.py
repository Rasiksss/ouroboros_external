"""Shared shape for every A0 contract: frozen, non-empty identifiers.

Каждый контракт неизменяем (нерушимое решение №1 из A0): один и тот же
`BaseModel.model_config` включён везде, чтобы попытка изменить поле после
создания падала одинаково во всех 30 типах, а не только там, где кто-то не
забыл дописать `frozen=True`.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]


def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def evidence_id_of(obj: Any) -> str | None:
    """Domain evidence identity, excluding infrastructure-only result ids."""
    direct = getattr(obj, "evidence_id", None)
    if direct:
        return str(direct)
    for attr in ("scalar_fact", "derived_fact", "human_override"):
        nested = getattr(obj, attr, None)
        value = getattr(nested, "evidence_id", None) if nested is not None else None
        if value:
            return str(value)
    return None


_RESULT_ID_FIELDS = (
    "locator_id", "run_id", "output_id", "artifact_id", "snapshot_id", "node_run_id",
)


def result_id_of(obj: Any) -> str | None:
    """Stable identity for every contract emitted as an executor result."""
    evidence_id = evidence_id_of(obj)
    if evidence_id:
        return evidence_id
    for attr in _RESULT_ID_FIELDS:
        direct = getattr(obj, attr, None)
        if direct:
            return str(direct)
    if isinstance(obj, BaseModel):
        raw = json.dumps({
            "type": f"{type(obj).__module__}.{type(obj).__qualname__}",
            "payload": obj.model_dump(mode="json"),
        }, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return "content:" + hashlib.sha256(raw).hexdigest()
    return None


class Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceRecord(Contract):
    """Общая форма для каждого доказательства, факта и правки из решения №4.

    Исправление не мутирует существующую запись — оно создаёт новую с тем же
    набором полей и `supersedes`, указывающим на исходный `evidence_id`.
    """

    evidence_id: str = Field(default_factory=new_id)
    produced_by_atom: NonEmptyStr
    produced_by_atom_version: NonEmptyStr
    source_ref: NonEmptyStr
    document_version: str | None = None
    concept: str | None = None
    period_ref: str | None = None
    basis: str | None = None
    supersedes: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
