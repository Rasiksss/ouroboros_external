"""`out.narrative` — критерий готовности атома (16b):

1. Текст формируется из `Judgment`/`Explanation` — модель не получает право
   заново извлекать/рассчитывать числа (вход — только typed evidence).
2. Пустая доказательная база (нет ни одного `Judgment`/`Explanation`) не даёт
   текста вовсе — явный пробел, не текст ни о чём.
3. Финансовые числа связываются с `evidence_id` (`claims`), не проверяются
   ЗДЕСЬ — `verify.grounding` (следующий тест-файл использует пару
   out.narrative → verify.grounding как единую сквозную цепочку).
4. Текст не содержит рекомендаций — это отдельный отрицательный набор
   (система-промпт запрещает; здесь тестируется парсинг/структура, не
   содержательное соответствие промпту — оценка соответствия промпту
   методики требует golden-набора и рубрики, вне периметра юнит-теста).
"""
from __future__ import annotations

from finengine.contracts import (
    DerivedFact,
    EvidenceBundleV2,
    Explanation,
    ExternalFactRef,
    Judgment,
    NarrativeOutput,
    ScalarFact,
)
from finengine.skills.atoms.out_narrative import out_narrative_executor
from finengine.skills.atoms.verify_grounding import verify_grounding_gate


class _Ctx:
    def __init__(self, deps, run_id="run-1", run_passport=None):
        self._deps = deps
        self.run_id = run_id
        self.run_passport = run_passport

    def dependencies(self):
        return tuple(self._deps)


def _bundle_with_revenue(value: float = 1500.0, evidence_id: str | None = None) -> tuple[EvidenceBundleV2, str]:
    fact = ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1.0.0", source_ref="loc-1",
        concept="REVENUE", period_ref="2025", value=value, unit="млн. руб.",
    )
    bundle = EvidenceBundleV2(run_id="run-1", scalar_facts=[fact])
    return bundle, fact.evidence_id


def test_no_judgments_or_explanations_gives_no_text():
    ctx = _Ctx(deps=[])
    result = out_narrative_executor(None, ctx, llm_json_call=lambda p, s: {"text": "что-то", "claims": []})
    assert result == []


def test_text_and_claims_are_parsed_from_model_response():
    bundle, evidence_id = _bundle_with_revenue()
    judgment = Judgment(concept="REVENUE", verdict="рост", supporting_evidence_ids=[evidence_id],
                         methodology_ref="m-1")

    def fake_call(prompt, system):
        return {
            "text": "Выручка составила 1500 млн руб.",
            "claims": [{"text": "1500 млн руб.", "kind": "financial", "value": 1500.0,
                        "period_ref": "2025", "unit": "млн. руб.", "evidence_id": evidence_id}],
        }

    ctx = _Ctx(deps=[bundle, judgment])
    result = out_narrative_executor(None, ctx, llm_json_call=fake_call)
    assert len(result) == 1
    narrative = result[0]
    assert isinstance(narrative, NarrativeOutput)
    assert narrative.claims[0].evidence_id == evidence_id


def test_an_invalid_model_evidence_id_is_rebound_to_the_one_matching_fact():
    """Живой MONITOR_CHANGES может вернуть один выдуманный UUID для всех claims.

    Это ошибка сериализации ссылки, а не новое число: если value/period/unit
    однозначно указывают на ровно один факт текущего bundle, публикационный
    слой восстанавливает ссылку до обязательного grounding-гейта.
    """
    bundle, evidence_id = _bundle_with_revenue()
    judgment = Judgment(concept="REVENUE", verdict="рост", supporting_evidence_ids=[evidence_id],
                        methodology_ref="m-1")
    raw = {
        "text": "Выручка составила 1500 млн руб.",
        "claims": [{"text": "1500 млн руб.", "kind": "financial", "value": 1500.0,
                    "period_ref": "2025", "unit": "млн. руб.",
                    "evidence_id": "a2677c37-531c-4e4d-91d3-bb81974e0d61"}],
    }

    narrative = out_narrative_executor(None, _Ctx([bundle, judgment]), llm_json_call=lambda _p, _s: raw)[0]

    assert narrative.claims[0].evidence_id == evidence_id
    gated = verify_grounding_gate(None, _Ctx([narrative, bundle]))
    assert narrative in gated


def test_an_invalid_model_evidence_id_is_not_guessed_when_two_facts_match():
    first, first_id = _bundle_with_revenue()
    duplicate = first.scalar_facts[0].model_copy(update={"evidence_id": "ev-duplicate"})
    bundle = first.model_copy(update={"scalar_facts": [first.scalar_facts[0], duplicate]})
    judgment = Judgment(concept="REVENUE", verdict="рост", supporting_evidence_ids=[first_id, duplicate.evidence_id],
                        methodology_ref="m-1")
    raw = {
        "text": "Выручка составила 1500 млн руб.",
        "claims": [{"text": "1500 млн руб.", "kind": "financial", "value": 1500.0,
                    "period_ref": "2025", "unit": "млн. руб.", "evidence_id": "ev-wrong"}],
    }

    narrative = out_narrative_executor(None, _Ctx([bundle, judgment]), llm_json_call=lambda _p, _s: raw)[0]

    assert narrative.claims[0].evidence_id == "ev-wrong"
    gated = verify_grounding_gate(None, _Ctx([narrative, bundle]))
    assert any(getattr(item, "status", None) == "fail" for item in gated)


def test_claim_with_invalid_kind_is_dropped_structurally():
    bundle, evidence_id = _bundle_with_revenue()
    judgment = Judgment(concept="REVENUE", verdict="рост", supporting_evidence_ids=[evidence_id],
                         methodology_ref="m-1")

    def fake_call(prompt, system):
        return {"text": "Текст.", "claims": [{"text": "x", "kind": "not_a_real_kind", "evidence_id": evidence_id}]}

    ctx = _Ctx(deps=[bundle, judgment])
    result = out_narrative_executor(None, ctx, llm_json_call=fake_call)
    assert result[0].claims == []


def test_empty_model_text_gives_no_narrative():
    bundle, evidence_id = _bundle_with_revenue()
    judgment = Judgment(concept="REVENUE", verdict="рост", supporting_evidence_ids=[evidence_id],
                         methodology_ref="m-1")
    ctx = _Ctx(deps=[bundle, judgment])
    result = out_narrative_executor(None, ctx, llm_json_call=lambda p, s: {"text": "", "claims": []})
    assert result == []


def test_model_failure_returns_empty_without_raising():
    bundle, evidence_id = _bundle_with_revenue()
    judgment = Judgment(concept="REVENUE", verdict="рост", supporting_evidence_ids=[evidence_id],
                         methodology_ref="m-1")
    ctx = _Ctx(deps=[bundle, judgment])

    def failing_call(prompt, system):
        raise RuntimeError("gateway timeout")

    assert out_narrative_executor(None, ctx, llm_json_call=failing_call) == []


# ---------------------------------------------------------------------------
# Сквозная пара out.narrative → verify.grounding — центральный сценарий
# критерия готовности связки анализа (16b, п.1): «намеренно вставленное
# моделью неподтверждённое число блокируется».
def test_fabricated_claim_is_blocked_by_grounding_gate():
    bundle, evidence_id = _bundle_with_revenue(value=1500.0)
    judgment = Judgment(concept="REVENUE", verdict="рост", supporting_evidence_ids=[evidence_id],
                         methodology_ref="m-1")

    def fabricating_call(prompt, system):
        return {
            "text": "Выручка составила 1500 млн руб., а маржа выросла до 40%.",
            "claims": [
                {"text": "1500 млн руб.", "kind": "financial", "value": 1500.0, "period_ref": "2025",
                 "unit": "млн. руб.", "evidence_id": evidence_id},
                # Придуманное число без evidence_id — не связано ни с каким
                # доказательством этого прогона.
                {"text": "маржа выросла до 40%", "kind": "financial", "value": 0.40, "evidence_id": None},
            ],
        }

    narrative_ctx = _Ctx(deps=[bundle, judgment])
    narrative = out_narrative_executor(None, narrative_ctx, llm_json_call=fabricating_call)[0]

    gate_ctx = _Ctx(deps=[narrative, bundle])
    gated = verify_grounding_gate(None, gate_ctx)
    assert any(f.status == "fail" for f in gated)
    assert not any(isinstance(e, NarrativeOutput) for e in gated)  # барьер не пропустил текст дальше


def test_fully_grounded_narrative_passes_the_gate():
    bundle, evidence_id = _bundle_with_revenue(value=1500.0)
    judgment = Judgment(concept="REVENUE", verdict="рост", supporting_evidence_ids=[evidence_id],
                         methodology_ref="m-1")

    def clean_call(prompt, system):
        return {
            "text": "Выручка составила 1500 млн руб.",
            "claims": [{"text": "1500 млн руб.", "kind": "financial", "value": 1500.0, "period_ref": "2025",
                        "unit": "млн. руб.", "evidence_id": evidence_id}],
        }

    narrative_ctx = _Ctx(deps=[bundle, judgment])
    narrative = out_narrative_executor(None, narrative_ctx, llm_json_call=clean_call)[0]

    gate_ctx = _Ctx(deps=[narrative, bundle])
    gated = verify_grounding_gate(None, gate_ctx)
    assert all(f.status == "pass" for f in gated if f is not narrative)
    assert narrative in gated  # барьер пропустил текст дальше


def test_period_substitution_with_same_number_is_blocked():
    # Карточка 16a: «Подмена периода... при том же числе считается
    # незаземлённым утверждением» — критично проверить именно на паре
    # out.narrative → verify.grounding, а не только на голом check_grounding.
    bundle, evidence_id = _bundle_with_revenue(value=1500.0)
    judgment = Judgment(concept="REVENUE", verdict="рост", supporting_evidence_ids=[evidence_id],
                         methodology_ref="m-1")

    def wrong_period_call(prompt, system):
        return {
            "text": "В 2024 году выручка уже составляла 1500 млн руб.",
            "claims": [{"text": "1500 млн руб. в 2024", "kind": "financial", "value": 1500.0,
                        "period_ref": "2024", "unit": "млн. руб.", "evidence_id": evidence_id}],
        }

    narrative_ctx = _Ctx(deps=[bundle, judgment])
    narrative = out_narrative_executor(None, narrative_ctx, llm_json_call=wrong_period_call)[0]
    gate_ctx = _Ctx(deps=[narrative, bundle])
    gated = verify_grounding_gate(None, gate_ctx)
    assert any(f.status == "fail" for f in gated)


def test_external_facts_and_derived_metrics_are_included_as_context():
    fact = ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1.0.0", source_ref="loc-1",
        concept="REVENUE", period_ref="2025", value=1500.0, unit="млн. руб.",
    )
    derived = DerivedFact(
        produced_by_atom="calc.formula", produced_by_atom_version="1.0.0", source_ref="formula:x",
        concept="EBITDA_MARGIN", period_ref="2025", formula_id="X", formula_version="1.0.0",
        inputs=["ev-a"], value=0.24,
    )
    bundle = EvidenceBundleV2(run_id="run-1", scalar_facts=[fact], derived_facts=[derived])
    judgment = Judgment(concept="REVENUE", verdict="рост", supporting_evidence_ids=[fact.evidence_id],
                         methodology_ref="m-1")
    explanation = Explanation(judgment_id=judgment.judgment_id, text="объяснение",
                               cited_evidence_ids=[fact.evidence_id])
    external = ExternalFactRef(text="новость", entity_id="ent-1", jurisdiction="RU", date="2025-03-01",
                                source="cbr.ru", url="https://cbr.ru/1")
    seen_payload = {}

    def capturing_call(prompt, system):
        import json
        seen_payload.update(json.loads(prompt.split(":\n", 1)[1]))
        return {"text": "текст", "claims": []}

    ctx = _Ctx(deps=[bundle, judgment, explanation, external])
    out_narrative_executor(None, ctx, llm_json_call=capturing_call)
    assert len(seen_payload["facts"]) == 2
    assert len(seen_payload["external_facts"]) == 1
    assert len(seen_payload["judgments"]) == 1
    assert len(seen_payload["explanations"]) == 1
