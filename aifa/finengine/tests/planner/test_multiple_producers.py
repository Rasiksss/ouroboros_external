"""A1.6/A1.11.8: несколько достижимых производителей одного результата — без
`PlanPolicy` это `ChoicePoint`, не выбор первого зарегистрированного атома;
с приоритетом в политике — детерминированный выбор."""
from __future__ import annotations

from finengine.contracts import PlanPolicy
from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_planner import GraphPlanner


def _registry_with_two_producers(atom):
    return AtomRegistry([
        atom("extract.scalar", ("CET1",)),
        atom("extract.capital_prompt", ("CET1",)),
    ])


def test_no_policy_rule_yields_a_choice_point_not_the_first_registered_atom(atom, target, policy):
    registry = _registry_with_two_producers(atom)
    result = GraphPlanner(registry=registry, policy=policy).plan(target("CET1"))

    assert not result.feasibility.feasible
    assert len(result.choice_points) == 1
    assert set(result.choice_points[0].candidate_atoms) == {"extract.scalar", "extract.capital_prompt"}
    # Ни один из двух узлов не должен молча попасть в граф как «победитель».
    assert not any(n.node_key.atom_name in ("extract.scalar", "extract.capital_prompt") for n in result.graph.nodes)


def test_policy_priority_resolves_deterministically(atom, target):
    registry = _registry_with_two_producers(atom)
    policy = PlanPolicy(policy_version="p1", producer_priority={"CET1": ["extract.scalar"]})

    result = GraphPlanner(registry=registry, policy=policy).plan(target("CET1"))

    assert result.feasibility.feasible
    assert result.graph.root.atom_name == "extract.scalar"
    assert not result.choice_points


def test_single_reachable_producer_needs_no_policy_rule(atom, target, policy):
    """«Если достижим только один — он выбирается детерминированно» (A1.6) —
    политика без единого правила для этого концепта не мешает единственному
    кандидату."""
    registry = AtomRegistry([atom("extract.scalar", ("REVENUE",))])
    result = GraphPlanner(registry=registry, policy=policy).plan(target("REVENUE"))

    assert result.feasibility.feasible
    assert not result.choice_points
