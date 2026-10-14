from __future__ import annotations

from finengine.contracts import (
    EntityIdentity,
    ExtractedMetric,
    RuleComparison,
    ScalarFact,
    SubjectAccess,
    SubjectProfile,
)
from finengine.skills.atoms.subject_history import subject_key_from_identity


def identity(identifier: str = "5493001KJTIIGC8Y1R12", *, name: str = "ACME") -> EntityIdentity:
    return EntityIdentity(
        entity_id=f"ent-{identifier[-6:]}", canonical_name=name, raw_name=name,
        external_ids={"lei": identifier}, confidence_low=False,
    )


def metric(
    value: float, *, period: str, concept: str = "REVENUE", unit: str = "RUB",
    basis: str = "IFRS", evidence_id: str | None = None, document_version: str | None = None,
) -> ExtractedMetric:
    kwargs = {"evidence_id": evidence_id} if evidence_id else {}
    fact = ScalarFact(
        **kwargs,
        produced_by_atom="read.value", produced_by_atom_version="1.0.0",
        source_ref=f"doc:{document_version or period}", document_version=document_version or period,
        concept=concept, period_ref=period, basis=basis, value=value, unit=unit,
    )
    return ExtractedMetric(scalar_fact=fact, concept=concept, period_ref=period)


def rule(
    actual: float, threshold: float, *, passed: bool, headroom: float,
    rule_id: str, evidence_id: str, reference_id: str = "covenant-1",
) -> RuleComparison:
    return RuleComparison(
        rule_id=rule_id, concept="LEVERAGE", operator="<=", actual_value=actual,
        actual_evidence_id=evidence_id, threshold_value=threshold,
        threshold_reference_id=reference_id, unit="x", delta=actual - threshold,
        relative_delta=(actual - threshold) / abs(threshold), headroom=headroom,
        passed=passed,
    )


def profile(
    version: int, metrics, *, owner: str = "1", plan_run_id: str | None = None,
    registry: str = "registry-1", methodology: str = "method-1", thresholds=(),
    entity: EntityIdentity | None = None,
) -> SubjectProfile:
    entity = entity or identity()
    metrics = list(metrics)
    thresholds = list(thresholds)
    evidence_ids = [m.scalar_fact.evidence_id for m in metrics]
    evidence_ids.extend(r.actual_evidence_id for r in thresholds)
    evidence_ids.extend(r.threshold_reference_id for r in thresholds)
    return SubjectProfile(
        subject_key=subject_key_from_identity(entity), access=SubjectAccess(owner_id=owner),
        version=version, plan_run_id=plan_run_id or f"plan-{version}",
        graph_hash=f"graph-{version}", case_id="case-1",
        registry_version=registry, methodology_version=methodology,
        period_refs=sorted({m.period_ref for m in metrics}),
        document_versions=sorted({m.scalar_fact.document_version for m in metrics}),
        metrics=metrics, thresholds=thresholds, evidence_ids=sorted(set(evidence_ids)),
    )
