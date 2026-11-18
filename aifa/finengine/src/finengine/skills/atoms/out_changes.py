"""`out.changes` — итог MONITOR_CHANGES по ОДНОМУ документу (карточка 32).

Таблица «показатель: предыдущий период → текущий, Δ, Δ %» строится здесь кодом из
уже проверенных фактов (`EvidenceBundleV2`) и пар `ReferenceValue` от `ref.prior`.
Ни модель, ни клиент проценты не считают: промпты `assess.judgment`/`explain.internal`
запрещают арифметику, поэтому «+12 %» в тексте модели появиться не может.

**Объяснение — best-effort, таблица — нет.** Атом требует `NarrativeOutput`, но если
`verify.grounding` заблокировал текст, гейт возвращает только находки (узел при этом
`succeeded`) — отчёт всё равно строится, с `explanation_status="unavailable"` и
названной причиной. Пара периодов берётся ТОЛЬКО из `ref.prior` (не изобретается
вторая эвристика «ближайший период»; `subject_history.build_series` сортирует подписи
как строки и мог бы перевернуть знак).

**Строки без сравнения не выбрасываются молча.** Показатель с текущим значением, но без
предыдущего периода, либо с несопоставимым базисом/единицей — строка со статусом и
причиной. Показатели роли, которых в отчёте нет вовсе, — один `OutputGap` со счётчиком.
"""
from __future__ import annotations

from collections.abc import Mapping

from finengine.contracts import (
    EvidenceBundleV2,
    EvidenceRef,
    NarrativeOutput,
    OutputGap,
    OutputRunMetadata,
    PeriodChangeReport,
    PeriodChangeRow,
    ReferenceValue,
    RunPassport,
    VerificationFinding,
)
from finengine.roles.access import OTHER_ROLE
from finengine.roles.scope import build_engine_scope

ATOM_VERSION = "1.0.0"

_STATUS_RANK = {"computed": 0, "zero_base": 1, "not_comparable": 2, "no_prior_period": 3}


def scope_concepts(extra: Mapping[str, str], passport: RunPassport) -> tuple[str, ...]:
    """Показатели, по которым строится таблица: явный `requested_concepts` либо состав
    по РОЛИ — тот же, что у `analyze.full_bundle`/`out.narrative` (`basis_full=False`)."""
    requested = tuple(
        value.strip() for value in (extra.get("requested_concepts") or "").split(",") if value.strip()
    )
    if requested:
        return tuple(sorted(set(requested)))
    role = extra.get("role") or OTHER_ROLE
    _, enums = build_engine_scope(
        role, passport.classification.company_type, passport.classification.profile_id,
        basis_full=False,
    )
    return tuple(sorted(enums))


def _labels(profile_id: str) -> dict[str, str]:
    """Русские названия показателей профиля; неизвестный профиль — подпись = код."""
    from finengine.skills.metric_schema import registry_for_profile  # noqa: PLC0415

    try:
        registry = registry_for_profile(profile_id)
    except FileNotFoundError:
        return {}
    labels = {
        str(item["enum"]).strip().upper(): str(item.get("name_ru") or "").strip()
        for item in registry.get("extractable", ())
    }
    labels.update({
        str(item[0]).strip().upper(): str(item[1] or "").strip()
        for item in registry.get("calculated", ())
    })
    return labels


def _ref(fact) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=fact.evidence_id, concept=fact.concept, period_ref=fact.period_ref,
        source_ref=fact.source_ref, document_version=fact.document_version,
    )


def _draft_compared(concept: str, label: str, reference: ReferenceValue, current, prior) -> dict:
    current_value, prior_value = float(current.value), float(prior.value)
    absolute = current_value - prior_value
    draft = {
        "concept": concept, "label": label, "unit": getattr(current, "unit", None),
        "current_period_ref": reference.current_period_ref or current.period_ref,
        "prior_period_ref": reference.prior_period_ref or prior.period_ref,
        "current_value": current_value, "prior_value": prior_value,
        "absolute_change": absolute, "evidence_refs": [_ref(current), _ref(prior)],
    }
    if draft["current_period_ref"] is None or draft["prior_period_ref"] is None:
        return _draft_without_change(
            concept, label, "not_comparable", "период одного из значений не определён",
        )
    if prior_value == 0:
        return {**draft, "status": "zero_base",
                "reason": "предыдущее значение равно нулю — процент не определён"}
    # Одно деление по абсолютной базе: знак процента — знак разницы, а 10/100*100
    # не превращается в 10.000000000000002.
    return {**draft, "status": "computed",
            "relative_change_pct": absolute * 100.0 / abs(prior_value)}


def _draft_without_change(concept: str, label: str, status: str, reason: str, *, facts=()) -> dict:
    draft = {"concept": concept, "label": label, "status": status, "reason": reason,
             "evidence_refs": [_ref(fact) for fact in facts]}
    if len(facts) == 1 and status == "no_prior_period":
        # Единственное значение показателя однозначно и является текущим.
        fact = facts[0]
        draft.update(
            unit=getattr(fact, "unit", None), current_period_ref=fact.period_ref,
            current_value=float(fact.value),
        )
    return draft


def _sort_key(draft: dict) -> tuple:
    if draft.get("relative_change_pct") is not None:
        magnitude = abs(draft["relative_change_pct"])
    else:
        magnitude = abs(draft.get("absolute_change") or 0.0)
    return (_STATUS_RANK[draft["status"]], -magnitude, draft["concept"])


def _explanation_note(dependencies) -> str:
    blocked = [
        item for item in dependencies
        if isinstance(item, VerificationFinding) and item.check_kind == "grounding" and item.status == "fail"
    ]
    if blocked:
        details = list(dict.fromkeys(item.detail for item in blocked if item.detail))[:3]
        return (
            "объяснение заблокировано проверкой опоры на доказательства "
            f"({len(blocked)} неподтверждённых утверждений): {'; '.join(details)}"
        )
    return "объяснение не сформировано: модель не вернула текст по проверенным суждениям"


def out_changes_executor(args, ctx) -> list[PeriodChangeReport]:
    passport = ctx.run_passport
    if passport is None:
        raise ValueError("out.changes: RunPassport не зафиксирован — узел не должен был стать ready")
    dependencies = ctx.dependencies()

    bundle = next((item for item in dependencies if isinstance(item, EvidenceBundleV2)), None)
    facts_by_id: dict[str, object] = {}
    facts_by_concept: dict[str, list] = {}
    for fact in (*bundle.scalar_facts, *bundle.derived_facts) if bundle is not None else ():
        facts_by_id[fact.evidence_id] = fact
        if fact.concept:
            facts_by_concept.setdefault(fact.concept, []).append(fact)
    references = {
        item.concept: item for item in dependencies
        if isinstance(item, ReferenceValue) and item.source_kind == "prior_period"
    }
    not_comparable = {
        item.concept: item for item in dependencies
        if isinstance(item, VerificationFinding) and item.produced_by_atom == "ref.prior"
        and item.status == "not_comparable" and item.concept
    }
    narrative = next((item for item in dependencies if isinstance(item, NarrativeOutput)), None)

    labels = _labels(passport.classification.profile_id)
    drafts: list[dict] = []
    absent = 0
    concepts = scope_concepts(args.extra, passport)
    for concept in concepts:
        label = labels.get(concept) or concept
        reference = references.get(concept)
        if reference is not None:
            current = facts_by_id.get(reference.current_evidence_id or "")
            prior = facts_by_id.get(reference.prior_evidence_id or "")
            if current is None or prior is None:
                drafts.append(_draft_without_change(
                    concept, label, "not_comparable",
                    "значение сравнения не найдено в пакете доказательств",
                ))
            else:
                drafts.append(_draft_compared(concept, label, reference, current, prior))
        elif concept in not_comparable:
            finding = not_comparable[concept]
            found = [facts_by_id[eid] for eid in finding.source_ref.split(",") if eid in facts_by_id]
            drafts.append(_draft_without_change(
                concept, label, "not_comparable",
                finding.detail or "значения периодов несопоставимы", facts=found,
            ))
        elif facts_by_concept.get(concept):
            drafts.append(_draft_without_change(
                concept, label, "no_prior_period",
                "предыдущий период для сравнения не найден в отчёте",
                facts=facts_by_concept[concept],
            ))
        else:
            absent += 1

    if not drafts:
        # Нечего показывать: шлюз превратит пустой корень в типизированный отказ.
        return []
    drafts.sort(key=_sort_key)
    rows = [
        PeriodChangeRow(row_id=f"changes:{draft['concept']}", order=index, **draft)
        for index, draft in enumerate(drafts)
    ]
    gaps = [OutputGap(
        code="concepts_not_found", required=False,
        message=f"В отчёте не найдено показателей роли: {absent} из {len(concepts)}",
    )] if absent else []
    ready = narrative is not None
    return [PeriodChangeReport(
        run_id=ctx.run_id,
        status="complete" if ready else "partial",
        role=args.extra.get("role") or ctx.inputs.role or OTHER_ROLE,
        rows=rows,
        explanation=narrative,
        explanation_status="ready" if ready else "unavailable",
        explanation_note=None if ready else _explanation_note(dependencies),
        gaps=gaps,
        run_metadata=OutputRunMetadata.from_passport(ctx.run_id, passport),
    )]


__all__ = ["ATOM_VERSION", "out_changes_executor", "scope_concepts"]
