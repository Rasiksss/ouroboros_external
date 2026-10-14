"""Retrofit v9 (R3, A1.4 п.9 / A1.6): равная minimum incremental cost среди
нескольких достижимых производителей разрешается ТОЛЬКО явным
`PlanPolicy.producer_priority`; без записи в политике — `ChoicePoint`, не
выбор первого по порядку регистрации."""
from __future__ import annotations

from finengine.contracts import PlanPolicy
from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_planner import GraphPlanner
from finengine.skills.spec.target import OutputSpec, Target


def _equal_cost_registry(atom):
    return AtomRegistry([
        atom("extract.scalar", (OutputSpec(output_type="CET1", concept="CET1"),), cost_class="free"),
        atom("extract.capital_prompt", (OutputSpec(output_type="CET1", concept="CET1"),), cost_class="free"),
    ])


def _cet1_target() -> Target:
    return Target(output_type="CET1", concept_id="CET1", entity_ref="e1", period_ref="p1")


def test_equal_cost_without_policy_yields_a_choice_point_not_the_first_registered(atom, policy):
    registry = _equal_cost_registry(atom)
    result = GraphPlanner(registry=registry, policy=policy).plan(_cet1_target())

    assert not result.feasibility.feasible
    assert len(result.choice_points) == 1
    assert set(result.choice_points[0].candidate_atoms) == {"extract.scalar", "extract.capital_prompt"}
    assert not any(n.node_key.atom_name in ("extract.scalar", "extract.capital_prompt") for n in result.graph.nodes)


def test_equal_cost_with_policy_priority_resolves_deterministically(atom):
    registry = _equal_cost_registry(atom)
    policy = PlanPolicy(policy_version="p1", producer_priority={"CET1": ["extract.capital_prompt"]})

    result = GraphPlanner(registry=registry, policy=policy).plan(_cet1_target())

    assert result.feasibility.feasible
    assert not result.choice_points
    assert result.graph.root.atom_name == "extract.capital_prompt"


def test_registration_order_never_decides_an_equal_cost_tie(atom, policy):
    """Тот же реестр, атомы зарегистрированы в ОБРАТНОМ порядке — результат
    (ChoicePoint с теми же двумя именами) не должен зависеть от порядка."""
    reversed_registry = AtomRegistry([
        atom("extract.capital_prompt", (OutputSpec(output_type="CET1", concept="CET1"),), cost_class="free"),
        atom("extract.scalar", (OutputSpec(output_type="CET1", concept="CET1"),), cost_class="free"),
    ])
    forward = GraphPlanner(registry=_equal_cost_registry(atom), policy=policy).plan(_cet1_target())
    backward = GraphPlanner(registry=reversed_registry, policy=policy).plan(_cet1_target())

    assert forward.feasibility == backward.feasibility
    assert {c.concept for c in forward.choice_points} == {c.concept for c in backward.choice_points}
    assert (
        set(forward.choice_points[0].candidate_atoms) == set(backward.choice_points[0].candidate_atoms)
    )
