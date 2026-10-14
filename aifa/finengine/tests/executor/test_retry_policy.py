"""A2.1.2 (исключение из правила «не более одного раза»): «повторная попытка
после технической ошибки» должна быть ЯВНО зарегистрирована (`NodeRun.
attempt`), не тихой. A2 (описание, не в самом чек-листе A2.1, но прямо
востребовано текстом): «Политика повторов зависит от класса ошибки: временная
ошибка поставщика может повторяться... доменная недостижимость не повторяется
бессмысленно» — здесь это `execution_kind in ("model", "io")` повторяется по
`ExecutionBudget.max_retries`, `"deterministic"` не повторяется вовсе (баг в
детерминированном коде не чинится попыткой №2)."""
from __future__ import annotations

from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_executor import ExecutionBudget, GraphExecutor


def test_a_model_atom_retries_on_transient_failure_and_then_succeeds(atom, target, build_plan):
    calls = {"n": 0}

    def flaky_executor(args, ctx):  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("поставщик модели временно недоступен")
        return ("value",)

    registry = AtomRegistry([atom("extract.flaky", ("X",), execution_kind="model", executor=flaky_executor)])
    result, node_args = build_plan(registry, target("X"))

    exec_result = GraphExecutor(registry=registry, budget=ExecutionBudget(max_retries=2)).run(
        result.graph, node_args, run_id="run-1",
    )

    assert calls["n"] == 3, "атом обязан был повториться дважды после первых двух неудач"
    run = exec_result.node_runs[result.graph.root]
    assert run.status == "succeeded"
    assert run.attempt == 3, "число попыток должно быть явно видно в NodeRun, не скрыто"


def test_a_model_atom_exhausts_retries_and_fails_explicitly(atom, target, build_plan):
    calls = {"n": 0}

    def always_fails(args, ctx):  # noqa: ARG001
        calls["n"] += 1
        raise RuntimeError("поставщик модели недоступен")

    registry = AtomRegistry([atom("extract.broken", ("X",), execution_kind="model", executor=always_fails)])
    result, node_args = build_plan(registry, target("X"))

    exec_result = GraphExecutor(registry=registry, budget=ExecutionBudget(max_retries=2)).run(
        result.graph, node_args, run_id="run-1",
    )

    assert calls["n"] == 3, "1 первая попытка + 2 повтора = 3 вызова, не бесконечно"
    run = exec_result.node_runs[result.graph.root]
    assert run.status == "failed"
    assert run.attempt == 3


def test_a_deterministic_atom_never_retries(atom, target, build_plan):
    calls = {"n": 0}

    def always_fails(args, ctx):  # noqa: ARG001
        calls["n"] += 1
        raise ValueError("ошибка в детерминированном расчёте")

    registry = AtomRegistry([atom("calc.broken", ("X",), execution_kind="deterministic", executor=always_fails)])
    result, node_args = build_plan(registry, target("X"))

    exec_result = GraphExecutor(registry=registry, budget=ExecutionBudget(max_retries=2)).run(
        result.graph, node_args, run_id="run-1",
    )

    assert calls["n"] == 1, "детерминированная ошибка не должна повторяться — она не временная"
    run = exec_result.node_runs[result.graph.root]
    assert run.status == "failed"
    assert run.attempt == 1
