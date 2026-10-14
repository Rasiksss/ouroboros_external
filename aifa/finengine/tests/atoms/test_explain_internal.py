"""`explain.internal` — критерий готовности атома (16b):

1. Каждая причинно-следственная формулировка ссылается на конкретные
   факты/структуры — тезис с `evidence_ids`, не резолвящимися в контекст,
   отбрасывается.
2. Атом не приписывает причину без доказательства — `causal_type` вне
   {"causal","correlation","co_movement"} отбрасывает тезис целиком.
3. Неподтверждённый тезис блокируется на уровне парсера (вторая линия
   защиты, не полагается на дисциплину модели).
4. Один физический вызов на весь пакет концептов (C9).
"""
from __future__ import annotations

from finengine.contracts import EvidenceBundleV2, Explanation, Judgment, ReferenceValue, ScalarFact
from finengine.contracts import ExtractedMetric as _ExtractedMetric
from finengine.skills.atoms.explain_internal import explain_internal_batch, explain_internal_requires
from finengine.skills.spec.atom_registry import NodeArgs


def _metric(concept: str, value: float, period_ref: str = "2025") -> _ExtractedMetric:
    fact = ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1.0.0", source_ref="loc-1",
        concept=concept, period_ref=period_ref, value=value, unit="млн. руб.",
    )
    return _ExtractedMetric(scalar_fact=fact, concept=concept, period_ref=period_ref)


def _bundle(*metrics: _ExtractedMetric) -> EvidenceBundleV2:
    return EvidenceBundleV2(run_id="run-1", scalar_facts=[m.scalar_fact for m in metrics])


def test_requires_none_without_run_passport_or_concept():
    assert explain_internal_requires(NodeArgs(concept="REVENUE"), None) == ()


def test_explanation_with_valid_evidence_and_causal_type_is_published():
    revenue = _metric("REVENUE", 1500.0)
    cost = _metric("COST_OF_SALES", 900.0)
    bundle = _bundle(revenue, cost)
    reference = ReferenceValue(
        concept="REVENUE", current_period_ref="2025", current_evidence_id=revenue.scalar_fact.evidence_id,
        prior_period_ref="FY 2024", prior_evidence_id="ev-prior", prior_metric_id="metric-prior",
        selection_reason="тест",
    )
    judgment = Judgment(concept="REVENUE", verdict="рост", supporting_evidence_ids=[revenue.scalar_fact.evidence_id],
                         methodology_ref="m-1")

    def fake_call(prompt, system):
        return {"explanations": [{
            "concept": "REVENUE", "text": "Выручка выросла за счёт роста продаж.",
            "factors": [{
                "text": "Рост продаж напрямую увеличил выручку", "causal_type": "causal",
                "evidence_ids": [revenue.scalar_fact.evidence_id],
            }],
        }]}

    result = explain_internal_batch(
        ["REVENUE"], current_by_concept={"REVENUE": revenue}, reference_by_concept={"REVENUE": reference},
        judgment_by_concept={"REVENUE": judgment}, bundle=bundle, llm_json_call=fake_call,
    )
    assert len(result["REVENUE"]) == 1
    explanation = result["REVENUE"][0]
    assert isinstance(explanation, Explanation)
    assert explanation.factors[0].causal_type == "causal"


def test_factor_with_unfounded_evidence_id_is_dropped():
    revenue = _metric("REVENUE", 1500.0)
    bundle = _bundle(revenue)

    def fake_call(prompt, system):
        return {"explanations": [{
            "concept": "REVENUE", "text": "Текст объяснения.",
            "factors": [{"text": "придуманный фактор", "causal_type": "causal", "evidence_ids": ["ev-fake"]}],
        }]}

    result = explain_internal_batch(
        ["REVENUE"], current_by_concept={"REVENUE": revenue}, reference_by_concept={},
        judgment_by_concept={}, bundle=bundle, llm_json_call=fake_call,
    )
    assert result["REVENUE"] == []


def test_co_movement_is_not_silently_upgraded_to_causal():
    revenue = _metric("REVENUE", 1500.0)
    bundle = _bundle(revenue)

    def fake_call(prompt, system):
        return {"explanations": [{
            "concept": "REVENUE", "text": "Совпадение по времени с открытием нового магазина.",
            "factors": [{
                "text": "Совпадает по времени с открытием магазина", "causal_type": "co_movement",
                "evidence_ids": [revenue.scalar_fact.evidence_id],
            }],
        }]}

    result = explain_internal_batch(
        ["REVENUE"], current_by_concept={"REVENUE": revenue}, reference_by_concept={},
        judgment_by_concept={}, bundle=bundle, llm_json_call=fake_call,
    )
    assert result["REVENUE"][0].factors[0].causal_type == "co_movement"


def test_invalid_causal_type_drops_the_whole_explanation():
    # Модель попыталась заявить причинный тезис ("definitely" — вне закрытого
    # списка causal_type) — тезис не переживает проверку, и, поскольку это
    # был ЕДИНСТВЕННЫЙ заявленный тезис, объяснение целиком не публикуется
    # (не «текст без тезисов», а неподтверждённая причинность).
    revenue = _metric("REVENUE", 1500.0)
    bundle = _bundle(revenue)

    def fake_call(prompt, system):
        return {"explanations": [{
            "concept": "REVENUE", "text": "Текст.",
            "factors": [{"text": "неопределённая связь", "causal_type": "definitely",
                         "evidence_ids": [revenue.scalar_fact.evidence_id]}],
        }]}

    result = explain_internal_batch(
        ["REVENUE"], current_by_concept={"REVENUE": revenue}, reference_by_concept={},
        judgment_by_concept={}, bundle=bundle, llm_json_call=fake_call,
    )
    assert result["REVENUE"] == []


def test_purely_descriptive_explanation_without_any_claimed_factor_is_published():
    # Модель НЕ пыталась заявить причинный тезис вовсе (`factors: []` с
    # самого начала, например при отсутствии `reference` — «без сравнения с
    # прошлым периодом ограничься описанием текущего состояния») — это
    # законное состояние, отличное от «тезис заявлен, но не пережил проверку».
    revenue = _metric("REVENUE", 1500.0)
    bundle = _bundle(revenue)

    def fake_call(prompt, system):
        return {"explanations": [{"concept": "REVENUE", "text": "Выручка за период — 1500 млн руб.", "factors": []}]}

    result = explain_internal_batch(
        ["REVENUE"], current_by_concept={"REVENUE": revenue}, reference_by_concept={},
        judgment_by_concept={}, bundle=bundle, llm_json_call=fake_call,
    )
    assert len(result["REVENUE"]) == 1
    assert result["REVENUE"][0].factors == []


def test_concept_without_current_fact_is_not_asked():
    bundle = _bundle()
    calls: list[str] = []

    def fake_call(prompt, system):
        calls.append(prompt)
        return {"explanations": []}

    result = explain_internal_batch(
        ["REVENUE"], current_by_concept={}, reference_by_concept={}, judgment_by_concept={}, bundle=bundle,
        llm_json_call=fake_call,
    )
    assert result["REVENUE"] == []
    assert calls == []


def test_model_failure_returns_empty_without_raising():
    revenue = _metric("REVENUE", 1500.0)
    bundle = _bundle(revenue)

    def failing_call(prompt, system):
        raise RuntimeError("gateway timeout")

    result = explain_internal_batch(
        ["REVENUE"], current_by_concept={"REVENUE": revenue}, reference_by_concept={}, judgment_by_concept={},
        bundle=bundle, llm_json_call=failing_call,
    )
    assert result == {"REVENUE": []}


def test_one_physical_call_covers_the_whole_batch():
    revenue = _metric("REVENUE", 1500.0)
    ebitda = _metric("EBITDA", 300.0)
    bundle = _bundle(revenue, ebitda)
    calls: list[dict] = []

    def fake_call(prompt, system):
        calls.append({"prompt": prompt})
        return {"explanations": []}

    explain_internal_batch(
        ["REVENUE", "EBITDA"], current_by_concept={"REVENUE": revenue, "EBITDA": ebitda},
        reference_by_concept={}, judgment_by_concept={}, bundle=bundle, llm_json_call=fake_call,
    )
    assert len(calls) == 1
