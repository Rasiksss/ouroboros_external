"""Взаимодействие A1.5 (двухпроходное раскрытие) и A2 (исполнитель): узел с
`needs_run_passport=True` не выполняется, пока `GraphExecutor.run()` не
получил настоящий `RunPassport` — даже если формально все его `requires`
(усечённые в первом проходе до заглушки на `identify.classify`) уже
succeeded. Регрессия конкретного дефекта: первый проход исполнял такой узел
по НЕПОЛНОМУ набору входов, фиксировал ложный `succeeded`, а второй проход
через идемпотентное возобновление (A2.1.6) принимал этот ложный результат за
готовый и никогда не пересчитывал по-настоящему."""
from __future__ import annotations

from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_executor import GraphExecutor


def test_gated_node_stays_planned_without_a_run_passport_even_if_its_stub_deps_succeeded(atom, target, build_plan):
    calls = {"gated": 0}

    def gated_executor(args, ctx):  # noqa: ARG001
        calls["gated"] += 1
        return ("computed",)

    registry = AtomRegistry([
        atom("ingest.document", ("DOC",)),
        atom("identify.classify", ("RunPassport",), requires=("DOC",)),
        atom("calc.gated", ("RESULT",), needs_run_passport=True,
             dynamic_requires=lambda args, passport: (), executor=gated_executor),
    ])
    result, node_args = build_plan(registry, target("RESULT"))
    assert result.pending_run_passport

    first_exec = GraphExecutor(registry=registry).run(result.graph, node_args, run_id="run-1")

    gated_key = result.graph.root
    assert calls["gated"] == 0, "узел с needs_run_passport не имел права выполниться без RunPassport"
    assert first_exec.node_runs[gated_key].status == "planned"
    assert gated_key not in first_exec.evidence_by_key or first_exec.evidence_by_key.get(gated_key) in (None, ())
