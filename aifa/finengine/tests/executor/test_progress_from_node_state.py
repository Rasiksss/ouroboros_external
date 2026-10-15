"""A2.1.9: пользовательский прогресс строится из ФАКТИЧЕСКИХ состояний
`NodeRun`, а не из отдельного текста, который модель объявила заранее.
`on_node_update` — единственный канал, которым `GraphExecutor` сообщает о
прогрессе наружу; здесь проверяется, что он действительно отражает реальные
переходы автомата, а не что-то придуманное поверх."""
from __future__ import annotations

from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_executor import GraphExecutor


def test_observed_transitions_match_the_real_automaton(atom, target, build_plan):
    def ok_executor(args, ctx):  # noqa: ARG001
        return ("value",)

    def boom_executor(args, ctx):  # noqa: ARG001
        raise RuntimeError("модель недоступна")

    registry = AtomRegistry([
        atom("extract.ok", ("OK",), executor=ok_executor),
        atom("extract.broken", ("BROKEN",), executor=boom_executor),
        atom("calc.root", ("ROOT",), requires=("OK", "BROKEN")),
    ])
    result, node_args = build_plan(registry, target("ROOT"))

    seen: list[tuple[str, str]] = []
    GraphExecutor(registry=registry).run(
        result.graph, node_args, run_id="run-1",
        on_node_update=lambda run: seen.append((run.node_key.atom_name, run.status)),
    )

    by_atom: dict[str, list[str]] = {}
    for atom_name, status in seen:
        by_atom.setdefault(atom_name, []).append(status)

    # extract.ok — успешный путь: planned → running → succeeded.
    assert by_atom["extract.ok"] == ["planned", "running", "succeeded"]
    # extract.broken — реальный сбой: planned → running → failed, БЕЗ
    # промежуточного «подделанного» статуса.
    assert by_atom["extract.broken"] == ["planned", "running", "failed"]
    # calc.root заблокирован зависимостью — он НИКОГДА не был running: узел
    # не запускался, только унаследовал блокировку.
    assert by_atom["calc.root"] == ["planned", "blocked"]
    assert "running" not in by_atom["calc.root"]
