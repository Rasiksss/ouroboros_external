"""Retrofit v9 (R3, A1.3 п.3-6): `ProducerResolver` сначала проверяет
reachability ВСЕХ requires каждого кандидата, отбрасывает недостижимых, и
только СРЕДИ ОСТАВШИХСЯ сравнивает cost/tie-break. До retrofit `_choose_
producer` никогда не пытался развернуть requires кандидата, чтобы отбросить
недостижимого, — многократный производитель с недостижимым собственным
входом создавал `ChoicePoint`/`Unreachable`, даже если единственный
реальный конкурент был прекрасно раскрываем."""
from __future__ import annotations

from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_planner import GraphPlanner
from finengine.skills.spec.target import InputSpec, OutputSpec, Target


def _cet1_target() -> Target:
    return Target(output_type="CET1", concept_id="CET1", entity_ref="e1", period_ref="p1")


def test_unreachable_candidate_is_silently_dropped_in_favor_of_the_reachable_one(atom, policy):
    registry = AtomRegistry([
        atom("ingest.document", ("DOC",)),
        # Кандидат A: раскрывается (DOC доступен через ingest.document).
        atom(
            "extract.capital_a", (OutputSpec(output_type="CET1", concept="CET1"),),
            requires=(InputSpec(input_type="DOC"),),
        ),
        # Кандидат B: НЕ раскрывается — его собственный обязательный вход
        # PROPRIETARY_FEED не имеет производителя нигде в реестре.
        atom(
            "extract.capital_b", (OutputSpec(output_type="CET1", concept="CET1"),),
            requires=(InputSpec(input_type="PROPRIETARY_FEED"),),
        ),
    ])

    result = GraphPlanner(registry=registry, policy=policy).plan(_cet1_target())

    assert result.feasibility.feasible, result.feasibility.reason
    assert not result.choice_points, "единственный РЕАЛЬНО достижимый кандидат не должен требовать ChoicePoint"
    assert result.graph.root.atom_name == "extract.capital_a"
    assert not any(n.node_key.atom_name == "extract.capital_b" for n in result.graph.nodes), (
        "недостижимый кандидат не должен попасть в граф как побочный узел пробы"
    )


def test_both_candidates_unreachable_gives_unreachable_not_choice_point(atom, policy):
    registry = AtomRegistry([
        atom(
            "extract.capital_a", (OutputSpec(output_type="CET1", concept="CET1"),),
            requires=(InputSpec(input_type="FEED_A"),),
        ),
        atom(
            "extract.capital_b", (OutputSpec(output_type="CET1", concept="CET1"),),
            requires=(InputSpec(input_type="FEED_B"),),
        ),
    ])

    result = GraphPlanner(registry=registry, policy=policy).plan(_cet1_target())

    assert not result.feasibility.feasible
    assert not result.choice_points, "ни один кандидат не достижим — это Unreachable, не неоднозначность"
    assert len(result.unreachable) >= 1
