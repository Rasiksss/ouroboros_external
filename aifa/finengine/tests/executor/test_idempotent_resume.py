"""A2.1.6: повтор запуска после технического сбоя не создаёт дублирующих
доказательств для уже успешно завершённых идемпотентных узлов. Краевой случай
39 (99-matrica.md): тайм-аут/повтор не создаёт второй запуск того же узла."""
from __future__ import annotations

from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_executor import GraphExecutor


def test_resume_skips_already_succeeded_nodes_and_reuses_their_evidence(atom, target, build_plan):
    calls = {"leaf": 0, "calc": 0}

    def leaf_executor(args, ctx):  # noqa: ARG001
        calls["leaf"] += 1
        return (f"leaf-value-{calls['leaf']}",)

    def calc_executor(args, ctx):  # noqa: ARG001
        calls["calc"] += 1
        return ("calc-value",)

    registry = AtomRegistry([
        atom("leaf.value", ("BASE",), executor=leaf_executor),
        atom("calc.result", ("RESULT",), requires=("BASE",), executor=calc_executor),
    ])
    result, node_args = build_plan(registry, target("RESULT"))
    executor = GraphExecutor(registry=registry)

    first = executor.run(result.graph, node_args, run_id="run-1")
    assert first.succeeded
    assert calls == {"leaf": 1, "calc": 1}

    leaf_node = next(n for n in result.graph.nodes if n.node_key.atom_name == "leaf.value")
    first_leaf_evidence = first.evidence_by_key[leaf_node.node_key]

    # "Падение исполнителя" смоделировано: у нас есть NodeRun/evidence только
    # для leaf.value (как если бы процесс упал ровно после него), calc.result
    # ещё не выполнялся.
    second = executor.run(
        result.graph, node_args, run_id="run-1",
        prior_node_runs={leaf_node.node_key: first.node_runs[leaf_node.node_key]},
        prior_evidence={leaf_node.node_key: first_leaf_evidence},
    )

    assert second.succeeded
    assert calls["leaf"] == 1, "leaf.value выполнился повторно при возобновлении — нарушение идемпотентности"
    assert calls["calc"] == 2, "calc.result — единственный узел, которого не хватало, обязан выполниться"
    assert second.evidence_by_key[leaf_node.node_key] == first_leaf_evidence, (
        "возобновление обязано переиспользовать УЖЕ существующее доказательство, не создавать новое"
    )
