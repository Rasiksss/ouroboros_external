"""C0.1: `tests/golden/planner/` — ручные сценарии валидны, генератор держит инварианты.

`synthetic_generator.py` не лежит в `src/finengine` (это тестовые данные, не
продуктовый код), поэтому грузится через `spec_from_file_location` — тот же
приём, которым `tests/build_metric_registry.py` уже пользуется в этом
репозитории (см. `test_dead_code_stays_out.py`).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from finengine.contracts import PlanGraph, PlanNode, TargetNode

ROOT = Path(__file__).resolve().parent / "golden" / "planner"
MANUAL_DIR = ROOT / "manual"


def _plan_graph_from_fixture(raw: dict) -> PlanGraph:
    """Строит `PlanGraph`, отбросив человеческие `note`-поля из фикстур.

    Фикстуры в `manual/` держат `note` рядом с узлом как пояснение для
    читателя сценария — это не часть контракта `PlanNode` (у него
    `extra=\"forbid\"`), и не должно быть частью его схемы только ради того,
    чтобы тестовые данные проходили как есть.
    """
    node_fields = set(PlanNode.model_fields)
    nodes = [{k: v for k, v in node.items() if k in node_fields} for node in raw["nodes"]]
    return PlanGraph(plan_id=raw["plan_id"], root=raw["root"], nodes=nodes, graph_hash=raw["graph_hash"])


def _load_generator():
    spec = importlib.util.spec_from_file_location("planner_synthetic_generator", ROOT / "synthetic_generator.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GEN = _load_generator()


def _manual_scenarios() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(MANUAL_DIR.glob("*.json"))]


def test_at_least_six_manual_scenarios():
    assert len(list(MANUAL_DIR.glob("*.json"))) >= 6


@pytest.mark.parametrize("scenario", _manual_scenarios(), ids=lambda s: s["scenario_id"])
def test_manual_scenario_deserializes_into_real_contracts(scenario):
    TargetNode(**scenario["target"])
    graph = _plan_graph_from_fixture(scenario["plan_graph"])
    assert any(GEN._key_tuple(n.node_key) == GEN._key_tuple(graph.root) for n in graph.nodes), (
        "root должен быть ключом одного из узлов графа"
    )


def test_the_negative_scenario_is_actually_cyclic():
    scenario = next(s for s in _manual_scenarios() if s["scenario_id"] == "planner-06-would-be-cyclic-negative")
    graph = _plan_graph_from_fixture(scenario["plan_graph"])
    assert not GEN.is_acyclic(graph), "фикстур 06 обязан быть циклическим — иначе он не проверяет детектор"


def test_the_other_five_manual_scenarios_are_acyclic():
    for scenario in _manual_scenarios():
        if scenario["scenario_id"] == "planner-06-would-be-cyclic-negative":
            continue
        graph = _plan_graph_from_fixture(scenario["plan_graph"])
        assert GEN.is_acyclic(graph), f"{scenario['scenario_id']} должен быть валидным (ацикличным) графом"


@pytest.mark.parametrize("seed", range(25))
def test_synthetic_random_dag_is_always_acyclic_and_self_consistent(seed):
    graph = GEN.generate_random_dag(num_nodes=12, seed=seed)
    assert GEN.is_acyclic(graph)
    assert GEN.all_requires_resolve_to_known_nodes(graph)


def test_synthetic_cyclic_graph_is_caught_by_the_detector():
    graph = GEN.generate_cyclic_graph(seed=1)
    assert not GEN.is_acyclic(graph)


def test_generator_rejects_zero_nodes():
    with pytest.raises(ValueError):
        GEN.generate_random_dag(num_nodes=0, seed=1)
