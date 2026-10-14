"""`explain.internal`: «текущий» факт в досье модели — тот, на который указывает `ReferenceValue`.

Живой прогон 25.09.2026 (Срез 32): вызывающий код собирает `current_by_concept` словарём
«последний факт показателя побеждает», а у показателя с несколькими периодами последним
оказывался ПРЕДЫДУЩИЙ, и объяснение описывало `FY 2024` как текущее состояние. Досье обязано
называть текущий период ровно так, как его назвал `ref.prior`.
"""
from __future__ import annotations

import json

import pytest

from finengine.contracts import EvidenceBundleV2, ExtractedMetric, ReferenceValue, ScalarFact
from finengine.skills.atoms.explain_internal import explain_internal_batch


def _fact(period: str, value: float) -> ScalarFact:
    return ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1.0.0", source_ref=f"loc:{period}",
        concept="REVENUE", period_ref=period, value=value, unit="млн. руб.",
    )


def _metric(fact: ScalarFact) -> ExtractedMetric:
    return ExtractedMetric(scalar_fact=fact, concept="REVENUE", period_ref=fact.period_ref)


def _dossier(current: ExtractedMetric, reference: ReferenceValue | None, bundle: EvidenceBundleV2) -> dict:
    prompts: list[str] = []

    def fake_model(prompt: str, system: str) -> dict:  # noqa: ARG001
        prompts.append(prompt)
        return {"explanations": []}

    explain_internal_batch(
        ["REVENUE"], current_by_concept={"REVENUE": current},
        reference_by_concept={"REVENUE": reference} if reference else {},
        judgment_by_concept={}, bundle=bundle, llm_json_call=fake_model,
    )
    payload = json.loads(prompts[0].split("(JSON):\n", 1)[1])
    return next(item for item in payload["concepts"] if item["concept"] == "REVENUE")


@pytest.mark.parametrize("wrongly_last", ["prior", "current"])
def test_the_dossier_names_the_referenced_fact_as_current_whichever_fact_came_last(wrongly_last):
    current, prior = _fact("FY 2025", 1500.0), _fact("FY 2024", 1200.0)
    reference = ReferenceValue(
        concept="REVENUE", current_period_ref="FY 2025", current_evidence_id=current.evidence_id,
        prior_period_ref="FY 2024", prior_evidence_id=prior.evidence_id, selection_reason="fixture",
    )
    bundle = EvidenceBundleV2(run_id="run-1", scalar_facts=[prior, current])

    dossier = _dossier(_metric(prior if wrongly_last == "prior" else current), reference, bundle)

    assert dossier["current_fact"]["period_ref"] == "FY 2025"
    assert dossier["current_fact"]["evidence_id"] == current.evidence_id
    assert dossier["current_fact"]["value"] == 1500.0
    assert dossier["reference"]["prior_period_ref"] == "FY 2024"


def test_without_a_reference_the_given_current_fact_is_kept():
    only, other = _fact("FY 2025", 1500.0), _fact("FY 2024", 1200.0)

    dossier = _dossier(_metric(other), None, EvidenceBundleV2(run_id="run-1", scalar_facts=[only, other]))

    assert dossier["current_fact"]["period_ref"] == "FY 2024"
    assert dossier["reference"] is None


def test_a_reference_to_a_fact_missing_from_the_bundle_keeps_the_given_current_fact():
    given, absent = _fact("FY 2024", 1200.0), _fact("FY 2025", 1500.0)
    reference = ReferenceValue(
        concept="REVENUE", current_period_ref="FY 2025", current_evidence_id=absent.evidence_id,
        prior_period_ref="FY 2024", prior_evidence_id=given.evidence_id, selection_reason="fixture",
    )

    dossier = _dossier(_metric(given), reference, EvidenceBundleV2(run_id="run-1", scalar_facts=[given]))

    assert dossier["current_fact"]["period_ref"] == "FY 2024"
