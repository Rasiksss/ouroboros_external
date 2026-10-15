"""Deterministic Slice-11 subject history calculations.

No function in this module calls a model, reads a document or interpolates a
missing period.  Model-backed semantic mapping may prepare typed inputs for
``decompose_change``/``select_peers`` but cannot alter their arithmetic or
policy filters.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from finengine.contracts import (
    ApproachPolicy,
    ApproachReport,
    ApproachSignal,
    CalculatedMetric,
    DecompositionComponent,
    DecompositionResult,
    DerivedFact,
    EntityIdentity,
    ExtractedMetric,
    FactDelta,
    MetricEvidence,
    PeerCandidate,
    PeerExclusion,
    PeerPolicy,
    PeerReferenceSet,
    RuleComparison,
    SeriesChange,
    SeriesFact,
    SeriesGap,
    SeriesPoint,
    SubjectDiff,
    SubjectKey,
    SubjectProfile,
    UserProvidedMetric,
)

ATOM_VERSION = "1.0.0"
SERIES_METHODOLOGY_VERSION = "comparable-period-series/1.0.0"
SUPPORTED_DECOMPOSITION_METHODS = frozenset({
    "additive_v1", "chain", "margin_chain", "laspeyres", "absolute",
})


class IncomparableSeriesError(ValueError):
    pass


class UnsupportedMethodologyError(ValueError):
    pass


def subject_key_from_identity(identity: EntityIdentity) -> SubjectKey:
    """Build a language/name-independent key from verified identifiers only."""
    identifiers = {
        str(kind).strip().casefold(): str(value).strip().upper()
        for kind, value in identity.external_ids.items()
        if str(kind).strip() and str(value).strip()
    }
    if identity.confidence_low or not identifiers:
        raise ValueError(
            "stable SubjectKey requires confirmed EntityIdentity with external identifiers; "
            "company name alone is not sufficient"
        )
    canonical = "|".join(f"{kind}:{value}" for kind, value in sorted(identifiers.items()))
    value = "subject:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return SubjectKey(value=value, entity_id=identity.entity_id, identifiers=identifiers)


# Сколько последних снимков субъекта смотрит выбор сравнимой пары. Повторных
# прогонов одного отчёта может быть много (на dev — 21 за ночь), поэтому окно
# заметно шире двух, но ограничено.
MONITOR_HISTORY_WINDOW = 50


def comparable_snapshot_pair(
    profiles: Sequence[SubjectProfile],
) -> tuple[SubjectProfile, SubjectProfile] | None:
    """`(previous, current)` для MONITOR_CHANGES из снимков в хронологическом порядке.

    Решение владельца (Срез 30, вариант A): снимки сравнимы, только если у них
    разные `document_version` или `period_refs`. `current` — последний снимок;
    `previous` — самый поздний более ранний снимок с ДРУГОЙ подписью
    (документ, период). Повторные прогоны того же документа и периода
    пропускаются — иначе «что изменилось» сравнило бы документ сам с собой.
    Нет такого снимка — `None` (вызывающий код возвращает typed-отказ)."""
    if len(profiles) < 2:
        return None

    def _signature(profile: SubjectProfile) -> tuple[tuple[str, ...], tuple[str, ...]]:
        return tuple(sorted(profile.document_versions)), tuple(sorted(profile.period_refs))

    current = profiles[-1]
    for candidate in reversed(profiles[:-1]):
        if _signature(candidate) != _signature(current):
            return candidate, current
    return None


def _metric_payload(metric: MetricEvidence):
    if isinstance(metric, ExtractedMetric):
        return metric.scalar_fact
    if isinstance(metric, CalculatedMetric):
        return metric.derived_fact
    if isinstance(metric, UserProvidedMetric):
        return metric.human_override
    raise TypeError(type(metric).__name__)


def _metric_tuple(metric: MetricEvidence) -> tuple[float, str, str, str | None, str, str | None, datetime]:
    payload = _metric_payload(metric)
    if isinstance(metric, UserProvidedMetric):
        if payload.numeric_value is None or not payload.unit:
            raise ValueError(f"override {payload.evidence_id} has no normalized numeric value/unit")
        value, unit = payload.numeric_value, payload.unit
    else:
        value, unit = payload.value, payload.unit
    if unit is None:
        raise ValueError(f"metric {payload.evidence_id} has no unit")
    return (
        float(value), str(unit), metric.period_ref, payload.basis,
        payload.evidence_id, payload.document_version, payload.created_at,
    )


_QUARTER = re.compile(r"^(?:FY)?(\d{4})[- ]?Q([1-4])$", re.I)
_QUARTER_ALT = re.compile(r"^Q([1-4])[- ]?(\d{4})$", re.I)
_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_YEAR = re.compile(r"^(?:FY)?(\d{4})$", re.I)


def _period_position(period_ref: str) -> tuple[str, int] | None:
    raw = period_ref.strip()
    m = _QUARTER.fullmatch(raw)
    if m:
        return "quarter", int(m.group(1)) * 4 + int(m.group(2)) - 1
    m = _QUARTER_ALT.fullmatch(raw)
    if m:
        return "quarter", int(m.group(2)) * 4 + int(m.group(1)) - 1
    m = _MONTH.fullmatch(raw)
    if m:
        return "month", int(m.group(1)) * 12 + int(m.group(2)) - 1
    m = _YEAR.fullmatch(raw)
    if m:
        return "year", int(m.group(1))
    return None


def _period_sort_key(period_ref: str) -> tuple[int, int | str]:
    pos = _period_position(period_ref)
    return (0, pos[1]) if pos else (1, period_ref.casefold())


def _missing_periods(left: str, right: str) -> list[str]:
    a, b = _period_position(left), _period_position(right)
    if not a or not b or a[0] != b[0] or b[1] - a[1] <= 1:
        return []
    kind = a[0]
    out: list[str] = []
    for position in range(a[1] + 1, b[1]):
        if kind == "quarter":
            year, q0 = divmod(position, 4)
            out.append(f"{year}-Q{q0 + 1}")
        elif kind == "month":
            year, m0 = divmod(position, 12)
            out.append(f"{year}-{m0 + 1:02d}")
        else:
            out.append(str(position))
    return out


def _months_between(left: str, right: str) -> int | None:
    a, b = _period_position(left), _period_position(right)
    if not a or not b or a[0] != b[0] or b[1] <= a[1]:
        return None
    multiplier = {"month": 1, "quarter": 3, "year": 12}[a[0]]
    return (b[1] - a[1]) * multiplier


def build_series(metrics: Sequence[MetricEvidence], *, concept: str | None = None) -> SeriesFact:
    selected = [m for m in metrics if concept is None or m.concept == concept]
    if not selected:
        raise ValueError("calc.series requires at least one MetricEvidence")
    concepts = {m.concept for m in selected}
    if len(concepts) != 1:
        raise ValueError("calc.series accepts one concept per SeriesFact")

    rows = [(m, _metric_tuple(m)) for m in selected]
    units = {row[1][1] for row in rows}
    bases = {row[1][3] for row in rows}
    if len(units) != 1 or len(bases) != 1:
        raise IncomparableSeriesError(
            f"series inputs are not comparable: units={sorted(units)!r}, bases={sorted(str(x) for x in bases)!r}"
        )

    # Explicit revision policy: for the same canonical period retain the most
    # recently created immutable evidence. Exact evidence retries deduplicate.
    by_period: dict[str, tuple[MetricEvidence, tuple]] = {}
    seen_evidence: set[str] = set()
    for metric, row in rows:
        if row[4] in seen_evidence:
            continue
        seen_evidence.add(row[4])
        old = by_period.get(row[2])
        if old is None or row[6] > old[1][6]:
            by_period[row[2]] = (metric, row)
    ordered = sorted(by_period.values(), key=lambda item: _period_sort_key(item[1][2]))
    points = [SeriesPoint(
        period_ref=row[2], value=row[0], unit=row[1], basis=row[3],
        evidence_id=row[4], document_version=row[5],
    ) for _metric, row in ordered]

    changes: list[SeriesChange] = []
    gaps: list[SeriesGap] = []
    for left, right in zip(points, points[1:]):
        absolute = right.value - left.value
        relative = absolute / abs(left.value) if left.value != 0 else None
        months = _months_between(left.period_ref, right.period_ref)
        annualized = None
        if months and left.value > 0 and right.value >= 0:
            annualized = (right.value / left.value) ** (12 / months) - 1
        changes.append(SeriesChange(
            from_period_ref=left.period_ref, to_period_ref=right.period_ref,
            absolute_change=absolute, relative_change=relative, rate=relative,
            annualized_rate=annualized,
            input_evidence_ids=[left.evidence_id, right.evidence_id],
        ))
        missing = _missing_periods(left.period_ref, right.period_ref)
        if missing:
            gaps.append(SeriesGap(
                after_period_ref=left.period_ref, before_period_ref=right.period_ref,
                missing_period_refs=missing,
            ))
    input_ids = [p.evidence_id for p in points]
    return SeriesFact(
        produced_by_atom="calc.series", produced_by_atom_version=ATOM_VERSION,
        source_ref="evidence:" + hashlib.sha256("|".join(input_ids).encode()).hexdigest()[:16],
        concept=next(iter(concepts)), basis=next(iter(bases)),
        points=points, changes=changes, gaps=gaps, input_evidence_ids=input_ids,
        methodology_version=SERIES_METHODOLOGY_VERSION, interpolation_applied=False,
    )


def decompose_change(
    *, concept: str, target_change: float, target_input_evidence_ids: Sequence[str],
    components: Sequence[Mapping[str, Any]], methodology_id: str,
    methodology_version: str, required_component_ids: Sequence[str] = (),
    tolerance: float = 1e-9,
) -> DecompositionResult:
    if methodology_id not in SUPPORTED_DECOMPOSITION_METHODS:
        raise UnsupportedMethodologyError(f"unsupported decomposition methodology: {methodology_id}")
    typed: list[DecompositionComponent] = []
    present: set[str] = set()
    for raw in components:
        component_id = str(raw["component_id"])
        value = float(raw["value"])
        if not math.isfinite(value):
            raise ValueError(f"component {component_id!r} is not finite")
        inputs = [str(x) for x in raw.get("input_evidence_ids", ())]
        if not inputs:
            raise ValueError(f"component {component_id!r} requires evidence inputs")
        present.add(component_id)
        fact = DerivedFact(
            produced_by_atom="calc.decompose", produced_by_atom_version=ATOM_VERSION,
            source_ref=f"methodology:{methodology_id}:{methodology_version}",
            concept=f"{concept}:{component_id}", formula_id=methodology_id,
            formula_version=methodology_version,
            expression=str(raw.get("expression") or f"component:{component_id}"),
            inputs=inputs, value=value, unit=raw.get("unit"),
        )
        typed.append(DecompositionComponent(
            component_id=component_id, label=str(raw.get("label") or component_id),
            value=value, derived_fact=fact,
        ))
    missing = sorted(set(required_component_ids) - present)
    residual = float(target_change) - sum(c.value for c in typed)
    complete = not missing and abs(residual) <= tolerance
    return DecompositionResult(
        concept=concept, methodology_id=methodology_id,
        methodology_version=methodology_version, target_change=float(target_change),
        target_input_evidence_ids=[str(x) for x in target_input_evidence_ids],
        components=typed, residual=residual, tolerance=tolerance,
        complete=complete, missing_components=missing,
    )


def adapt_factor_results(
    *, concept: str, target_change: float, target_input_evidence_ids: Sequence[str],
    factors: Sequence[Any], methodology_version: str = "factor-analysis/1.0.0",
    tolerance: float = 1e-6,
) -> DecompositionResult:
    methods = {str(f.method) for f in factors}
    if len(methods) != 1:
        raise UnsupportedMethodologyError(f"factor adapter requires one explicit methodology, got {sorted(methods)!r}")
    method = next(iter(methods), "")
    return decompose_change(
        concept=concept, target_change=target_change,
        target_input_evidence_ids=target_input_evidence_ids,
        components=[{
            "component_id": f.id, "label": f.name, "value": f.value,
            "expression": f.formula_repr,
            "input_evidence_ids": list(target_input_evidence_ids),
        } for f in factors],
        methodology_id=method, methodology_version=methodology_version,
        tolerance=tolerance,
    )


def select_peers(
    *, subject_key: SubjectKey, scenario_id: str, policy: PeerPolicy,
    candidates: Sequence[PeerCandidate],
) -> PeerReferenceSet:
    if not scenario_id.strip():
        raise ValueError("ref.peer is allowed only under an explicit scenario")
    included: list[PeerCandidate] = []
    excluded: list[PeerExclusion] = []
    seen: set[str] = set()
    for candidate in candidates:
        reasons: list[str] = []
        if candidate.stable_peer_id in seen:
            reasons.append("duplicate stable_peer_id")
        seen.add(candidate.stable_peer_id)
        for field in ("company_type", "business_model", "standard", "currency"):
            if getattr(candidate, field) != getattr(policy, field):
                reasons.append(f"{field} mismatch")
        values = {v.concept: v for v in candidate.values}
        for concept in policy.required_concepts:
            value = values.get(concept)
            if value is None:
                reasons.append(f"missing concept {concept}")
                continue
            if value.period_ref != policy.period_ref:
                reasons.append(f"period mismatch for {concept}")
            if value.basis != policy.basis:
                reasons.append(f"basis mismatch for {concept}")
            if not value.source_ref:
                reasons.append(f"missing source for {concept}")
        if reasons:
            excluded.append(PeerExclusion(stable_peer_id=candidate.stable_peer_id, reasons=reasons))
        else:
            included.append(candidate)
    return PeerReferenceSet(
        scenario_id=scenario_id, subject_key=subject_key, policy_id=policy.policy_id,
        policy_version=policy.version, peers=included, excluded=excluded,
        sufficient=len(included) >= policy.min_peers,
    )


def _metric_map(profile: SubjectProfile) -> dict[str, MetricEvidence]:
    out: dict[str, MetricEvidence] = {}
    for metric in profile.metrics:
        value, unit, period, basis, _eid, _doc, _created = _metric_tuple(metric)
        del value
        out[f"{metric.concept}|{period}|{unit}"] = metric
    return out


def _delta(identity: str, old: MetricEvidence | None, new: MetricEvidence | None, *, detail: str = "") -> FactDelta:
    old_row = _metric_tuple(old) if old else None
    new_row = _metric_tuple(new) if new else None
    absolute = relative = None
    if old_row and new_row and old_row[3] == new_row[3]:
        absolute = new_row[0] - old_row[0]
        relative = absolute / abs(old_row[0]) if old_row[0] != 0 else None
    return FactDelta(
        identity=identity,
        old_evidence_id=old_row[4] if old_row else None,
        new_evidence_id=new_row[4] if new_row else None,
        old_value=old_row[0] if old_row else None,
        new_value=new_row[0] if new_row else None,
        absolute_delta=absolute, relative_delta=relative, detail=detail,
    )


def diff_profiles(previous: SubjectProfile, current: SubjectProfile) -> SubjectDiff:
    if previous.subject_key.value != current.subject_key.value:
        raise ValueError("watch.diff cannot compare different SubjectKey values")
    issues: list[str] = []
    status = "comparable"
    if previous.registry_version != current.registry_version:
        issues.append("registry_version changed")
        status = "partial"
    if previous.methodology_version != current.methodology_version:
        issues.append("methodology_version changed")
        status = "partial"
    old, new = _metric_map(previous), _metric_map(current)
    added: list[FactDelta] = []
    changed: list[FactDelta] = []
    disappeared: list[FactDelta] = []
    for key in sorted(old.keys() | new.keys()):
        left, right = old.get(key), new.get(key)
        if left is None:
            added.append(_delta(key, None, right))
            continue
        if right is None:
            disappeared.append(_delta(key, left, None))
            continue
        lrow, rrow = _metric_tuple(left), _metric_tuple(right)
        if lrow[3] != rrow[3]:
            issues.append(f"basis mismatch for {key}: {lrow[3]!r} != {rrow[3]!r}")
            status = "incompatible"
            changed.append(_delta(key, left, right, detail="basis_changed; numeric delta suppressed"))
        elif lrow[0] != rrow[0] or lrow[5] != rrow[5]:
            changed.append(_delta(key, left, right, detail="value_or_document_version_changed"))

    def structure_map(profile: SubjectProfile) -> dict[str, Any]:
        return {
            f"{s.table_kind}|{s.structured_fact.concept or ''}|{s.structured_fact.period_ref or ''}": s
            for s in profile.structures
        }

    structure_changes: list[FactDelta] = []
    os, ns = structure_map(previous), structure_map(current)
    for key in sorted(os.keys() | ns.keys()):
        left, right = os.get(key), ns.get(key)
        left_json = json.dumps(left.model_dump(mode="json"), sort_keys=True) if left else None
        right_json = json.dumps(right.model_dump(mode="json"), sort_keys=True) if right else None
        if left_json != right_json:
            structure_changes.append(FactDelta(
                identity=key,
                old_evidence_id=left.structured_fact.evidence_id if left else None,
                new_evidence_id=right.structured_fact.evidence_id if right else None,
                detail="structure_changed",
            ))

    def threshold_map(profile: SubjectProfile) -> dict[str, RuleComparison]:
        return {f"{r.concept}|{r.operator}|{r.unit or ''}": r for r in profile.thresholds}

    threshold_changes: list[FactDelta] = []
    ot, nt = threshold_map(previous), threshold_map(current)
    for key in sorted(ot.keys() | nt.keys()):
        left, right = ot.get(key), nt.get(key)
        if left is None or right is None or (
            left.threshold_value != right.threshold_value
            or left.threshold_reference_id != right.threshold_reference_id
        ):
            threshold_changes.append(FactDelta(
                identity=key,
                old_value=left.threshold_value if left else None,
                new_value=right.threshold_value if right else None,
                detail="threshold_changed",
            ))
    return SubjectDiff(
        subject_key=current.subject_key,
        previous_snapshot_id=previous.snapshot_id, current_snapshot_id=current.snapshot_id,
        previous_plan_run_id=previous.plan_run_id, current_plan_run_id=current.plan_run_id,
        comparability=status, comparability_issues=issues,
        new_facts=added, changed_facts=changed, disappeared_facts=disappeared,
        changed_structures=structure_changes, changed_thresholds=threshold_changes,
    )


_RECOMMENDATION_WORDS = re.compile(
    r"\b(рекоменду(?:ем|ется|ю)|следует|необходимо|купить|продать|инвестировать|"
    r"recommend|should|buy|sell|invest)\b", re.I,
)


def contains_recommendation_language(text: str) -> bool:
    return bool(_RECOMMENDATION_WORDS.search(text))


def _distance(rule: RuleComparison, policy: ApproachPolicy) -> float | None:
    if rule.headroom is None:
        return None
    if policy.distance_kind == "absolute":
        return abs(rule.headroom)
    if rule.threshold_value == 0:
        return None
    return abs(rule.headroom) / abs(rule.threshold_value)


def evaluate_approach(
    current: RuleComparison, previous: RuleComparison | None, policy: ApproachPolicy,
) -> ApproachSignal:
    comparable = previous is None or (
        previous.concept == current.concept
        and previous.operator == current.operator
        and previous.unit == current.unit
        and previous.threshold_value == current.threshold_value
        and previous.threshold_reference_id == current.threshold_reference_id
    )
    distance = _distance(current, policy)
    previous_distance = _distance(previous, policy) if previous and comparable else None
    if not comparable or distance is None:
        return ApproachSignal(
            rule_id=current.rule_id, concept=current.concept, status="insufficient",
            distance=distance, previous_distance=previous_distance, direction="unknown",
            policy_id=policy.policy_id, policy_version=policy.version,
            narrative="Сопоставимость с предыдущим порогом не подтверждена; сигнал не рассчитан.",
        )
    if previous_distance is None:
        direction = "unknown"
    elif distance < previous_distance:
        direction = "toward"
    elif distance > previous_distance:
        direction = "away"
    else:
        direction = "unchanged"
    crossed = bool(previous is not None and previous.passed and not current.passed)
    approaching = bool(current.passed and distance <= policy.approach_boundary and direction in {"toward", "unknown"})
    if crossed:
        status = "crossed"
        narrative = "Показатель пересёк проверенный порог; зафиксирован факт пересечения."
    elif approaching:
        status = "approaching"
        narrative = "Показатель приблизился к проверенному порогу в пределах заданной политики наблюдения."
    elif not current.passed:
        status = "outside"
        narrative = "Показатель находится за проверенным порогом; это нейтральная фиксация состояния."
    else:
        status = "not_approaching"
        narrative = "Признак приближения к проверенному порогу по заданной политике отсутствует."
    assert not contains_recommendation_language(narrative)
    return ApproachSignal(
        rule_id=current.rule_id, concept=current.concept, status=status,
        distance=distance, previous_distance=previous_distance, direction=direction,
        crossed=crossed, approaching=approaching,
        policy_id=policy.policy_id, policy_version=policy.version, narrative=narrative,
    )


def approach_profiles(
    previous: SubjectProfile, current: SubjectProfile, policy: ApproachPolicy,
) -> ApproachReport:
    prior = {f"{r.concept}|{r.operator}|{r.unit or ''}": r for r in previous.thresholds}
    signals = [
        evaluate_approach(rule, prior.get(f"{rule.concept}|{rule.operator}|{rule.unit or ''}"), policy)
        for rule in current.thresholds
    ]
    return ApproachReport(policy_id=policy.policy_id, policy_version=policy.version, signals=signals)


def series_from_profiles(profiles: Iterable[SubjectProfile]) -> list[SeriesFact]:
    grouped: dict[str, list[MetricEvidence]] = defaultdict(list)
    for profile in profiles:
        for metric in profile.metrics:
            grouped[metric.concept].append(metric)
    out: list[SeriesFact] = []
    for concept, metrics in sorted(grouped.items()):
        try:
            out.append(build_series(metrics, concept=concept))
        except IncomparableSeriesError:
            # A monitor result remains usable, but incompatible series is not
            # silently blended. watch.diff carries the explicit basis issue.
            continue
    return out


__all__ = [
    "ATOM_VERSION", "SERIES_METHODOLOGY_VERSION", "IncomparableSeriesError",
    "UnsupportedMethodologyError", "subject_key_from_identity", "build_series",
    "decompose_change", "adapt_factor_results", "select_peers", "diff_profiles",
    "contains_recommendation_language", "evaluate_approach", "approach_profiles",
    "series_from_profiles",
]
