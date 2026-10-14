"""Регрессия для `ExecutionContext.requires` пакетного вызова (Срез 5a).

До этого среза `GraphExecutor._run_batch` передавал исполнителю `requires`
ТОЛЬКО первого узла пакета — безобидное упрощение, пока единственный
пакетируемый атом (`extract.scalar`) имел одинаковые `requires` у каждого
члена пакета (один документ на пакет). `read.value` разрушил это совпадение:
каждый концепт в пакете требует СВОЙ узел `locate` (`dynamic_requires`
возвращает `locator:<concept>`), и без объединения пакетный физический вызов
видел бы кандидатов только ОДНОГО концепта из всех. Здесь воспроизводится та
же форма зависимостей на синтетических атомах, без реальных `locate`/
`read.value`."""
from __future__ import annotations

from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_executor import GraphExecutor
from finengine.skills.spec.target import InputSpec, OutputSpec


def test_batched_executor_sees_dependencies_of_every_member_not_only_the_first(atom, target, build_plan):
    def locate_like(items, ctx):  # noqa: ARG001
        return {a.concept: (f"evidence:{a.concept}",) for a in items}

    seen_dependencies: list[tuple] = []

    def read_like(items, ctx):
        seen_dependencies.append(ctx.dependencies())
        return {a.concept: (f"value:{a.concept}",) for a in items}

    def dynamic_requires(node_args, _passport):
        # `InputSpec(..., concept=...)` явно — голая строка нормализовалась
        # бы с `concept=None`, и три разных концепта (A/B/C) искали бы
        # ОДНОГО и того же (output_type="CandidateLocator", concept=None)
        # производителя вместо трёх разных.
        return (InputSpec(input_type="CandidateLocator", concept=node_args.concept),)

    registry = AtomRegistry([
        atom(
            "locate_like", tuple(OutputSpec(output_type="CandidateLocator", concept=c) for c in ("A", "B", "C")),
            execution_kind="model", cost_class="model_call",
            batch_key_fields=("entity_ref", "period_ref"), executor=locate_like,
        ),
        atom(
            "read_like", tuple(OutputSpec(output_type="ScalarFact", concept=c) for c in ("A", "B", "C")),
            execution_kind="model", cost_class="model_call",
            batch_key_fields=("entity_ref", "period_ref"), executor=read_like,
            dynamic_requires=dynamic_requires,
        ),
        atom(
            "calc.combine", ("COMBINED",),
            requires=tuple(InputSpec(input_type="ScalarFact", concept=c) for c in ("A", "B", "C")),
        ),
    ])
    result, node_args = build_plan(registry, target("COMBINED"))
    assert result.feasibility.feasible

    exec_result = GraphExecutor(registry=registry).run(result.graph, node_args, run_id="run-1")
    assert exec_result.succeeded, {k: v.error for k, v in exec_result.node_runs.items() if v.error}

    assert len(seen_dependencies) == 1, "три read_like-узла обязаны выполниться ОДНИМ физическим вызовом"
    dependencies = seen_dependencies[0]
    assert set(dependencies) == {"evidence:A", "evidence:B", "evidence:C"}, (
        "пакетный вызов обязан видеть evidence ВСЕХ узлов-зависимостей пакета (union), "
        "не только требования первого узла — иначе read.value видел бы кандидатов только одного концепта"
    )


def test_equal_requires_across_the_batch_give_the_same_result_as_before(atom, target, build_plan):
    """Надмножество, не смена поведения: когда у ВСЕХ членов пакета
    требования и так ОДИНАКОВЫЕ (форма Среза 3, до `read.value`),
    объединение даёт ТОТ ЖЕ набор, что раньше давало «requires первого узла
    пакета» — они по построению совпадали."""
    def batched_extract(items, ctx):  # noqa: ARG001
        return {a.concept: (f"value:{a.concept}",) for a in items}

    seen_dependencies: list[tuple] = []

    def batched_combine(items, ctx):
        seen_dependencies.append(ctx.dependencies())
        return {a.concept: (f"combined:{a.concept}",) for a in items}

    registry = AtomRegistry([
        atom(
            "extract.scalar", tuple(OutputSpec(output_type="ScalarFact", concept=c) for c in ("A", "B")),
            execution_kind="model", cost_class="model_call",
            batch_key_fields=("entity_ref", "period_ref"), executor=batched_extract,
        ),
        atom(
            "calc.combine", tuple(OutputSpec(output_type="Combined", concept=c) for c in ("X", "Y")),
            requires=tuple(InputSpec(input_type="ScalarFact", concept=c) for c in ("A", "B")),
            execution_kind="model", cost_class="model_call",
            batch_key_fields=("entity_ref", "period_ref"), executor=batched_combine,
        ),
        atom(
            "root.pick", ("COMBINED",),
            requires=tuple(InputSpec(input_type="Combined", concept=c) for c in ("X", "Y")),
        ),
    ])
    result, node_args = build_plan(registry, target("COMBINED"))
    exec_result = GraphExecutor(registry=registry).run(result.graph, node_args, run_id="run-1")
    assert exec_result.succeeded, {k: v.error for k, v in exec_result.node_runs.items() if v.error}
    assert len(seen_dependencies) == 1, "X и Y обязаны выполниться ОДНИМ физическим вызовом calc.combine"
    assert set(seen_dependencies[0]) == {"value:A", "value:B"}
