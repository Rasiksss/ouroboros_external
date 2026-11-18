"""`norm.time` — карточка 16a: нормализовать временной базис и сделать
допустимую годизацию явной трассировкой, а не скрытой эвристикой.

**Что уже корректно решено и не переписывается.** `formula_engine.
annualization_factor`/`annualization_note`/`days_in_period`/
`formula_carries_period_length` УЖЕ реализуют «интервальные показатели
годизируются только по явному правилу формулы» — `ANNUAL(...)`/
`DAYS_IN_PERIOD` в тексте самой формулы реестра, не скрытая эвристика
движка; `CalcDef._one_annualization_not_two` уже запрещает двойное
приведение к году на уровне схемы. `norm.time` не меняет ни одно из этих
решений — он делает уже принятое решение ВИДИМЫМ узлом графа с явной
трассировкой (карточка: «Временная семантика — доменная логика, а не
форматирование»), и добавляет то, чего сегодня НЕТ: проверку point/duration
согласованности у ИЗВЛЕЧЁННОГО факта против `MetricDefinition.period_type`.

**LTM/YTD как отдельный `DerivedFact` — вне периметра этой сессии.**
Карточка требует «LTM рассчитывается как отдельный DerivedFact payload из
явно указанных входов». Явные входы LTM — это факты ДРУГИХ периодов
(текущий интерим + предыдущий годовой − предыдущий интерим), а получение
факта другого периода того же концепта — работа `ref.prior`
(`docs/build-plan/16b-srez-06b-analiz-i-veha-m1.md`, явно отнесена к части 2
этого среза, Срез 6b). Строить LTM без `ref.prior` значило бы либо
предполагать наличие входа, которого механизм ещё не даёт, либо
реализовывать `ref.prior` вне его собственной карточки. Здесь заведена
только точка расширения (`annualization_trace`, ниже) — сама сборка LTM
подключается в 6b.
"""
from __future__ import annotations

import re

from finengine.contracts import CalculatedMetric, ExtractedMetric, MetricDefinition, ScalarFact, VerificationFinding
from finengine.skills.atoms.definitions import resolve_metric_definition
from finengine.skills.bulk_extractor.formula_engine import (
    annualization_note,
    days_in_period,
    formula_carries_period_length,
)
from finengine.skills.metric_schema import registry_for_passport
from finengine.skills.spec.atom_registry import NodeArgs

ATOM_VERSION = "1.0.0"
CHECK_KIND = "time_normalization"

# «NM» в начале метки — тот же формат, что `formula_engine._MONTHS_IN_PERIOD_RE`
# (не импортируется — приватный `re.Pattern` внутри чужого модуля; лучше
# небольшое устойчивое повторение маркера, чем протаскивать компилированный
# объект между модулями по имени с подчёркиванием).
_DURATION_MARKER_RE = re.compile(r"^(\d{1,2})M\b")
# Дата-подобная метка (`31.03.2025`, `2025-03-31`) — типичный вид point-периода.
_POINT_DATE_RE = re.compile(r"^\d{1,2}[./]\d{1,2}[./]\d{2,4}$|^\d{4}-\d{2}-\d{2}$")


def classify_extracted_period(
    fact: ScalarFact, metric_def: MetricDefinition, *, known_periods: frozenset[str] | None = None,
) -> tuple[ScalarFact, VerificationFinding]:
    """Point/duration согласованность для ИЗВЛЕЧЁННОГО факта. Возвращает
    ТОТ ЖЕ `ScalarFact` (норма — здесь нет числового преобразования, только
    проверка) либо блокирующий `VerificationFinding`, если период
    неоднозначен для этого прогона."""
    declared = (metric_def.period_type or "").strip().lower()
    label = fact.period_ref
    duration_match = _DURATION_MARKER_RE.match(label)
    looks_like_duration = bool(duration_match) and int(duration_match.group(1)) != 12
    looks_like_point_date = bool(_POINT_DATE_RE.match(label))

    conflict: str | None = None
    if declared == "point" and looks_like_duration:
        conflict = f"{fact.concept!r} объявлен моментным (point), а период {label!r} — интервал"
    elif declared == "duration" and looks_like_point_date:
        conflict = f"{fact.concept!r} объявлен интервальным (duration), а период {label!r} выглядит датой"
    elif known_periods is not None and label not in known_periods:
        conflict = f"период {label!r} не входит в известные периоды текущего прогона — неоднозначен"

    if conflict is not None:
        finding = VerificationFinding(
            produced_by_atom="norm.time", produced_by_atom_version=ATOM_VERSION,
            source_ref=fact.evidence_id, concept=fact.concept, period_ref=fact.period_ref, basis=fact.basis,
            check_kind=CHECK_KIND, status="needs_period_resolution", detail=conflict,
        )
        return fact, finding

    finding = VerificationFinding(
        produced_by_atom="norm.time", produced_by_atom_version=ATOM_VERSION,
        source_ref=fact.evidence_id, concept=fact.concept, period_ref=fact.period_ref, basis=fact.basis,
        check_kind=CHECK_KIND, status="pass",
        detail=f"период {label!r} согласован с period_type={declared or 'не объявлен'!r}",
    )
    return fact, finding


def annualization_trace(formula_id: str, expression: str, period_ref: str) -> VerificationFinding:
    """Явная трассировка ГОДИЗАЦИИ `calc.formula` (карточка: «LTM
    рассчитывается ... а не через замену меток»; здесь — только годовой
    темп, не LTM). Решение «годизировать или нет» уже приняла сама формула
    реестра (`ANNUAL(...)`/`DAYS_IN_PERIOD` в её тексте) — эта функция не
    переигрывает решение, а печатает, что оно означает для ЭТОГО периода,
    тем же вычислением, что уже использует `formula_engine`."""
    note = annualization_note(period_ref)
    carries = formula_carries_period_length(expression)
    if not carries and not note:
        detail = f"формула {formula_id!r} не годизирует поток; период {period_ref!r} не приводится к году"
    else:
        detail = (
            f"формула {formula_id!r} несёт годизацию по своему тексту "
            f"(ANNUAL/DAYS_IN_PERIOD); период {period_ref!r} = {days_in_period(period_ref)} дней"
            + (f", множитель {note}" if note else "")
        )
    return VerificationFinding(
        produced_by_atom="norm.time", produced_by_atom_version=ATOM_VERSION,
        source_ref=formula_id, concept=None, period_ref=period_ref,
        check_kind=CHECK_KIND, status="pass", detail=detail,
    )


# ---------------------------------------------------------------------------
# Graph wiring — один общий гейт для `read.value` (ExtractedMetric) и
# `calc.formula` (CalculatedMetric), тот же приём, что `verify.identity`/
# `verify.cross` делят один handler (16a, врезка «сверка механики»).
def norm_time_gate(args: NodeArgs, ctx) -> list:
    # Срез 31: значений может быть несколько (все доступные периоды) — гейт
    # проходит КАЖДОЕ, а не только первое.
    extracted = [e for e in ctx.dependencies() if isinstance(e, ExtractedMetric) and e.concept == args.concept]
    if extracted:
        passport = ctx.run_passport
        registry = registry_for_passport(passport) if passport is not None else {}
        metric_def = resolve_metric_definition(args.concept or "", registry, passport)
        out: list = []
        for metric in extracted:
            _fact, finding = classify_extracted_period(metric.scalar_fact, metric_def)
            out.extend([finding, metric])
        return out

    calculated = [e for e in ctx.dependencies() if isinstance(e, CalculatedMetric) and e.concept == args.concept]
    if calculated:
        out = []
        for metric in calculated:
            fact = metric.derived_fact
            finding = annualization_trace(fact.formula_id, fact.expression, fact.period_ref or args.period_ref or "")
            out.extend([finding, metric])
        return out

    return []


__all__ = ["classify_extracted_period", "annualization_trace", "norm_time_gate", "ATOM_VERSION", "CHECK_KIND"]
