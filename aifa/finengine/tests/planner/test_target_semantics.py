"""Retrofit v9 (R3.3/R4.4, `RETROFIT-REPORT-v9.md`): `ExtractedMetric(X)` и
`CalculatedMetric(X)` — разные `Target` с разными, единственными
производителями, не один `Target(concept=X)`, разрешаемый через `PlanPolicy`
tie-break между конкурирующими producer'ами. Это центральная находка аудита
и главное структурное отличие от дорефакторного `TargetNode`."""
from __future__ import annotations

from finengine.contracts import PlanPolicy
from finengine.skills.atoms.graph_atoms import build_registry, default_policy
from finengine.skills.atoms.intent import root_from_goal
from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_planner import GraphPlanner
from finengine.skills.spec.target import InputSpec, OutputSpec, Target


def test_extracted_and_calculated_metric_are_different_targets_with_single_producer_each(atom):
    registry = AtomRegistry([
        atom("read.value", (OutputSpec(output_type="ExtractedMetric", concept="EBITDA"),)),
        atom("calc.formula_engine", (OutputSpec(output_type="CalculatedMetric", concept="EBITDA"),)),
    ])
    policy = PlanPolicy(policy_version="p1")

    extracted = Target(output_type="ExtractedMetric", concept_id="EBITDA", entity_ref="e1", period_ref="p1")
    calculated = Target(output_type="CalculatedMetric", concept_id="EBITDA", entity_ref="e1", period_ref="p1")

    extracted_result = GraphPlanner(registry=registry, policy=policy).plan(extracted)
    calculated_result = GraphPlanner(registry=registry, policy=policy).plan(calculated)

    assert extracted_result.feasibility.feasible
    assert calculated_result.feasibility.feasible
    # Ни один tie-break/ChoicePoint не понадобился — каждый Target разрешается
    # РОВНО одним производителем по построению, не выбором среди конкурентов.
    assert not extracted_result.choice_points
    assert not calculated_result.choice_points
    assert extracted_result.graph.root.atom_name == "read.value"
    assert calculated_result.graph.root.atom_name == "calc.formula_engine"
    assert extracted_result.graph.root != calculated_result.graph.root


def test_target_factory_gives_extract_and_calculate_different_output_types_for_the_same_concept():
    """`root_from_goal` (TargetFactory, R4) — то же самое на уровне
    intent-слоя, без ручной сборки `Target`."""
    from finengine.contracts import GoalIR, TargetNode

    extract_goal = GoalIR(
        raw_query="q", intent_kind="extract_registered_metric",
        targets=[TargetNode(concept="EBITDA", output_kind="scalar")],
        entity_ref="ent", period_ref="2024",
    )
    calculate_goal = GoalIR(
        raw_query="q", intent_kind="calculate_registered_metric",
        targets=[TargetNode(concept="EBITDA", output_kind="scalar")],
        entity_ref="ent", period_ref="2024",
    )
    extract_root = root_from_goal(extract_goal)[0].root
    calculate_root = root_from_goal(calculate_goal)[0].root

    assert extract_root.output_type == "ExtractedMetric"
    assert calculate_root.output_type == "CalculatedMetric"
    assert extract_root.concept_id == calculate_root.concept_id == "EBITDA"


def test_real_registry_resolves_extract_and_calculate_ebitda_to_different_producers():
    """Сквозная проверка на РЕАЛЬНОМ реестре продукта (не синтетическом) —
    migration gate требует именно это: «один end-to-end кейс
    EXTRACT_REGISTERED_METRIC и один CALCULATE_REGISTERED_METRIC для одного
    concept показывают разные Targets и разные producers»."""
    registry = build_registry(profile_id="ifrs_nf_ru")
    policy = default_policy("ifrs_nf_ru")
    planner = GraphPlanner(registry=registry, policy=policy)

    extracted = Target(output_type="ExtractedMetric", concept_id="EBITDA", entity_ref="e1", period_ref="p1")
    calculated = Target(output_type="CalculatedMetric", concept_id="EBITDA", entity_ref="e1", period_ref="p1")

    # Первый проход (двухпроходное раскрытие, A1.5) — до RunPassport оба узла
    # ещё не раскрыли requires, но уже различимы по атому-корню.
    extracted_result = planner.plan(extracted)
    calculated_result = planner.plan(calculated)

    assert extracted_result.graph.root.atom_name == "read.value"
    assert calculated_result.graph.root.atom_name == "calc.formula"
    assert extracted_result.graph.root != calculated_result.graph.root
