"""Subject-centric history and deterministic monitoring contracts (Slice 11)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field, model_validator

from .analysis import RuleComparison
from .base import Contract, EvidenceRecord, NonEmptyStr, new_id, utc_now
from .evidence import DerivedFact, ExtractedStructure, MetricEvidence


class SubjectKey(Contract):
    value: NonEmptyStr
    entity_id: NonEmptyStr
    identifiers: dict[str, NonEmptyStr]

    @model_validator(mode="after")
    def _requires_verified_identifier(self):
        if not self.identifiers:
            raise ValueError("SubjectKey requires at least one verified external identifier")
        return self


class SubjectAccess(Contract):
    owner_id: NonEmptyStr
    tenant_id: str | None = None
    scope_id: str | None = None


class SubjectProfile(Contract):
    """One immutable history version produced from a successful verified run."""

    snapshot_id: str = Field(default_factory=new_id)
    subject_key: SubjectKey
    access: SubjectAccess
    version: int = Field(ge=1)
    plan_run_id: NonEmptyStr
    graph_hash: NonEmptyStr
    case_id: NonEmptyStr
    registry_version: NonEmptyStr
    methodology_version: NonEmptyStr
    period_refs: list[NonEmptyStr]
    document_versions: list[NonEmptyStr] = Field(default_factory=list)
    metrics: list[MetricEvidence] = Field(default_factory=list)
    structures: list[ExtractedStructure] = Field(default_factory=list)
    thresholds: list[RuleComparison] = Field(default_factory=list)
    evidence_ids: list[NonEmptyStr] = Field(default_factory=list)
    previous_snapshot_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _is_traceable(self):
        if not self.metrics and not self.structures and not self.thresholds:
            raise ValueError("SubjectProfile requires verified evidence")
        if not self.evidence_ids:
            raise ValueError("SubjectProfile requires evidence references")
        return self


class SubjectSnapshot(Contract):
    """Legacy Phase-0 DTO kept only for serialized-contract compatibility.

    New history code uses :class:`SubjectProfile`; the two are intentionally
    not aliases because the old DTO had no stable identity or owner boundary.
    """

    snapshot_id: str = Field(default_factory=new_id)
    subject_entity: NonEmptyStr
    as_of: date
    key_facts_evidence_ids: list[str] = Field(default_factory=list)
    change_notes: str | None = None
    supersedes: str | None = None

    @model_validator(mode="after")
    def _has_at_least_one_fact(self):
        if not self.key_facts_evidence_ids:
            raise ValueError("a snapshot with no evidence is not observable")
        return self


class SeriesPoint(Contract):
    period_ref: NonEmptyStr
    value: float
    unit: NonEmptyStr
    basis: str | None = None
    evidence_id: NonEmptyStr
    document_version: str | None = None


class SeriesChange(Contract):
    from_period_ref: NonEmptyStr
    to_period_ref: NonEmptyStr
    absolute_change: float
    relative_change: float | None = None
    rate: float | None = None
    annualized_rate: float | None = None
    input_evidence_ids: list[NonEmptyStr]


class SeriesGap(Contract):
    after_period_ref: NonEmptyStr
    before_period_ref: NonEmptyStr
    missing_period_refs: list[NonEmptyStr]


class SeriesFact(EvidenceRecord):
    points: list[SeriesPoint]
    changes: list[SeriesChange] = Field(default_factory=list)
    gaps: list[SeriesGap] = Field(default_factory=list)
    input_evidence_ids: list[NonEmptyStr]
    methodology_version: NonEmptyStr
    interpolation_applied: bool = False


class DecompositionComponent(Contract):
    component_id: NonEmptyStr
    label: NonEmptyStr
    value: float
    derived_fact: DerivedFact


class DecompositionResult(Contract):
    result_id: str = Field(default_factory=new_id)
    concept: NonEmptyStr
    methodology_id: NonEmptyStr
    methodology_version: NonEmptyStr
    target_change: float
    target_input_evidence_ids: list[NonEmptyStr]
    components: list[DecompositionComponent]
    residual: float
    tolerance: float = Field(ge=0)
    complete: bool
    missing_components: list[NonEmptyStr] = Field(default_factory=list)


class PeerMetricValue(Contract):
    concept: NonEmptyStr
    value: float
    unit: NonEmptyStr
    period_ref: NonEmptyStr
    basis: NonEmptyStr
    # Candidate data may arrive without provenance, but policy filtering must
    # exclude it. Keeping the missing state representable prevents callers
    # from inventing a source merely to satisfy schema validation.
    source_ref: str | None = None


class PeerCandidate(Contract):
    stable_peer_id: NonEmptyStr
    display_name: NonEmptyStr
    company_type: NonEmptyStr
    business_model: NonEmptyStr
    standard: NonEmptyStr
    currency: NonEmptyStr
    values: list[PeerMetricValue]


class PeerPolicy(Contract):
    policy_id: NonEmptyStr
    version: NonEmptyStr
    company_type: NonEmptyStr
    business_model: NonEmptyStr
    standard: NonEmptyStr
    currency: NonEmptyStr
    basis: NonEmptyStr
    period_ref: NonEmptyStr
    required_concepts: list[NonEmptyStr]
    min_peers: int = Field(default=1, ge=1)


class PeerExclusion(Contract):
    stable_peer_id: NonEmptyStr
    reasons: list[NonEmptyStr]


class PeerReferenceSet(Contract):
    reference_set_id: str = Field(default_factory=new_id)
    scenario_id: NonEmptyStr
    subject_key: SubjectKey
    policy_id: NonEmptyStr
    policy_version: NonEmptyStr
    peers: list[PeerCandidate]
    excluded: list[PeerExclusion] = Field(default_factory=list)
    sufficient: bool


class WatchSchedule(Contract):
    kind: Literal["manual", "hourly", "daily", "weekly", "event"]
    at_time: str | None = None
    weekdays: list[int] = Field(default_factory=list)
    event_type: str | None = None

    @model_validator(mode="after")
    def _event_or_clock(self):
        if self.kind == "event" and not self.event_type:
            raise ValueError("event schedule requires event_type")
        if self.kind in {"hourly", "daily", "weekly"} and not self.at_time:
            raise ValueError("clock schedule requires at_time")
        return self


class WatchSubscription(Contract):
    subscription_id: str = Field(default_factory=new_id)
    subject_key: SubjectKey
    owner_id: NonEmptyStr
    tenant_id: str | None = None
    case_id: NonEmptyStr
    check_type: Literal["snapshot_diff", "threshold_approach", "snapshot_and_threshold"]
    schedule: WatchSchedule
    allowed_scope: list[NonEmptyStr]
    allowed_actions: list[Literal["run_normal_graph"]] = Field(default_factory=lambda: ["run_normal_graph"])
    policy_version: NonEmptyStr
    scenario_id: str | None = None
    idempotency_key: NonEmptyStr
    active: bool = True


class FactDelta(Contract):
    identity: NonEmptyStr
    old_evidence_id: str | None = None
    new_evidence_id: str | None = None
    old_value: float | None = None
    new_value: float | None = None
    absolute_delta: float | None = None
    relative_delta: float | None = None
    detail: str = ""


class SubjectDiff(Contract):
    diff_id: str = Field(default_factory=new_id)
    subject_key: SubjectKey
    previous_snapshot_id: NonEmptyStr
    current_snapshot_id: NonEmptyStr
    previous_plan_run_id: NonEmptyStr
    current_plan_run_id: NonEmptyStr
    comparability: Literal["comparable", "partial", "incompatible"]
    comparability_issues: list[str] = Field(default_factory=list)
    new_facts: list[FactDelta] = Field(default_factory=list)
    changed_facts: list[FactDelta] = Field(default_factory=list)
    disappeared_facts: list[FactDelta] = Field(default_factory=list)
    changed_structures: list[FactDelta] = Field(default_factory=list)
    changed_thresholds: list[FactDelta] = Field(default_factory=list)


class ApproachPolicy(Contract):
    policy_id: NonEmptyStr
    version: NonEmptyStr
    distance_kind: Literal["relative_to_threshold", "absolute"]
    approach_boundary: float = Field(ge=0)


class ApproachSignal(Contract):
    signal_id: str = Field(default_factory=new_id)
    rule_id: NonEmptyStr
    concept: NonEmptyStr
    status: Literal["approaching", "crossed", "outside", "not_approaching", "insufficient"]
    distance: float | None = None
    previous_distance: float | None = None
    direction: Literal["toward", "away", "unchanged", "unknown"]
    crossed: bool = False
    approaching: bool = False
    policy_id: NonEmptyStr
    policy_version: NonEmptyStr
    narrative: NonEmptyStr


class ApproachReport(Contract):
    report_id: str = Field(default_factory=new_id)
    policy_id: NonEmptyStr
    policy_version: NonEmptyStr
    signals: list[ApproachSignal] = Field(default_factory=list)


class MonitorRunTrace(Contract):
    monitor_plan_run_id: NonEmptyStr
    source_snapshot_ids: list[NonEmptyStr]
    source_plan_run_ids: list[NonEmptyStr]


class MonitorResult(Contract):
    """`output_type` — дискриминант терминального typed-результата (Срез 27):
    делает `MonitorResult` адресуемым в `result_events.TypedResultModel`
    наравне с `ReportOutput`/`FormOutput`, тем же полем, на которое уже
    опирается station listener и веб-фронтенд."""

    schema_version: Literal["1.0"] = "1.0"
    output_type: Literal["MonitorResult"] = "MonitorResult"
    status: Literal["complete"] = "complete"
    result_id: str = Field(default_factory=new_id)
    subject_key: SubjectKey
    diff: SubjectDiff
    series: list[SeriesFact] = Field(default_factory=list)
    approaches: ApproachReport
    trace: MonitorRunTrace


class MonitoringCapacity(Contract):
    subject_count: int = Field(ge=0)
    snapshot_count: int = Field(ge=0)
    serialized_bytes: int = Field(ge=0)
    active_subscriptions: int = Field(ge=0)
    estimated_runs_per_day: float = Field(ge=0)
