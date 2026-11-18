"""`verify.bounds` — карточка 16a: отсеять математически невозможные значения
и отдельно отметить подозрительные — из версionированного определения
показателя/политики, не из скрытого списка в коде.

**Источники границ (оба уже существуют, ни один не изобретён здесь).**
Жёсткая пара — `CalcDef.min_value/max_value` (`metric_schema.py`, Р7,
24.08.2026 — введена владельцем для само-проверки авторских формул, здесь
впервые получает потребителя). Мягкая пара — `CalcDef.soft_min_value/
soft_max_value`, диапазоны 9 банковских коэффициентов в данных конкретных
ячеек (Срез 6a, миграция реестра).
Обе живут в `registry["calculated_meta"][concept]` независимо от того,
извлечён факт или рассчитан — коэффициент остаётся тем же самым
коэффициентом вне зависимости от происхождения числа, и `verify.bounds`
проверяет ОБЕ формы одним и тем же правилом (карточка: «Запускать после
нормализации и после расчётов»).

**`math.isfinite` сюда не входит.** `ScalarFact`/`DerivedFact` уже
валидируют конечность значения на конструкторе (`contracts/evidence.py`,
`_finite` field_validator) — факт с NaN/inf не может существовать как
объект, значит повторная проверка здесь была бы всегда истинной проверкой
несуществующего случая. Настоящая защита от деления на ноль — в самом
`calc.formula` (перехват `ZeroDivisionError` ДО попытки построить
`DerivedFact`, см. `calc_formula.py`), не здесь.

**Отраслевые диапазоны не добавляются без источника.** Единственные
жёсткие/мягкие границы, которые эта сессия признаёт, — уже существующие два
источника выше. Новый показатель без объявленной границы получает
`status="pass"` без проверки, а не выдуманный диапазон (карточка: «Отсутствие
границы не приводит к выдуманному ограничению»)."""
from __future__ import annotations

from finengine.contracts import CalculatedMetric, ExtractedMetric, RunPassport, VerificationFinding
from finengine.skills.metric_schema import registry_for_passport
from finengine.skills.spec.atom_registry import NodeArgs

ATOM_VERSION = "1.0.0"
CHECK_KIND_HARD = "bounds_hard"
CHECK_KIND_SOFT = "bounds_soft"


def check_bounds(
    *, concept: str, evidence_id: str, period_ref: str | None, value: float,
    hard_min: float | None, hard_max: float | None, soft_min: float | None, soft_max: float | None,
) -> VerificationFinding:
    """Чистая функция — ядро атома. Жёсткая граница нарушена → `status="fail"`
    (`check_kind=bounds_hard`, факт НЕ проходит гейт). Мягкая нарушена, а
    жёсткая — нет → `status="warn"` (`check_kind=bounds_soft`, факт проходит
    как есть — предупреждение не переписывает и не удаляет число)."""
    if hard_min is not None and value < hard_min:
        return VerificationFinding(
            produced_by_atom="verify.bounds", produced_by_atom_version=ATOM_VERSION,
            source_ref=evidence_id, concept=concept, period_ref=period_ref,
            check_kind=CHECK_KIND_HARD, status="fail",
            detail=f"{concept}={value!r} ниже жёсткой границы {hard_min!r}",
        )
    if hard_max is not None and value > hard_max:
        return VerificationFinding(
            produced_by_atom="verify.bounds", produced_by_atom_version=ATOM_VERSION,
            source_ref=evidence_id, concept=concept, period_ref=period_ref,
            check_kind=CHECK_KIND_HARD, status="fail",
            detail=f"{concept}={value!r} выше жёсткой границы {hard_max!r}",
        )
    if (soft_min is not None and value < soft_min) or (soft_max is not None and value > soft_max):
        return VerificationFinding(
            produced_by_atom="verify.bounds", produced_by_atom_version=ATOM_VERSION,
            source_ref=evidence_id, concept=concept, period_ref=period_ref,
            check_kind=CHECK_KIND_SOFT, status="warn",
            detail=f"{concept}={value!r} вне мягкого диапазона [{soft_min!r}, {soft_max!r}]",
        )
    has_any_bound = any(b is not None for b in (hard_min, hard_max, soft_min, soft_max))
    return VerificationFinding(
        produced_by_atom="verify.bounds", produced_by_atom_version=ATOM_VERSION,
        source_ref=evidence_id, concept=concept, period_ref=period_ref,
        check_kind=CHECK_KIND_HARD, status="pass",
        detail=(
            f"{concept}={value!r} в пределах объявленных границ" if has_any_bound
            else f"{concept}: граница не объявлена, проверка не применяется"
        ),
    )


def _bounds_for(concept: str, registry: dict) -> dict:
    return (registry.get("calculated_meta") or {}).get((concept or "").strip().upper()) or {}


# ---------------------------------------------------------------------------
# Graph wiring — общий гейт для read.value (ExtractedMetric) и calc.formula
# (CalculatedMetric); заменяет `verify.arithmetic` в цепочке `calc.formula`
# (см. `graph_atoms.build_registry`).
def verify_bounds_gate(args: NodeArgs, ctx) -> list:
    passport: RunPassport | None = ctx.run_passport
    registry = registry_for_passport(passport) if passport is not None else {}
    bounds = _bounds_for(args.concept or "", registry)

    # Срез 31: значений может быть несколько (все доступные периоды) — границы
    # проверяются у КАЖДОГО; выпавшее за жёсткую границу отбрасывается, остальные
    # остаются.
    def _checked(evidences, fact_of) -> list:
        out: list = []
        for evidence in evidences:
            fact = fact_of(evidence)
            finding = check_bounds(
                concept=args.concept or "", evidence_id=fact.evidence_id, period_ref=fact.period_ref,
                value=fact.value, hard_min=bounds.get("min_value"), hard_max=bounds.get("max_value"),
                soft_min=bounds.get("soft_min_value"), soft_max=bounds.get("soft_max_value"),
            )
            out.extend([finding] if finding.status == "fail" else [finding, evidence])
        return out

    calcs = [e for e in ctx.dependencies() if isinstance(e, CalculatedMetric) and e.concept == args.concept]
    if calcs:
        return _checked(calcs, lambda e: e.derived_fact)

    extracted = [e for e in ctx.dependencies() if isinstance(e, ExtractedMetric) and e.concept == args.concept]
    if extracted:
        return _checked(extracted, lambda e: e.scalar_fact)

    return []


__all__ = ["check_bounds", "verify_bounds_gate", "ATOM_VERSION", "CHECK_KIND_HARD", "CHECK_KIND_SOFT"]
