"""A2.1.3 / A2.1.10: логические узлы одного пакетируемого атома объединяются в
ФИЗИЧЕСКИЙ вызов только при совместимых документе/версии/политике/профиле
песочницы; результат раскладывается обратно по исходным `NodeKey`. Прямая
проверка C9 («логическая атомизация не должна превратить 100 концептов в 100
отдельных модельных вызовов»)."""
from __future__ import annotations

from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_executor import GraphExecutor
from finengine.skills.spec.target import InputSpec, OutputSpec


def test_five_concepts_from_the_same_document_become_one_physical_call(atom, target, build_plan):
    calls: list[list[str]] = []

    def batched_extract(args_list, ctx):  # noqa: ARG001
        calls.append(sorted(a.concept for a in args_list))
        return {a.concept: (f"value:{a.concept}",) for a in args_list}

    concepts = ("ASSETS", "LIABILITIES", "EQUITY", "REVENUE", "EXPENSES")
    # `OutputSpec(..., concept=...)` явно на каждый концепт — голая-строка-
    # конвенция фикстуры `atom()` дала бы всем concept=None и схлопнула бы
    # пять логических узлов в один (см. `RETROFIT-REPORT-v9.md`, R3 test suite).
    registry = AtomRegistry([
        atom(
            "extract.scalar", tuple(OutputSpec(output_type="ScalarFact", concept=c) for c in concepts),
            execution_kind="model", cost_class="model_call",
            batch_key_fields=("entity_ref", "period_ref"), executor=batched_extract,
        ),
        atom(
            "calc.combine", ("COMBINED",),
            requires=tuple(InputSpec(input_type="ScalarFact", concept=c) for c in concepts),
        ),
    ])
    result, node_args = build_plan(registry, target("COMBINED"))
    assert result.feasibility.feasible
    scalar_nodes = [n for n in result.graph.nodes if n.node_key.atom_name == "extract.scalar"]
    assert len(scalar_nodes) == 5, "пять логических узлов — иначе тест ничего не пакетирует"

    exec_result = GraphExecutor(registry=registry).run(result.graph, node_args, run_id="run-1")

    assert exec_result.succeeded
    assert len(calls) == 1, f"ожидался один физический вызов, получено {len(calls)}: {calls}"
    assert calls[0] == sorted(concepts)
    for node in scalar_nodes:
        concept = node_args[node.node_key].concept
        assert exec_result.node_runs[node.node_key].status == "succeeded"
        assert exec_result.evidence_by_key[node.node_key] == (f"value:{concept}",), (
            "результат пакета должен разложиться обратно по СВОЕМУ логическому узлу, не перепутаться с соседним"
        )


def test_different_entities_do_not_share_a_batch(atom, target, build_plan):
    """`BatchKey` включает сущность/период (A2.1.3: «совместимые документе,
    версии, политике...») — два РАЗНЫХ прогона по разным компаниям не
    объединяются в один физический вызов случайно."""
    calls: list[list[str]] = []

    def batched_extract(args_list, ctx):  # noqa: ARG001
        calls.append(sorted(f"{a.entity_ref}:{a.concept}" for a in args_list))
        return {a.concept: () for a in args_list}

    registry = AtomRegistry([
        atom("extract.scalar", ("REVENUE",), execution_kind="model", cost_class="model_call",
             batch_key_fields=("entity_ref", "period_ref"), executor=batched_extract),
        atom("calc.combine_a", ("A_DONE",), requires=("REVENUE",)),
    ])
    result_a, args_a = build_plan(registry, target("A_DONE", entity_ref="severstal"))
    result_b, args_b = build_plan(registry, target("A_DONE", entity_ref="nlmk"))

    executor = GraphExecutor(registry=registry)
    executor.run(result_a.graph, args_a, run_id="run-a")
    executor.run(result_b.graph, args_b, run_id="run-b")

    assert len(calls) == 2, "разные прогоны/сущности не должны объединяться в общий пакет"
