"""A1.11.10: «`GraphPlanner` не выполняет модельных вызовов и финансовых
расчётов; это подтверждается тестом зависимостей/моками»."""
from __future__ import annotations

import ast
import inspect

from finengine.skills.spec import graph_planner

_FORBIDDEN_IMPORT_ROOTS = (
    "finengine.llm", "finengine.skills.two_stage_extractor", "finengine.skills.ratios",
    "finengine.skills.bulk_extractor", "finengine.skills.extract_text",
)


def test_module_imports_nothing_from_the_model_or_calculation_layers():
    """Статическая проверка зависимостей — не косвенная: планировщик физически
    не может дойти до модельного шлюза или движка расчёта, потому что даже не
    импортирует их модули."""
    tree = ast.parse(inspect.getsource(graph_planner))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    for forbidden in _FORBIDDEN_IMPORT_ROOTS:
        hit = [m for m in imported if m == forbidden or m.startswith(forbidden + ".")]
        assert not hit, f"GraphPlanner импортирует модельный/расчётный слой: {hit}"


def test_planning_a_realistic_graph_never_calls_any_atom_executor(atom, target, policy):
    """Поведенческая проверка тем же духом: исполнители атомов в этом каталоге
    поднимают `AssertionError` при вызове (см. `conftest.py::_noop`) — если бы
    `GraphPlanner.plan()` где-то дёрнул `executor` атома, любой тест этого
    файла упал бы, а не только этот. Здесь фиксируем это намерение явно."""
    from finengine.skills.spec.atom_registry import AtomRegistry
    from finengine.skills.spec.graph_planner import GraphPlanner

    registry = AtomRegistry([
        atom("ingest.document", ("DOC",)),
        atom("calc.a", ("A",), requires=("DOC",)),
        atom("calc.b", ("B",), requires=("A",)),
    ])
    result = GraphPlanner(registry=registry, policy=policy).plan(target("B"))
    assert result.feasibility.feasible  # построился, ни разу не исполнив ни одного `_noop`
