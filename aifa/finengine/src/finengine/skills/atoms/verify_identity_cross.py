"""`verify.identity` / `verify.cross` — карточки 16a. Один общий исполнитель
(врезка «сверка механики», 18.09, `16a-...md`): оба сравнивают два
независимо полученных значения одного факта и создают `VerificationFinding`,
не уничтожая ни одну из сторон. Разница только в источнике пары:

- `identity` — пара из ЗАРАНЕЕ зарегистрированного правила (`registry
  ["verification_rules"]`, ключ — концепт этой ячейки); мандаторный вентиль
  перед `calc.formula` для концептов, у которых правило есть.
- `cross` — пара из того, что ОППОРТУНИСТИЧЕСКИ доступно в этом прогоне
  (концепт одновременно извлекаемый и расчётный — тот же `dual_nature`
  признак, которым уже пользуется `default_policy()` в `graph_atoms.py`);
  условный — не блокирует концепты без второй стороны.

Оба вентиля объявляют СОБСТВЕННЫЙ `dynamic_requires` — с расширением
`GraphPlanner._apply_mandatory_gates` (Срез 6a, `graph_planner.py`) это
единственный способ дотянуться до ВТОРОЙ, независимо полученной стороны:
`requires=[current]`, который гейт получал бы без этого расширения, несёт
только вывод СВОЕГО непосредственного предшественника в цепочке.

Оба `dynamic_requires` СНАЧАЛА проверяют, что концепт реально в списке
`extractable` этой ячейки, ПРЕЖДЕ чем просить `ExtractedMetric` — иначе
`_resolve()` для расчётного-только концепта (нет producer'а вовсе) добавил
бы `Unreachable` и уронил бы `feasibility.feasible` всего графа ради
необязательной сверки. Это и есть техническая реализация «условности»
`verify.cross`: не пытаться получить то, что заведомо недостижимо."""
from __future__ import annotations

from finengine.contracts import (
    CalculatedMetric,
    ExtractedMetric,
    RunPassport,
    VerificationFinding,
)
from finengine.skills.metric_schema import registry_for_passport
from finengine.skills.periods import normalize_period_label
from finengine.skills.spec.atom_registry import NodeArgs
from finengine.skills.spec.target import InputSpec

ATOM_VERSION = "1.0.0"


def _fact_of(evidence: ExtractedMetric | CalculatedMetric):
    return evidence.scalar_fact if isinstance(evidence, ExtractedMetric) else evidence.derived_fact


def _verify_pair(
    *, concept: str, evidence_a: ExtractedMetric | CalculatedMetric, evidence_b: ExtractedMetric | CalculatedMetric,
    tolerance: float, check_kind: str, atom_name: str,
) -> VerificationFinding:
    """Ядро, общее для обоих атомов (см. докстринг модуля). `basis` сверяется
    только по уже существующему `EvidenceRecord.basis` (голая строка) — сборка
    полноценного многоосевого `Basis` (`norm.basis.compare_basis`) требует
    метаданных, которые сегодня ни один producer не прикрепляет к факту;
    `ref.prior`/`ref.peer` (Срез 6b/11) — настоящие потребители этого пути."""
    fact_a, fact_b = _fact_of(evidence_a), _fact_of(evidence_b)

    if fact_a.basis and fact_b.basis and fact_a.basis != fact_b.basis:
        return VerificationFinding(
            produced_by_atom=atom_name, produced_by_atom_version=ATOM_VERSION,
            source_ref=f"{fact_a.evidence_id},{fact_b.evidence_id}", concept=concept,
            period_ref=fact_a.period_ref, check_kind=check_kind, status="not_comparable",
            detail=f"базис не совпадает: {fact_a.basis!r} против {fact_b.basis!r}",
        )

    delta = fact_a.value - fact_b.value
    denom = max(abs(fact_a.value), abs(fact_b.value)) or 1.0
    rel_delta = abs(delta) / denom
    status = "pass" if rel_delta <= tolerance else "fail"
    return VerificationFinding(
        produced_by_atom=atom_name, produced_by_atom_version=ATOM_VERSION,
        source_ref=f"{fact_a.evidence_id},{fact_b.evidence_id}", concept=concept, period_ref=fact_a.period_ref,
        check_kind=check_kind, status=status,
        detail=(
            f"{concept}: {fact_a.value!r} (evidence {fact_a.evidence_id}) против "
            f"{fact_b.value!r} (evidence {fact_b.evidence_id}); относительное расхождение "
            f"{rel_delta:.6f}, допуск {tolerance!r}"
        ),
    )


def _is_extractable(concept: str, registry: dict) -> bool:
    return concept in {str(m.get("enum", "")).strip().upper() for m in registry.get("extractable", [])}


# ---------------------------------------------------------------------------
# verify.identity — мандаторный вентиль перед calc.formula для концептов,
# зарегистрированных в registry["verification_rules"].
def verify_identity_requires(node_args: NodeArgs, run_passport: RunPassport | None) -> tuple[InputSpec, ...]:
    if run_passport is None or not node_args.concept:
        return ()
    registry = registry_for_passport(run_passport)
    rule = (registry.get("verification_rules") or {}).get(node_args.concept)
    if not rule or not _is_extractable(node_args.concept, registry):
        return ()
    return (InputSpec(input_type="ExtractedMetric", concept=node_args.concept),)


def _pair_for(calc: CalculatedMetric, extracted: list[ExtractedMetric], *, only_one_calc: bool):
    """Вторая сторона сравнения для ОДНОГО рассчитанного значения.

    Срез 31: показатель приходит за несколько периодов (все доступные даты), и
    рассчитанное значение сверяется с извлечённым ТОГО ЖЕ периода (нормализованная
    подпись). Если совпадений нет, но и рассчитанное значение единственное, а
    извлечённое — тоже единственное, пара остаётся прежней (поведение до среза):
    подписи одного периода у извлечения и расчёта бывают разной формы
    («31.12.2024» и «FY 2024»)."""
    target = normalize_period_label(calc.derived_fact.period_ref or "")
    for candidate in extracted:
        if normalize_period_label(candidate.period_ref) == target:
            return candidate
    if only_one_calc and len(extracted) == 1:
        return extracted[0]
    return None


def verify_identity_gate(args: NodeArgs, ctx) -> list:
    deps = ctx.dependencies()
    calcs = [e for e in deps if isinstance(e, CalculatedMetric) and e.concept == args.concept]
    if not calcs:
        return []
    extracted = [e for e in deps if isinstance(e, ExtractedMetric) and e.concept == args.concept]
    passport = ctx.run_passport
    registry = registry_for_passport(passport) if passport is not None else {}
    rule = (registry.get("verification_rules") or {}).get(args.concept or "") or {}
    tolerance = float(rule.get("tolerance", 0.01))
    out: list = []
    for calc in calcs:
        other = _pair_for(calc, extracted, only_one_calc=len(calcs) == 1)
        if other is None:
            finding = VerificationFinding(
                produced_by_atom="verify.identity", produced_by_atom_version=ATOM_VERSION,
                source_ref=calc.derived_fact.evidence_id, concept=args.concept,
                period_ref=calc.derived_fact.period_ref or args.period_ref,
                check_kind="identity", status="not_verified",
                detail="для этого концепта нет зарегистрированного правила тождества или второй стороны",
            )
        else:
            finding = _verify_pair(
                concept=args.concept or "", evidence_a=other, evidence_b=calc, tolerance=tolerance,
                check_kind="identity", atom_name="verify.identity",
            )
        out.extend([finding, calc])
    return out


# ---------------------------------------------------------------------------
# verify.cross — условный вентиль для любого concept'а «двойной природы»
# (извлекаемый И расчётный одновременно), без реестровой записи правила.
def verify_cross_requires(node_args: NodeArgs, run_passport: RunPassport | None) -> tuple[InputSpec, ...]:
    if run_passport is None or not node_args.concept:
        return ()
    registry = registry_for_passport(run_passport)
    if not _is_extractable(node_args.concept, registry):
        return ()  # расчётный-только концепт — оппортунистической стороны нет, и это законно
    return (InputSpec(input_type="ExtractedMetric", concept=node_args.concept),)


# Допуск на округление форматирования отчёта (не бизнес-порог) — расхождение
# в пределах решённого округления таблицы не маркируется конфликтом (карточка
# `verify.cross`: «Округление в пределах заранее заданного допуска не
# маркируется как конфликт»). Отдельного источника/политики для этого числа в
# репозитории сегодня нет — 1% выбран как самая широкая величина, которая всё
# ещё ловит перепутанный показатель, а не как проверенный норматив; при
# появлении реальной политики значение переносится туда, не остаётся здесь.
DEFAULT_CROSS_TOLERANCE = 0.01


def verify_cross_gate(args: NodeArgs, ctx) -> list:
    deps = ctx.dependencies()
    calcs = [e for e in deps if isinstance(e, CalculatedMetric) and e.concept == args.concept]
    if not calcs:
        return []
    extracted = [e for e in deps if isinstance(e, ExtractedMetric) and e.concept == args.concept]
    out: list = []
    for calc in calcs:
        other = _pair_for(calc, extracted, only_one_calc=len(calcs) == 1)
        if other is None:
            finding = VerificationFinding(
                produced_by_atom="verify.cross", produced_by_atom_version=ATOM_VERSION,
                source_ref=calc.derived_fact.evidence_id, concept=args.concept,
                period_ref=calc.derived_fact.period_ref or args.period_ref,
                check_kind="cross", status="not_verified",
                detail="нет второго независимого пути для этого концепта в текущем прогоне",
            )
        else:
            finding = _verify_pair(
                concept=args.concept or "", evidence_a=other, evidence_b=calc,
                tolerance=DEFAULT_CROSS_TOLERANCE, check_kind="cross", atom_name="verify.cross",
            )
        out.extend([finding, calc])
    return out


__all__ = [
    "ATOM_VERSION",
    "DEFAULT_CROSS_TOLERANCE",
    "verify_identity_requires",
    "verify_identity_gate",
    "verify_cross_requires",
    "verify_cross_gate",
]
