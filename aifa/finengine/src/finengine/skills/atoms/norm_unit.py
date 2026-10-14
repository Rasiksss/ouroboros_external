"""`norm.unit` — карточка 16a: канонизировать единицу/валюту/масштаб/знак с
явной трассировкой преобразования.

Реальная свёртка денежного масштаба ("тыс."/"млрд." → "млн.") уже выполняется
ДО этого узла, внутри `read.value` (`bulk_extractor.period_values.
_coerce_period_values` → `value_parsing._normalise_money_value` — см.
докстринг `tests/atoms/test_read_value.py`, пункт 1). `norm.unit` не
переизобретает эту свёртку (решение репозитория — не дублировать разбор).

Его задача — то, чего сегодняшний путь не делает вовсе:
1. Явным узлом графа зафиксировать, что преобразование произошло (или не
   требовалось) — провенанс вместо молчаливого доверия уже готовому числу.
2. Свериться с ОЖИДАЕМЫМ видом единицы из `MetricDefinition.unit_kind`
   (сегодня реестр этой сверки не делает: `read.value` не знает, что
   `COST_TO_INCOME_RATIO` обязан быть долей/процентом, а не деньгами).
3. Там, где текст единицы не распознан ни как деньги, ни как процент/б.п., и
   реестр не объявляет ожидание, — явно заблокировать (`needs_unit_
   resolution`), а не пропустить как есть. `_normalise_money_value` на
   нераспознанном тексте молча возвращает значение без изменений
   («leave as-is, propagate unit») — именно эта тишина здесь становится
   явным отказом нормализации.
"""
from __future__ import annotations

from finengine.contracts import ExtractedMetric, MetricDefinition, RunPassport, ScalarFact, VerificationFinding
from finengine.skills.atoms.definitions import resolve_metric_definition
from finengine.skills.bulk_extractor import value_parsing
from finengine.skills.metric_schema import registry_for_passport
from finengine.skills.spec.atom_registry import NodeArgs

ATOM_VERSION = "1.0.0"
CHECK_KIND = "unit_normalization"

# `б.п.`/`bp` — распознавание новое: в `value_parsing.py` его нет вовсе
# (карточка 16a явно требует базисные пункты как отдельный тестовый сценарий).
_BP_KEYWORDS = ("б.п.", "бп", "basis point", "bps", " bp", "bp ")


def _looks_like_bp(unit_text: str) -> bool:
    u = unit_text.lower()
    return any(kw in u for kw in _BP_KEYWORDS) or u.strip() == "bp"


def _looks_like_money(unit_text: str) -> bool:
    u = unit_text.lower()
    if value_parsing._currency_from_unit_text(unit_text):
        return True
    return any(kw in u for kw in value_parsing._MONEY_SCALE_FACTORS)


def normalize_unit(fact: ScalarFact, metric_def: MetricDefinition) -> tuple[ScalarFact, VerificationFinding]:
    """Чистая функция — ядро атома, тестируемая независимо от graph plumbing.

    Возвращает `(итоговый ScalarFact, VerificationFinding)`. Итоговый факт —
    ТОТ ЖЕ объект (`is` identity), когда преобразование не требовалось (сохраняет
    идемпотентность: повторный вызов на уже каноничном факте не создаёт нового
    evidence_id), либо новый `ScalarFact` c `supersedes=fact.evidence_id`,
    когда масштаб/единица реально изменились.
    """
    unit_text = fact.unit
    is_percent = value_parsing._is_percentage_unit_text(unit_text)
    is_bp = _looks_like_bp(unit_text)
    is_money = _looks_like_money(unit_text)
    declared = (metric_def.unit_kind or "").strip().lower()

    conflict: str | None = None
    if declared == "money" and not is_money:
        conflict = f"реестр ожидает денежную единицу для {fact.concept!r}, факт несёт {unit_text!r}"
    elif declared == "percent" and not (is_percent or is_bp):
        conflict = f"реестр ожидает процент/б.п. для {fact.concept!r}, факт несёт {unit_text!r}"
    elif declared in ("ratio", "count", "days") and is_money:
        conflict = f"реестр ожидает {declared} для {fact.concept!r}, факт несёт денежную единицу {unit_text!r}"
    elif not declared and not (is_money or is_percent or is_bp):
        conflict = (
            f"единица {unit_text!r} не распознана ни как деньги, ни как процент/б.п., "
            f"а реестр не объявляет ожидание для {fact.concept!r}"
        )

    if conflict is not None:
        finding = VerificationFinding(
            produced_by_atom="norm.unit", produced_by_atom_version=ATOM_VERSION,
            source_ref=fact.evidence_id, concept=fact.concept, period_ref=fact.period_ref, basis=fact.basis,
            check_kind=CHECK_KIND, status="needs_unit_resolution", detail=conflict,
        )
        return fact, finding

    if is_money:
        new_value, new_unit = value_parsing._normalise_money_value(fact.value, unit_text)
        if new_value != fact.value or new_unit != unit_text:
            factor = next(
                (f for kw, f in value_parsing._MONEY_SCALE_FACTORS.items() if kw in unit_text.lower()), 1.0,
            )
            normalized = ScalarFact(
                produced_by_atom="norm.unit", produced_by_atom_version=ATOM_VERSION,
                source_ref=fact.source_ref, document_version=fact.document_version,
                concept=fact.concept, period_ref=fact.period_ref, basis=fact.basis,
                supersedes=fact.evidence_id, value=new_value, unit=new_unit, page=fact.page,
            )
            finding = VerificationFinding(
                produced_by_atom="norm.unit", produced_by_atom_version=ATOM_VERSION,
                source_ref=fact.evidence_id, concept=fact.concept, period_ref=fact.period_ref, basis=fact.basis,
                check_kind=CHECK_KIND, status="pass",
                detail=(
                    f"исходная единица {unit_text!r} ({fact.value!r}) -> целевая {new_unit!r} "
                    f"({new_value!r}), коэффициент к млн.={factor!r}"
                ),
            )
            return normalized, finding

    finding = VerificationFinding(
        produced_by_atom="norm.unit", produced_by_atom_version=ATOM_VERSION,
        source_ref=fact.evidence_id, concept=fact.concept, period_ref=fact.period_ref, basis=fact.basis,
        check_kind=CHECK_KIND, status="pass", detail="единица уже канонична, преобразование не требуется",
    )
    return fact, finding


# ---------------------------------------------------------------------------
# Graph wiring — mandatory_gate атома `read.value` (см. `graph_atoms.py`).
def norm_unit_gate(args: NodeArgs, ctx) -> list:
    metrics = [e for e in ctx.dependencies() if isinstance(e, ExtractedMetric) and e.concept == args.concept]
    if not metrics:
        # Показателя не нашлось выше по цепочке (read.value не дал факта) —
        # штатный исход, не ошибка гейта: нечего нормализовать.
        return []
    passport: RunPassport | None = ctx.run_passport
    registry = registry_for_passport(passport) if passport is not None else {}
    metric_def = resolve_metric_definition(args.concept or "", registry, passport)
    # Срез 31: показатель может прийти за несколько периодов (все доступные
    # даты) — нормализуется КАЖДОЕ значение, а не только первое.
    out: list = []
    for metric in metrics:
        normalized_fact, finding = normalize_unit(metric.scalar_fact, metric_def)
        if normalized_fact is metric.scalar_fact:
            out.extend([finding, metric])
            continue
        out.extend([finding, ExtractedMetric(
            scalar_fact=normalized_fact, concept=metric.concept, period_ref=metric.period_ref,
            definition_origin=metric.definition_origin,
        )])
    return out


__all__ = ["normalize_unit", "norm_unit_gate", "ATOM_VERSION", "CHECK_KIND"]
