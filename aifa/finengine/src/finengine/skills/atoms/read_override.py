"""`read.override` — Срез 10: пользовательская поправка как новое evidence.

Исходный ScalarFact/DerivedFact не мутируется. Атом получает УЖЕ однозначно
выбранное MetricEvidence из owner-scoped Case; выбор кандидата и аудит живут в
`skills.case_runtime`/`store.cases`, а эта функция проверяет совместимость
периода/единицы и строит immutable HumanOverride + UserProvidedMetric.
"""
from __future__ import annotations

import json
from typing import Any

from finengine.contracts import (
    CalculatedMetric, CorrectionIntent, ExtractedMetric, HumanOverride, RunPassport,
    UserProvidedMetric,
)
from finengine.skills.spec.atom_registry import NodeArgs
from finengine.skills.spec.target import InputSpec

ATOM_VERSION = "1.0.0"

MetricLike = ExtractedMetric | CalculatedMetric | UserProvidedMetric


def metric_evidence_id(metric: MetricLike) -> str:
    if isinstance(metric, ExtractedMetric):
        return metric.scalar_fact.evidence_id
    if isinstance(metric, CalculatedMetric):
        return metric.derived_fact.evidence_id
    return metric.human_override.evidence_id


def metric_value_unit_period(metric: MetricLike) -> tuple[float, str | None, str, str | None, str | None]:
    if isinstance(metric, ExtractedMetric):
        f = metric.scalar_fact
        return f.value, f.unit, f.period_ref, f.basis, f.document_version
    if isinstance(metric, CalculatedMetric):
        f = metric.derived_fact
        return f.value, f.unit, f.period_ref or metric.period_ref, f.basis, f.document_version
    h = metric.human_override
    if h.numeric_value is None:
        try:
            value = float(h.new_value.replace(",", "."))
        except ValueError as exc:  # pragma: no cover - старый non-numeric override
            raise ValueError("active HumanOverride is not numeric") from exc
    else:
        value = h.numeric_value
    return value, h.unit, h.period_ref or metric.period_ref, h.basis, h.document_version


def validate_correction(original: MetricLike, correction: CorrectionIntent) -> None:
    if correction.action != "apply":
        raise ValueError("read.override creates only apply evidence; cancel is an audit/selection event")
    if correction.value is None:
        raise ValueError("read.override: numeric correction requires value")
    _value, unit, period, basis, _doc_version = metric_value_unit_period(original)
    if correction.period_ref and correction.period_ref != period:
        raise ValueError(
            f"read.override: period mismatch: correction={correction.period_ref!r}, evidence={period!r}"
        )
    if correction.unit and unit and correction.unit != unit:
        raise ValueError(f"read.override: unit mismatch: correction={correction.unit!r}, evidence={unit!r}")
    if correction.basis and basis and correction.basis != basis:
        raise ValueError(f"read.override: basis mismatch: correction={correction.basis!r}, evidence={basis!r}")


def create_override(
    original: MetricLike, correction: CorrectionIntent, *, owner_id: str, case_id: str,
) -> tuple[HumanOverride, UserProvidedMetric]:
    validate_correction(original, correction)
    _old_value, old_unit, period, basis, document_version = metric_value_unit_period(original)
    evidence_id = metric_evidence_id(original)
    unit = correction.unit or old_unit
    reason = correction.reason.strip() or "user correction"
    override = HumanOverride(
        produced_by_atom="read.override", produced_by_atom_version=ATOM_VERSION,
        source_ref=f"user:{owner_id}/case:{case_id}", document_version=document_version,
        concept=original.concept, period_ref=period, basis=basis, supersedes=evidence_id,
        overridden_evidence_id=evidence_id, new_value=str(correction.value),
        reviewer=owner_id, justification=reason, owner_id=owner_id, case_id=case_id,
        original_wording=correction.original_wording, reason=reason,
        numeric_value=correction.value, unit=unit,
    )
    wrapper = UserProvidedMetric(
        human_override=override, concept=original.concept, period_ref=period,
        definition_origin=original.definition_origin,
    )
    return override, wrapper


def read_override_requires(node_args: NodeArgs, run_passport: RunPassport | None) -> tuple[InputSpec, ...]:
    del run_passport
    if not node_args.concept:
        return ()
    return (InputSpec(input_type="MetricEvidence", concept=node_args.concept),)


def _intent_from_args(args: NodeArgs) -> CorrectionIntent:
    raw = args.extra.get("correction_json")
    if not raw:
        raise ValueError("read.override: missing correction_json in NodeArgs.extra")
    return CorrectionIntent.model_validate(json.loads(raw))


def read_override_executor(args: NodeArgs, ctx: Any) -> list:
    correction = _intent_from_args(args)
    owner_id = args.extra.get("owner_id") or ""
    case_id = args.extra.get("case_id") or ""
    expected_evidence_id = args.extra.get("overridden_evidence_id") or correction.evidence_id
    if not owner_id or not case_id or not expected_evidence_id:
        raise ValueError("read.override: owner_id/case_id/exact overridden_evidence_id are required")
    candidates = [
        e for e in ctx.dependencies()
        if isinstance(e, (ExtractedMetric, CalculatedMetric, UserProvidedMetric))
        and e.concept == args.concept and metric_evidence_id(e) == expected_evidence_id
    ]
    if len(candidates) != 1:
        raise ValueError(
            f"read.override: exact evidence binding failed for {expected_evidence_id!r}: {len(candidates)} candidates"
        )
    _override, wrapper = create_override(candidates[0], correction, owner_id=owner_id, case_id=case_id)
    return [wrapper]


__all__ = [
    "ATOM_VERSION", "MetricLike", "metric_evidence_id", "metric_value_unit_period",
    "validate_correction", "create_override", "read_override_requires", "read_override_executor",
]
