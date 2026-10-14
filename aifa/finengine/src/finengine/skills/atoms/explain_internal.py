"""`explain.internal` — карточка 16b: объяснить изменение показателя
внутренними данными отчётности (факты, структуры, сравнение с прошлым
периодом), различая причину и сопутствующее изменение.

**Причина vs корреляция.** Каждый тезис `Explanation.factors` несёт
`causal_type` (`causal`/`correlation`/`co_movement`) — модель обязана явно
выбрать степень доказанности, а не только упомянуть связь прозой (карточка:
«Если причинность не доказана, формулировать связь/сопутствующее изменение, а
не утверждать причину»). Парсер ниже — вторая линия защиты: тезис без
`evidence_ids`, резолвящихся в контекст, отбрасывается, как и в `assess.
judgment` (не полагаемся на дисциплину модели).

**Пакетная экономика (C9).** Один физический вызов модели на ВЕСЬ пакет
концептов прогона — та же причина, что у `assess_judgment_batch`: логическая
атомизация по концепту не должна означать сто отдельных модельных вызовов."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence

from pydantic import ValidationError

from finengine.contracts import (
    CalculatedMetric,
    EvidenceBundleV2,
    Explanation,
    ExplanationFactor,
    ExtractedMetric,
    Judgment,
    ReferenceValue,
    ScalarFact,
)
from finengine.skills.spec.atom_registry import NodeArgs
from finengine.skills.spec.target import InputSpec

ATOM_VERSION = "1.0.0"

LlmJsonCall = Callable[[str, str], dict]


def explain_internal_requires(node_args: NodeArgs, run_passport) -> tuple[InputSpec, ...]:
    if run_passport is None or not node_args.concept:
        return ()
    return (
        InputSpec(input_type="MetricEvidence", concept=node_args.concept),
        InputSpec(input_type="ReferenceValue", concept=node_args.concept),
        InputSpec(input_type="Judgment", concept=node_args.concept),
        InputSpec(input_type="EvidenceBundleV2"),
    )


def _fact_view(fact, *, kind: str) -> dict:
    return {
        "evidence_id": fact.evidence_id, "concept": fact.concept, "period_ref": fact.period_ref,
        "value": fact.value, "unit": getattr(fact, "unit", None), "kind": kind,
    }


def _structure_view(fact) -> dict:
    return {"evidence_id": fact.evidence_id, "table_kind": fact.table_kind, "rows": fact.rows}


def _bundle_view(bundle: EvidenceBundleV2 | None) -> dict:
    if bundle is None:
        return {"metrics": [], "structures": []}
    return {
        "metrics": (
            [_fact_view(f, kind="extracted") for f in bundle.scalar_facts]
            + [_fact_view(f, kind="calculated") for f in bundle.derived_facts]
        ),
        "structures": [_structure_view(f) for f in bundle.structured_facts],
    }


_SYSTEM_PROMPT = (
    "Ты объясняешь изменения нескольких финансовых показателей СТРОГО по "
    "предоставленным фактам, структурам и сравнению с предыдущим периодом. "
    "Правила: (1) не выполняй арифметику и не выводи новых числовых значений; "
    "(2) каждый тезис обязан ссылаться на evidence_id хотя бы одного "
    "предоставленного факта или структуры; (3) для каждого тезиса выбери ровно "
    "один causal_type: \"causal\" — доказанная причина (например прямое "
    "факторное разложение), \"correlation\" — статистическая связь без "
    "доказанного механизма, \"co_movement\" — совпадение по времени без "
    "утверждения связи; никогда не помечай простое совпадение по времени как "
    "\"causal\"; (4) для концепта без reference (сравнение с прошлым "
    "недоступно) ограничься описанием текущего состояния; (5) не давай "
    "рекомендаций. Верни JSON вида {\"explanations\": [{\"concept\": str, "
    "\"text\": str, \"factors\": [{\"text\": str, \"causal_type\": str, "
    "\"evidence_ids\": [str, ...]}]}]}."
)


def _concept_dossier(
    concept: str, *, current_fact, reference: ReferenceValue | None, judgment: Judgment | None,
) -> dict:
    kind = "extracted" if isinstance(current_fact, ExtractedMetric) else "calculated"
    fact = current_fact.scalar_fact if isinstance(current_fact, ExtractedMetric) else current_fact.derived_fact
    return {
        "concept": concept,
        "current_fact": _fact_view(fact, kind=kind),
        "reference": (
            {
                "prior_period_ref": reference.prior_period_ref, "prior_evidence_id": reference.prior_evidence_id,
                "selection_reason": reference.selection_reason,
            } if reference is not None else None
        ),
        "judgment": (
            {"verdict": judgment.verdict, "judgment_id": judgment.judgment_id} if judgment is not None else None
        ),
    }


def _build_prompt(dossiers: list[dict], bundle_view: dict) -> str:
    payload = {"concepts": dossiers, "context": bundle_view}
    return "Объясни изменения показателей по данным ниже (JSON):\n" + json.dumps(payload, ensure_ascii=False)


def _parse_explanations(
    raw: dict, *, judgment_by_concept: dict[str, Judgment], evidence_ids: set[str],
) -> dict[str, list[Explanation]]:
    result: dict[str, list[Explanation]] = {c: [] for c in judgment_by_concept}
    for item in (raw.get("explanations") or []):
        if not isinstance(item, dict):
            continue
        concept = str(item.get("concept") or "").strip().upper()
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        factors: list[ExplanationFactor] = []
        raw_factors = item.get("factors") or []
        for f_item in raw_factors:
            if not isinstance(f_item, dict):
                continue
            ftext = str(f_item.get("text") or "").strip()
            causal_type = str(f_item.get("causal_type") or "").strip()
            if causal_type not in ("causal", "correlation", "co_movement"):
                continue
            ids = [e for e in (f_item.get("evidence_ids") or []) if isinstance(e, str) and e in evidence_ids]
            if not ftext or not ids:
                continue
            factors.append(ExplanationFactor(text=ftext, causal_type=causal_type, evidence_ids=ids))
        if raw_factors and not factors:
            # Модель заявила причинные/сопутствующие тезисы, но НИ ОДИН не
            # пережил проверку опоры — это не «чисто описательный» случай, а
            # неподтверждённая причинная формулировка (карточка: «Каждая
            # причинно-следственная формулировка... ссылается на конкретные
            # факты»). Публиковать текст с такой прозой без единого уцелевшего
            # тезиса значило бы пропустить недоказанную причинность мимо
            # проверки — тише не публиковать объяснение вовсе, тот же принцип,
            # что у `assess_judgment._parse_judgments`.
            continue
        judgment = judgment_by_concept.get(concept)
        judgment_id = judgment.judgment_id if judgment is not None else "no-judgment"
        cited = sorted({eid for f in factors for eid in f.evidence_ids})
        try:
            explanation = Explanation(judgment_id=judgment_id, text=text, cited_evidence_ids=cited, factors=factors)
        except ValidationError:
            continue
        result.setdefault(concept, []).append(explanation)
    return result


def _default_llm_json_call(prompt: str, system: str, *, model_id: str | None) -> dict:
    from finengine.llm import openrouter as llm  # noqa: PLC0415

    async def _call() -> dict:
        raw, _usage = await llm.complete_with_usage(
            prompt=prompt, system=system, model=model_id, response_format={"type": "json_object"},
        )
        try:
            return json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}

    return asyncio.run(_call())


def _anchored_current(
    current_by_concept: dict[str, ExtractedMetric | CalculatedMetric],
    reference_by_concept: dict[str, ReferenceValue],
    bundle: EvidenceBundleV2 | None,
) -> dict[str, ExtractedMetric | CalculatedMetric]:
    """«Текущий» факт показателя — тот, на который указывает его `ReferenceValue`.

    Вызывающий код собирает `current_by_concept` словарём «последний факт показателя
    побеждает», а у показателя с несколькими периодами последним может оказаться ПРЕДЫДУЩИЙ
    (живой прогон Срез 32: текст объяснения описывал `FY 2024` как текущее состояние).
    Досье модели обязано называть текущий период ровно так, как его назвал `ref.prior`."""
    if bundle is None or not reference_by_concept:
        return current_by_concept
    facts = {fact.evidence_id: fact for fact in (*bundle.scalar_facts, *bundle.derived_facts)}
    anchored = dict(current_by_concept)
    for concept, reference in reference_by_concept.items():
        fact = facts.get(reference.current_evidence_id or "")
        period = reference.current_period_ref or getattr(fact, "period_ref", None)
        if fact is None or not period:
            continue
        anchored[concept] = (
            ExtractedMetric(scalar_fact=fact, concept=concept, period_ref=period)
            if isinstance(fact, ScalarFact)
            else CalculatedMetric(derived_fact=fact, concept=concept, period_ref=period)
        )
    return anchored


def explain_internal_batch(
    concepts: Sequence[str],
    *,
    current_by_concept: dict[str, ExtractedMetric | CalculatedMetric],
    reference_by_concept: dict[str, ReferenceValue],
    judgment_by_concept: dict[str, Judgment],
    bundle: EvidenceBundleV2 | None,
    model_id: str | None = None,
    llm_json_call: LlmJsonCall | None = None,
) -> dict[str, list[Explanation]]:
    """Один физический вызов модели на весь `concepts`. Концепт без текущего
    факта не подаётся модели (нечего объяснять) — пустой список, не ошибка."""
    current_by_concept = _anchored_current(current_by_concept, reference_by_concept, bundle)
    askable = [c for c in concepts if c in current_by_concept]
    result: dict[str, list[Explanation]] = {c: [] for c in concepts}
    if not askable:
        return result

    bundle_view = _bundle_view(bundle)
    evidence_ids = {row["evidence_id"] for row in bundle_view["metrics"]}
    evidence_ids.update(row["evidence_id"] for row in bundle_view["structures"])
    dossiers = []
    for concept in askable:
        reference = reference_by_concept.get(concept)
        if reference is not None:
            evidence_ids.add(reference.prior_evidence_id)
        dossiers.append(_concept_dossier(
            concept, current_fact=current_by_concept[concept], reference=reference,
            judgment=judgment_by_concept.get(concept),
        ))
    fact = current_by_concept[askable[0]]
    evidence_ids.add((fact.scalar_fact if isinstance(fact, ExtractedMetric) else fact.derived_fact).evidence_id)
    for concept in askable:
        f = current_by_concept[concept]
        evidence_ids.add((f.scalar_fact if isinstance(f, ExtractedMetric) else f.derived_fact).evidence_id)

    prompt = _build_prompt(dossiers, bundle_view)
    call = llm_json_call or (lambda p, s: _default_llm_json_call(p, s, model_id=model_id))
    try:
        raw = call(prompt, _SYSTEM_PROMPT)
    except Exception:  # noqa: BLE001
        return result
    parsed = _parse_explanations(
        raw or {}, judgment_by_concept={c: judgment_by_concept[c] for c in askable if c in judgment_by_concept},
        evidence_ids=evidence_ids,
    )
    result.update(parsed)
    return result


__all__ = [
    "ATOM_VERSION", "explain_internal_requires", "explain_internal_batch", "LlmJsonCall",
]
