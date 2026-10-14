"""Общие помощники для тестов `GraphExecutor` (A2.1, docs/build-plan/
13-srez-03-graph-planner-executor.md). Та же причина фикстур-фабрик вместо
импортов, что в `tests/planner/conftest.py` — `tests/executor/` не пакет."""
from __future__ import annotations

import pytest

from finengine.contracts import PlanPolicy
from finengine.skills.spec.atom_registry import AtomDef, AtomRegistry
from finengine.skills.spec.graph_planner import GraphPlanner
from finengine.skills.spec.target import Target


def _make_atom(
    name: str, produces: tuple[str, ...], *, requires: tuple[str, ...] = (),
    mandatory_gates: tuple[str, ...] = (), sandbox_profile: str = "compute_no_network",
    executor=None, **kwargs,
) -> AtomDef:
    """См. `tests/planner/conftest.py::_make_atom` — голая строка в produces/
    requires нормализуется с `concept=None` (`AtomDef.__post_init__`); тесты
    с несколькими РАЗНЫМИ концептами на одном batch-атоме передают настоящие
    `OutputSpec`/`InputSpec(..., concept=...)` явно."""
    return AtomDef(
        name=name, version=kwargs.pop("version", "1.0.0"), produces=produces, requires=requires,
        mandatory_gates=mandatory_gates, sandbox_profile=sandbox_profile,
        execution_kind=kwargs.pop("execution_kind", "deterministic"),
        cache_policy=kwargs.pop("cache_policy", "run_scoped"), cost_class=kwargs.pop("cost_class", "free"),
        executor=executor or (lambda *_a, **_k: ()), **kwargs,
    )


def _make_target(concept: str, *, entity_ref: str = "e1", period_ref: str = "p1", **kwargs) -> Target:
    return Target(output_type=concept, entity_ref=entity_ref, period_ref=period_ref, **kwargs)


@pytest.fixture
def atom():
    return _make_atom


@pytest.fixture
def target():
    return _make_target


@pytest.fixture
def policy() -> PlanPolicy:
    return PlanPolicy(policy_version="test-policy-1")


@pytest.fixture
def build_plan(policy):
    """`AtomRegistry + TargetNode → (PlanBuildResult, node_args)` — тесты
    исполнителя строят граф РЕАЛЬНЫМ `GraphPlanner`, не пишут `PlanGraph`
    вручную: иначе тест исполнителя проверял бы исполнитель против графа,
    которого планировщик никогда бы не построил."""
    def _build(registry: AtomRegistry, goal: TargetNode, **kwargs):
        return GraphPlanner(registry=registry, policy=policy).plan_ex(goal, **kwargs)
    return _build
