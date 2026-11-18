"""``out.form``: exact layout projection over already-produced evidence.

No formulas, ratios, signs or OCR are interpreted here. The atom only selects
typed values and copies them into declared layout rows. Under the explicit
``period_scope=all_available`` policy it creates one row per period already
present in evidence; it never invents or selects a period itself.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

from finengine.contracts import (
    CalculatedMetric,
    EvidenceRef,
    ExtractedMetric,
    FormOutput,
    FormRow,
    OutputGap,
    OutputRunMetadata,
    UserProvidedMetric,
)
from finengine.roles.scope import layouts
from finengine.skills.periods import normalize_period_label, period_kind_for_enum

ATOM_VERSION = "1.0.0"

Metric = ExtractedMetric | CalculatedMetric | UserProvidedMetric


def _payload(metric: Metric):
    if isinstance(metric, ExtractedMetric):
        return metric.scalar_fact
    if isinstance(metric, CalculatedMetric):
        return metric.derived_fact
    return metric.human_override


def _evidence_ref(metric: Metric) -> EvidenceRef:
    fact = _payload(metric)
    return EvidenceRef(
        evidence_id=fact.evidence_id,
        concept=metric.concept,
        period_ref=metric.period_ref,
        source_ref=fact.source_ref,
        document_version=fact.document_version,
    )


def _value(metric: Metric):
    fact = _payload(metric)
    return getattr(fact, "value", None) if not isinstance(metric, UserProvidedMetric) else fact.numeric_value


def build_form_output(
    *, run_id: str, role: str, company_type: str, metrics: Iterable[Metric], passport=None,
    requested_concepts: tuple[str, ...] = (),
    requested_periods: tuple[str, ...] = (),
) -> FormOutput:
    root = layouts()
    role_cfg = (root.get("roles") or {}).get(role) or {}
    fmt = (role_cfg.get("formats") or {}).get(company_type)
    if not fmt:
        raise ValueError(f"layout is not defined for role={role!r}, company_type={company_type!r}")

    layout_id = f"{role}.{company_type}"
    layout_version = str(root.get("version") or "1")
    preserve_empty = bool(fmt.get("preserve_empty"))
    row_specs = list(fmt.get("rows") or [])
    if requested_concepts:
        # Targeted extraction already exists as a product behavior.  Represent
        # it as a narrow typed form instead of invoking the full role layout or
        # reconstructing rows from markdown downstream.
        layout_id = f"requested.{company_type}"
        preserve_empty = True
        row_specs = [
            {"kind": "metric", "enum": concept, "label": concept}
            for concept in dict.fromkeys(requested_concepts)
        ]
    if requested_periods:
        layout_id += ".all-periods"

    by_concept: dict[str, list[Metric]] = defaultdict(list)
    for metric in metrics:
        by_concept[metric.concept].append(metric)

    rows: list[FormRow] = []
    gaps: list[OutputGap] = []

    def _append_metric_row(*, enum: str, label: str, kind: str, candidates: list[Metric]) -> None:
        common = {
            "row_id": f"{layout_id}:{len(rows):03d}",
            "order": len(rows),
            "kind": kind,
            "enum": enum,
            "label": label,
        }
        if not candidates:
            rows.append(FormRow(
                **common, status="missing", missing_reason="evidence_not_found",
            ))
            gaps.append(OutputGap(
                code="missing_form_value", concept=enum,
                message=f"Для строки «{label}» нет типизированного значения",
            ))
            return

        values = {_value(metric) for metric in candidates}
        refs = [_evidence_ref(metric) for metric in candidates]
        if len(values) != 1:
            rows.append(FormRow(
                **common, status="conflicting", evidence_refs=refs,
                missing_reason="conflicting_evidence",
            ))
            gaps.append(OutputGap(
                code="conflicting_form_value", concept=enum,
                message=f"Для строки «{label}» найдены противоречивые значения",
            ))
            return

        value = next(iter(values))
        if value is None:
            rows.append(FormRow(
                **common, status="missing", evidence_refs=refs,
                missing_reason="explicit_missing_value",
            ))
            gaps.append(OutputGap(
                code="explicit_missing_form_value", concept=enum,
                message=f"Для строки «{label}» доказательство явно не содержит значения",
            ))
            return
        rows.append(FormRow(
            **common, value=value, display_value=str(value), status="supported", evidence_refs=refs,
        ))

    def _label_kind(period: str) -> str:
        normalized = normalize_period_label(period)
        return "duration" if normalized.startswith(("FY ", "Q")) or re.match(r"^\d{1,2}M ", normalized) else "point"

    for spec in row_specs:
        enum = str(spec.get("enum") or "").strip() or None
        kind = str(spec.get("kind") or "row")
        label = str(spec.get("label") or enum or f"Строка {len(rows) + 1}")
        if enum is None:
            rows.append(FormRow(
                row_id=f"{layout_id}:{len(rows):03d}", order=len(rows), kind=kind,
                enum=None, label=label, status="not_applicable",
                missing_reason="layout_static_row",
            ))
            continue

        candidates = by_concept.get(enum, [])
        if not requested_periods:
            _append_metric_row(enum=enum, label=label, kind=kind, candidates=candidates)
            continue

        wanted_kind = period_kind_for_enum(enum)
        periods = [period for period in requested_periods if _label_kind(period) == wanted_kind]
        if not periods:
            periods = list(dict.fromkeys(metric.period_ref for metric in candidates))
        if not periods:
            periods = list(requested_periods)
        for period in periods:
            normalized = normalize_period_label(period, value_kind=wanted_kind)
            period_candidates = [
                metric for metric in candidates
                if normalize_period_label(metric.period_ref, value_kind=wanted_kind) == normalized
            ]
            _append_metric_row(
                enum=enum, label=f"{label} — {period}", kind=kind,
                candidates=period_candidates,
            )

    return FormOutput(
        run_id=run_id,
        status="partial" if gaps else "complete",
        role=role,
        layout_id=layout_id,
        layout_version=layout_version,
        preserve_empty=preserve_empty,
        rows=rows,
        gaps=gaps,
        run_metadata=OutputRunMetadata.from_passport(run_id, passport),
    )


def out_form_executor(args, ctx) -> list[FormOutput]:
    passport = ctx.run_passport
    if passport is None:
        raise ValueError("out.form: RunPassport is required")
    role = args.extra.get("role") or ctx.inputs.role or "other"
    metrics = [
        item for item in ctx.dependencies()
        if isinstance(item, (ExtractedMetric, CalculatedMetric, UserProvidedMetric))
    ]
    requested = tuple(
        value.strip() for value in (args.extra.get("requested_concepts") or "").split(",")
        if value.strip()
    )
    requested_periods = tuple(
        value.strip() for value in (args.extra.get("requested_periods") or "").split("\x1f")
        if value.strip()
    )
    if not requested_periods and args.extra.get("period_scope") == "all_available":
        # Ingress v2 не угадывает период prepass-ом: ось строится из реально
        # извлечённых фактов. Так form следует той же политике «все доступные
        # даты», что точечное извлечение и расчёт, без маркера __all_periods__.
        requested_periods = tuple(dict.fromkeys(
            metric.period_ref for metric in metrics if metric.period_ref
        ))
    return [build_form_output(
        run_id=ctx.run_id,
        role=role,
        company_type=passport.classification.company_type,
        metrics=metrics,
        passport=passport,
        requested_concepts=requested,
        requested_periods=requested_periods,
    )]


__all__ = ["ATOM_VERSION", "build_form_output", "out_form_executor"]
