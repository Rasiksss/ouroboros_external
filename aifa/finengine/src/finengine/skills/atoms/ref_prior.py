"""`ref.prior` — карточка 16b: сопоставимое значение предыдущего периода для
одного концепта, выбранное ПОСЛЕ проверки periода/базиса — не второй числовой
путь, а фильтр по уже существующим `MetricEvidence` того же прогона.

**Почему без нового модельного вызова.** Финансовые отчёты почти всегда
показывают сравнительную колонку (текущий/предыдущий период) в одной и той же
таблице — `read.value`/`locate` уже извлекают ОБА периода одним пакетным
вызовом (см. `period_current`/`period_prev` в `graph_atoms.py`). Значит
`ctx.dependencies()` для концепта X при периоде `period_current` часто уже
несёт факт и за `period_prev` — этот атом ничего не извлекает заново, только
находит вторую сторону среди уже полученного и проверяет её сопоставимость.

**Период предыдущего сравнения** вычисляется ТЕМ ЖЕ вызовом
(`bulk._select_run_periods`), которым уже пользуются `locate`/`read.value` —
не изобретается вторая эвристика «ближайший другой период» (карточка:
«Ближайший по дате факт не обязательно является сопоставимым»).

**Базис** сверяется по плоской строке `EvidenceRecord.basis` — тем же
принципом и с той же честной оговоркой, что `verify_identity_cross.
_verify_pair`: полноценный многоосевой `Basis` не строится здесь заново,
пока ни один producer не прикрепляет его к факту.

**Пара периодов на реальном отчёте (Срез 32, живой прогон 25.09.2026).** Пара уровня
прогона (`_select_run_periods`) на РСБУ-отчёте ЛУКОЙЛ дала `FY 2025 → 31.12.2024`
(«предыдущий» — дата), тогда как факты интервальных показателей подписаны
`FY 2025/FY 2024`, а баланса — `31.12.2025/2024/2023`: ни один показатель не нашёл
сравнения. Поэтому, если пара прогона не совпала с ЗНАЧЕНИЯМИ самого показателя и
период явно не задан, пара строится по подписям этого показателя (`_comparable_pair`):
самый поздний период и `previous_period_label` от него; для промежуточного баланса
(дата не 31.12) — предыдущий 31.12. Пропуск года (нет точного «предыдущего») по-прежнему
не сравнивается, а не угадывается.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from types import SimpleNamespace

from finengine.contracts import CalculatedMetric, ExtractedMetric, UserProvidedMetric, ReferenceValue, RunPassport, VerificationFinding
from finengine.skills.periods import normalize_period_label, period_kind_for_enum, previous_period_label, sort_periods
from finengine.skills.spec.atom_registry import NodeArgs
from finengine.skills.spec.target import InputSpec

ATOM_VERSION = "1.0.1"

_DATE_LABEL = re.compile(r"(\d{1,2})\.(\d{2})\.(20\d{2})")


def _comparable_pair(labels: Iterable[str]) -> tuple[str, str] | None:
    """`(текущий, предыдущий)` по подписям периодов ОДНОГО показателя либо `None`.

    Текущий — самый поздний (`sort_periods` упорядочивает и интервалы, и даты).
    Предыдущий — `previous_period_label` от него (`FY 2025 → FY 2024`,
    `31.12.2025 → 31.12.2024`, `6M 2026 → 6M 2025`); для промежуточной даты баланса
    (`30.06.2026`), у которой годовой «предыдущий» (`30.06.2025`) не подписан, —
    ближайший конец прошлого года `31.12.2025`. Иначе сравнения нет."""
    ordered = sort_periods([label for label in labels if label])
    if len(ordered) < 2:
        return None
    current = ordered[0]
    previous = previous_period_label(current)
    if previous in ordered:
        return current, previous
    date = _DATE_LABEL.fullmatch(current)
    if date and date.group(2) != "12":
        year_end = f"31.12.{int(date.group(3)) - 1}"
        if year_end in ordered:
            return current, year_end
    return None


def _fact_of(evidence: ExtractedMetric | CalculatedMetric | UserProvidedMetric):
    if isinstance(evidence, ExtractedMetric):
        return evidence.scalar_fact
    if isinstance(evidence, CalculatedMetric):
        return evidence.derived_fact
    h = evidence.human_override
    return SimpleNamespace(
        evidence_id=h.evidence_id, value=h.numeric_value, unit=h.unit,
        period_ref=evidence.period_ref, basis=h.basis,
    )


def ref_prior_requires(node_args: NodeArgs, run_passport: RunPassport | None) -> tuple[InputSpec, ...]:
    if run_passport is None or not node_args.concept:
        return ()
    return (InputSpec(input_type="MetricEvidence", concept=node_args.concept),)


def ref_prior_executor(args: NodeArgs, ctx) -> list:
    from finengine.skills import bulk_extractor as bulk  # noqa: PLC0415

    passport = ctx.run_passport
    if passport is None:
        raise ValueError("ref.prior: RunPassport не зафиксирован — узел не должен был стать ready")
    concept = args.concept or ""
    candidates = [
        e for e in ctx.dependencies()
        if isinstance(e, (ExtractedMetric, CalculatedMetric, UserProvidedMetric)) and e.concept == concept
    ]
    if len(candidates) < 2:
        # Нет второй стороны в этом прогоне — штатное «предыдущего периода
        # нет» (карточка: «Отсутствие предыдущего периода возвращается явно
        # и не подменяется внешним источником»), не ошибка.
        return []

    text = ctx.inputs.document_text
    _periods_detected, period_current, period_prev = bulk._select_run_periods(
        text, "", fallback_period_label="Текущий",
    )
    value_kind = period_kind_for_enum(concept)
    requested_period = args.period_ref or period_current
    # A node-level period is the report anchor and must be projected onto the
    # metric axis (6M -> 30.06 for a balance metric).  The detector path,
    # however, is compared in its original axis first; if it mixed FY/date
    # labels, the per-concept fallback below remains observable and explains
    # the selection correctly.
    match_kind = value_kind if args.period_ref else None
    normalized_requested = normalize_period_label(requested_period, value_kind=match_kind)
    normalized_previous = normalize_period_label(period_prev, value_kind=match_kind)
    current = next(
        (
            e for e in candidates
            if normalize_period_label(_fact_of(e).period_ref, value_kind=match_kind) == normalized_requested
        ),
        None,
    )
    prior = next(
        (
            e for e in candidates
            if normalize_period_label(_fact_of(e).period_ref, value_kind=match_kind) == normalized_previous
            and e is not current
        ),
        None,
    )
    current_period = _fact_of(current).period_ref if current is not None else requested_period
    pair_source = "явно узлом" if args.period_ref else "детектором периодов"
    if current is None or prior is None:
        # Пара прогона не совпала со значениями ЭТОГО показателя (живой прогон
        # 25.09.2026: `FY 2025 → 31.12.2024` против фактов `FY 2025/FY 2024` и
        # `31.12.2025/2024/2023`). Явно заданный период не переопределяем.
        chosen = _comparable_pair(
            normalize_period_label(_fact_of(e).period_ref) for e in candidates
        )
        current_matches_chosen = (
            current is not None
            and chosen is not None
            and normalize_period_label(_fact_of(current).period_ref) == chosen[0]
        )
        if chosen is not None and (not args.period_ref or current_matches_chosen):
            by_label = {normalize_period_label(_fact_of(e).period_ref): e for e in reversed(candidates)}
            current, prior = by_label[chosen[0]], by_label[chosen[1]]
            current_period = _fact_of(current).period_ref
            pair_source = "подписями периодов самого показателя"
    if current is None or prior is None:
        return []

    fact_current, fact_prior = _fact_of(current), _fact_of(prior)
    if fact_current.basis and fact_prior.basis and fact_current.basis != fact_prior.basis:
        return [VerificationFinding(
            produced_by_atom="ref.prior", produced_by_atom_version=ATOM_VERSION,
            source_ref=f"{fact_current.evidence_id},{fact_prior.evidence_id}", concept=concept,
            period_ref=current_period, check_kind="basis", status="not_comparable",
            detail=f"базис предыдущего периода не совпадает: {fact_current.basis!r} против {fact_prior.basis!r}",
        )]
    current_unit, prior_unit = getattr(fact_current, "unit", None), getattr(fact_prior, "unit", None)
    if current_unit and prior_unit and current_unit != prior_unit:
        # Карточка: «Несопоставимая база/единица не используется без
        # нормализации» — на практике `norm.unit` уже канонизирует единицу
        # ДО этого узла (mandatory_gates на read.value), так что расхождение
        # здесь означает реальную аномалию (например разный масштаб между
        # периодами), не рутинный случай — проверка всё равно нужна как
        # вторая линия защиты, не полагающаяся на порядок гейтов.
        return [VerificationFinding(
            produced_by_atom="ref.prior", produced_by_atom_version=ATOM_VERSION,
            source_ref=f"{fact_current.evidence_id},{fact_prior.evidence_id}", concept=concept,
            period_ref=current_period, check_kind="unit", status="not_comparable",
            detail=f"единица предыдущего периода не совпадает: {current_unit!r} против {prior_unit!r}",
        )]

    return [ReferenceValue(
        concept=concept, current_period_ref=current_period, current_evidence_id=fact_current.evidence_id,
        prior_period_ref=fact_prior.period_ref, prior_evidence_id=fact_prior.evidence_id,
        prior_metric_id=prior.metric_id, basis_status="comparable",
        selection_reason=(
            f"предыдущий период {fact_prior.period_ref!r} того же прогона извлечения "
            f"(период определён {pair_source}), базис совпадает"
        ),
    )]


__all__ = ["ATOM_VERSION", "ref_prior_requires", "ref_prior_executor"]
