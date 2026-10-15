"""A2.1.4: ошибка одного концепта внутри пакетного чтения не переводит
успешно извлечённые соседние концепты в ошибку. Сквозной приёмочный сценарий
#14 (99-matrica.md)."""
from __future__ import annotations

from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_executor import GraphExecutor
from finengine.skills.spec.target import InputSpec, OutputSpec


def test_one_missing_concept_in_a_batch_does_not_fail_its_neighbors(atom, target, build_plan):
    def batched_extract(args_list, ctx):  # noqa: ARG001
        # RIGHT_OF_USE_ASSETS «не нашлось» — просто отсутствует в ответе, как
        # реально ведёт себя движок на показателе, которого нет в отчёте.
        return {a.concept: (f"value:{a.concept}",) for a in args_list if a.concept != "RIGHT_OF_USE_ASSETS"}

    concepts = ("SALES_REVENUE", "RIGHT_OF_USE_ASSETS")
    registry = AtomRegistry([
        atom(
            "extract.scalar", tuple(OutputSpec(output_type="ScalarFact", concept=c) for c in concepts),
            execution_kind="model", cost_class="model_call",
            batch_key_fields=("entity_ref", "period_ref"), executor=batched_extract,
        ),
        atom(
            "compose.form", ("FORM",),
            requires=tuple(InputSpec(input_type="ScalarFact", concept=c) for c in concepts),
        ),
    ])
    result, node_args = build_plan(registry, target("FORM"))
    assert result.feasibility.feasible

    exec_result = GraphExecutor(registry=registry).run(result.graph, node_args, run_id="run-1")

    revenue_node = next(n for n in result.graph.nodes if node_args[n.node_key].concept == "SALES_REVENUE")
    missing_node = next(n for n in result.graph.nodes if node_args[n.node_key].concept == "RIGHT_OF_USE_ASSETS")

    assert exec_result.node_runs[revenue_node.node_key].status == "succeeded"
    assert exec_result.evidence_by_key[revenue_node.node_key] == ("value:SALES_REVENUE",)
    assert exec_result.node_runs[missing_node.node_key].status == "failed"
    # Отсутствие одного показателя в пакете — не повод валить корень формы:
    # форма обязана прийти с недоступной строкой (решение №8), не пропасть
    # целиком. `compose.form` в этом синтетическом реестре требует ОБА
    # показателя как обязательные — здесь он корректно блокируется; реальный
    # `compose.form` (Срез 3, интеграционный тест роли) метит недостающую
    # строку как `optional_requires`, чтобы форма всё равно собралась
    # (см. tests/integration/test_graph_role_form.py).
    assert exec_result.node_runs[result.graph.root].status == "blocked"
