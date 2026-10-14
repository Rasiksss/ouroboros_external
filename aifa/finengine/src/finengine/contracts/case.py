"""Срез 10: постоянное состояние одной беседы (`Case`) и её `PlanRun`.

Контракты здесь описывают только переносимое состояние. SQL-форма живёт в
`store/cases.py`; planner по-прежнему работает со своими внутренними
`Target`/`NodeArgs`, а на границе persistence они переводятся в эти DTO.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from .base import Contract, NonEmptyStr, new_id, utc_now
from .intent import AdHocConceptSpec, AdHocFormulaSpec, CorrectionIntent, GoalIR
from .planning import PlanGraph
from .run import RunPassport


class CaseIdentity(Contract):
    case_id: NonEmptyStr
    owner_id: NonEmptyStr
    tenant_id: str | None = None


class CaseTarget(Contract):
    output_type: NonEmptyStr
    concept_id: str | None = None
    entity_ref: str | None = None
    period_ref: str | None = None
    basis: str | None = None
    document_version: str | None = None
    policy_version: str | None = None
    scope_id: str | None = None
    params: dict[str, str] = Field(default_factory=dict)


class CaseDocumentRef(Contract):
    logical_name: NonEmptyStr
    document_id: NonEmptyStr
    document_version: NonEmptyStr
    active: bool = True
    added_at: datetime = Field(default_factory=utc_now)


class OverrideChoicePoint(Contract):
    concept: str | None = None
    candidate_evidence_ids: list[str]
    reason: NonEmptyStr


class OverrideAuditEvent(Contract):
    event_id: str = Field(default_factory=new_id)
    action: Literal["apply", "cancel"]
    owner_id: NonEmptyStr
    case_id: NonEmptyStr
    root_evidence_id: NonEmptyStr
    replaced_evidence_id: NonEmptyStr
    override_evidence_id: str | None = None
    reason: str = ""
    original_wording: NonEmptyStr
    idempotency_key: NonEmptyStr
    case_revision: int = Field(ge=1)
    created_at: datetime = Field(default_factory=utc_now)


PlanRunStatus = Literal["planned", "running", "succeeded", "failed", "blocked", "stale"]


class PlanRunRecord(Contract):
    plan_run_id: NonEmptyStr
    case_id: NonEmptyStr
    owner_id: NonEmptyStr
    goal_ir: GoalIR
    target: CaseTarget
    graph: PlanGraph
    graph_hash: NonEmptyStr
    registry_fingerprint: NonEmptyStr
    policy_version: NonEmptyStr
    run_passport: RunPassport | None = None
    run_passport_ref: str | None = None
    node_args: dict[str, dict] = Field(default_factory=dict)
    # Узлы, которые при построении этого конкретного графа были удовлетворены
    # Case-листьями. При resume их совместимость проверяется заново; если лист
    # стал stale (например, заменён документ), старый граф НЕ переисполняется
    # с урезанными requires — PlanRun помечается stale и строится новый delta-plan.
    available_leaf_keys: list[dict] = Field(default_factory=list)
    pending_run_passport: bool = False
    status: PlanRunStatus = "planned"
    case_revision: int = Field(ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class CaseSnapshot(Contract):
    identity: CaseIdentity
    revision: int = Field(default=0, ge=0)
    latest_goal_ir: GoalIR | None = None
    latest_target: CaseTarget | None = None
    latest_graph_hash: str | None = None
    run_passport: RunPassport | None = None
    # Passport is reusable only for the exact document revision it classified.
    # A replacement with the same filename but a different content hash clears
    # this association; another active document does not.
    run_passport_document_version: str | None = None
    active_documents: list[CaseDocumentRef] = Field(default_factory=list)
    # Temporary definitions belong to this owner-scoped Case, never to the
    # canonical registry.  Maps make retries/idempotent replays replace the
    # same spec id instead of appending duplicates.
    ad_hoc_concepts: dict[str, AdHocConceptSpec] = Field(default_factory=dict)
    ad_hoc_formulas: dict[str, AdHocFormulaSpec] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


__all__ = [
    "CaseIdentity", "CaseTarget", "CaseDocumentRef", "CorrectionIntent",
    "OverrideChoicePoint", "OverrideAuditEvent", "PlanRunStatus",
    "PlanRunRecord", "CaseSnapshot",
]
