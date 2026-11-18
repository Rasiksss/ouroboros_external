"""`assess.rule` — карточка 19 (Срез 9, B10): детерминированно сравнить факт
с нормой/планом и вернуть формальное математическое отношение (`RuleComparison`)
— без финансового вердикта (нерушимое решение №5: смысл формулирует
`assess.judgment` по методике, не этот атом).

**Вход.** Тестируемый показатель — обычный `MetricEvidence` (`ExtractedMetric`/
`CalculatedMetric`/`UserProvidedMetric`; активная пользовательская поправка
имеет тот же semantic slot, но документный факт остаётся отдельным evidence. Порог/план — одна или несколько
`ReferenceValue(source_kind in {"plan","norm"})` от `ref.plan`/`ref.norm`
(МНОЖЕСТВЕННОСТЬ — тест «multiple covenants for the same metric»: два
разных условия на один показатель дают ДВЕ `ReferenceValue`, значит ДВА
`RuleComparison`, не один усреднённый).

**norm.basis предшествует числовому сравнению** (брифинг сессии): базис
факта (`EvidenceRecord.basis`, плоская строка) и базис ссылки (`ReferenceValue.
basis`, тот же принцип «определение показателя» — карточка `ref.norm`:
«единица/период/определение должны быть сопоставимы с фактом») сравниваются
через `norm_basis.compare_basis` (тот же атом, что `verify.cross`/
`ref.prior` используют для базиса, здесь — по одной оси `standard`, потому
что ни ScalarFact/DerivedFact, ни ConditionEvidence сегодня не несут
полноценный многоosevой `Basis` — тот же честный предел, что `ref_prior.py`
уже документирует). Несовпадение → `VerificationFinding(status=
"not_comparable")`, СРАВНЕНИЕ БЛОКИРУЕТСЯ («разное определение чистого
долга» — тест «different debt definition»). Несовпадение единицы измерения
блокирует так же (тест «currency/unit mismatch»). Неполная ссылка (оператор
или значение не разрешены) блокирует так же (тест «incomplete condition»/
«missing plan/norm no fallback» — просто нет `ReferenceValue` вовсе).

**Математика.** `delta = actual - threshold` (знак не зависит от оператора).
`headroom` учитывает направление оператора: `threshold - actual` для `<`/`<=`
(положительный — запас, отрицательный — превышение), `actual - threshold`
для `>`/`>=`, `-|actual - threshold|` для `==`/`!=` (ноль — точное совпадение,
отрицательное — величина расхождения). `relative_delta = delta / |threshold|`
(`None`, если порог — ноль). `passed` — механический результат оператора
(карточка: «formal operator status can be represented mechanically»), не
качественный ярлык."""
from __future__ import annotations

from finengine.contracts import (
    Basis,
    CalculatedMetric,
    ExtractedMetric,
    ReferenceValue,
    RunPassport,
    RuleComparison,
    UserProvidedMetric,
    VerificationFinding,
)
from finengine.skills.atoms.norm_basis import compare_basis
from finengine.skills.spec.atom_registry import NodeArgs
from finengine.skills.spec.target import InputSpec

ATOM_VERSION = "1.0.0"

_MetricLike = ExtractedMetric | CalculatedMetric


def _fact_of(evidence: _MetricLike):
    return evidence.scalar_fact if isinstance(evidence, ExtractedMetric) else evidence.derived_fact


def assess_rule_requires(node_args: NodeArgs, run_passport: RunPassport | None) -> tuple[InputSpec, ...]:
    if run_passport is None or not node_args.concept:
        return ()
    return (
        InputSpec(input_type="MetricEvidence", concept=node_args.concept),
        InputSpec(input_type="ReferenceValue:plan", concept=node_args.concept, cardinality="MANY"),
        InputSpec(input_type="ReferenceValue:norm", concept=node_args.concept, cardinality="MANY"),
    )


def _evaluate(operator: str, actual: float, threshold: float) -> bool:
    if operator == "<":
        return actual < threshold
    if operator == "<=":
        return actual <= threshold
    if operator == ">":
        return actual > threshold
    if operator == ">=":
        return actual >= threshold
    if operator == "==":
        return actual == threshold
    if operator == "!=":
        return actual != threshold
    raise ValueError(f"assess.rule: неизвестный оператор {operator!r}")


def _headroom(operator: str, actual: float, threshold: float) -> float | None:
    if operator in ("<", "<="):
        return threshold - actual
    if operator in (">", ">="):
        return actual - threshold
    if operator in ("==", "!="):
        return -abs(actual - threshold)
    return None


def assess_rule_executor(args: NodeArgs, ctx) -> list:
    if ctx.run_passport is None:
        raise ValueError("assess.rule: RunPassport не зафиксирован — узел не должен был стать ready")
    concept = args.concept or ""
    deps = ctx.dependencies()
    metric = next(
        (e for e in deps if isinstance(e, (ExtractedMetric, CalculatedMetric, UserProvidedMetric)) and e.concept == concept), None,
    )
    if metric is None:
        return []
    fact = _fact_of(metric)
    references = [
        e for e in deps
        if isinstance(e, ReferenceValue) and e.concept == concept and e.source_kind in ("plan", "norm")
    ]
    if not references:
        # Ни плана, ни нормы для этого показателя — нет базы сравнения,
        # сравнение не выдумывается (тест «missing plan/norm no fallback»).
        return []

    out: list = []
    for reference in references:
        common = dict(
            produced_by_atom="assess.rule", produced_by_atom_version=ATOM_VERSION,
            source_ref=f"{fact.evidence_id},{reference.reference_id}", concept=concept,
            period_ref=fact.period_ref,
        )
        if reference.operator is None or reference.value is None:
            out.append(VerificationFinding(
                **common, check_kind="rule", status="not_comparable",
                detail=f"{reference.source_kind} {reference.reference_id!r} неполон(а) — оператор/значение не разрешены",
            ))
            continue

        basis_a = Basis(standard=fact.basis) if fact.basis else Basis()
        basis_b = Basis(standard=reference.basis) if reference.basis else Basis()
        basis_cmp = compare_basis(basis_a, basis_b)
        if basis_cmp.status == "not_comparable":
            out.append(VerificationFinding(
                **common, check_kind="basis", status="not_comparable", detail=basis_cmp.reason,
            ))
            continue

        fact_unit = getattr(fact, "unit", None)
        if fact_unit and reference.unit and fact_unit != reference.unit:
            out.append(VerificationFinding(
                **common, check_kind="unit", status="not_comparable",
                detail=f"единица не совпадает: {fact_unit!r} против {reference.unit!r}",
            ))
            continue

        operator = reference.operator
        threshold_value = reference.value
        delta = fact.value - threshold_value
        relative_delta = delta / abs(threshold_value) if threshold_value != 0 else None
        headroom = _headroom(operator, fact.value, threshold_value)
        passed = _evaluate(operator, fact.value, threshold_value)
        out.append(RuleComparison(
            concept=concept, operator=operator, actual_value=fact.value, actual_evidence_id=fact.evidence_id,
            threshold_value=threshold_value, threshold_reference_id=reference.reference_id,
            unit=fact_unit or reference.unit, delta=delta, relative_delta=relative_delta, headroom=headroom,
            passed=passed,
            detail=(
                f"{concept}: факт {fact.value!r} {operator} порог {threshold_value!r} "
                f"({reference.source_kind}, reference {reference.reference_id})"
            ),
        ))
    return out


__all__ = ["ATOM_VERSION", "assess_rule_requires", "assess_rule_executor"]
