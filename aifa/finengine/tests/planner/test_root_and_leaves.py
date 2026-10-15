"""A1.11.2/A1.11.3: ровно одна пользовательская корневая вершина, и каждая
терминальная ветвь заканчивается либо доступным листом, либо явным
`Unreachable`/`ChoicePoint` — без скрытых незакрытых зависимостей."""
from __future__ import annotations

from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_planner import GraphPlanner


def test_single_leaf_chain_has_one_root_and_one_leaf(atom, target, policy):
    registry = AtomRegistry([
        atom("ingest.document", ("DOC",)),
        atom("extract.scalar", ("REVENUE",), requires=("DOC",)),
    ])
    result = GraphPlanner(registry=registry, policy=policy).plan(target("REVENUE"))

    assert result.feasibility.feasible
    assert result.graph.root.atom_name == "extract.scalar"
    leaves = [n for n in result.graph.nodes if not n.requires]
    assert len(leaves) == 1
    assert leaves[0].node_key.atom_name == "ingest.document"


def test_fan_in_root_stays_a_single_root_key(atom, target, policy):
    """Корень с двумя независимыми ветвями зависимостей — граф раскрывается
    в дерево, но `graph.root` остаётся ОДНИМ узлом, не парой параллельных
    целей (A1.11.2: «планировщик не создаёт параллельный список верхнеуровневых
    целей»)."""
    registry = AtomRegistry([
        atom("ingest.document", ("DOC",)),
        atom("extract.scalar", ("ASSETS", "LIABILITIES"), requires=("DOC",)),
        atom("calc.ratio", ("RATIO",), requires=("ASSETS", "LIABILITIES")),
    ])
    result = GraphPlanner(registry=registry, policy=policy).plan(target("RATIO"))

    assert result.feasibility.feasible
    assert result.graph.root.atom_name == "calc.ratio"
    root_nodes = [n for n in result.graph.nodes if n.node_key == result.graph.root]
    assert len(root_nodes) == 1


def test_unreachable_target_still_produces_a_graph_with_one_node(atom, target, policy):
    """golden-фикстур `planner-05-unreachable-target`: концепт без
    производителя — узел заведён как корень, но без requires/produces."""
    registry = AtomRegistry([atom("ingest.document", ("DOC",))])
    result = GraphPlanner(registry=registry, policy=policy).plan(target("BRAND_KARMA_SCORE"))

    assert not result.feasibility.feasible
    # Retrofit v9: `Target` не несёт `target_id` (в отличие от `TargetNode`) —
    # ярлык недостижимой цели строится из output_type/concept напрямую.
    assert result.feasibility.unreachable_targets == ["BRAND_KARMA_SCORE"]
    assert len(result.unreachable) == 1
    assert result.unreachable[0].concept == "BRAND_KARMA_SCORE"
    assert len(result.graph.nodes) == 1
    assert result.graph.nodes[0].requires == []
    assert result.graph.nodes[0].produces == []
