"""Общие помощники для тестов `GraphPlanner` (A1.10, docs/build-plan/
13-srez-03-graph-planner-executor.md).

Как фикстуры-фабрики, а не импортируемые функции: `tests/planner/` — не пакет
(в отличие от верхнего `tests/__init__.py`, у подпапок его нет, тем же путём,
которым это уже сделано у `tests/atoms/`), и `import tests.planner.conftest`
здесь не сработает — `conftest.py` подключается pytest автоматически, фикстуры
этого файла видны каждому тесту `tests/planner/*.py` без единого импорта.

Тестовые атомы здесь НЕ исполняются ни в одном тесте этого каталога — у всех
`executor=_noop`. `tests/planner/` проверяет ПОСТРОЕНИЕ графа (чистую функцию
`TargetNode + AtomRegistry + PlanPolicy → PlanGraph`), не выполнение; за
исполнение отвечает `tests/executor/` (тот же срез, отдельные файлы по A2.1)."""
from __future__ import annotations

import pytest

from finengine.contracts import PlanPolicy
from finengine.skills.spec.atom_registry import AtomDef
from finengine.skills.spec.target import Target


def _noop(*_args, **_kwargs):
    raise AssertionError("тестовый атом не должен исполняться в tests/planner — только строиться")


def _make_atom(
    name: str, produces: tuple[str, ...], *, requires: tuple[str, ...] = (),
    collection_scope: tuple[str, ...] = (), mandatory_gates: tuple[str, ...] = (),
    sandbox_profile: str = "compute_no_network", **kwargs,
) -> AtomDef:
    """Retrofit v9 (R3): голые строки в `produces`/`requires`/`collection_scope`
    по-прежнему приняты — `AtomDef.__post_init__` нормализует их в
    `OutputSpec(output_type=строка, concept=None)`/`InputSpec(input_type=
    строка, concept=None)` (`atom_registry._norm_output_specs`/
    `_norm_input_specs`) — строка целиком играет роль идентичности узла
    (`output_type`), концепта у неё нет, как раньше не было отдельного
    измерения у голого `TargetNode.concept`. Тестам, которым НУЖНО различение
    output_type/concept (несколько разных концептов на одном batch-атоме —
    см. `tests/executor/test_batch_coalescing.py` и соседей), нужно передавать
    настоящие `OutputSpec`/`InputSpec(..., concept=...)` явно, не полагаться
    на эту нормализацию."""
    return AtomDef(
        name=name, version=kwargs.pop("version", "1.0.0"), produces=produces, requires=requires,
        collection_scope=collection_scope, mandatory_gates=mandatory_gates,
        sandbox_profile=sandbox_profile, execution_kind=kwargs.pop("execution_kind", "deterministic"),
        cache_policy=kwargs.pop("cache_policy", "run_scoped"), cost_class=kwargs.pop("cost_class", "free"),
        executor=kwargs.pop("executor", _noop), **kwargs,
    )


def _make_target(concept: str, *, entity_ref: str = "e1", period_ref: str = "p1", **kwargs) -> Target:
    """`concept` играет роль `Target.output_type` целиком (см. `_make_atom`).
    Тесты, которым нужно различение output_type/concept_id по отдельности,
    строят `Target(...)` напрямую с явным `concept_id=`."""
    return Target(output_type=concept, entity_ref=entity_ref, period_ref=period_ref, **kwargs)


@pytest.fixture
def atom():
    """Короткий конструктор `AtomDef`: наполняет обязательные по A1.1 поля
    разумными умолчаниями, чтобы каждый тест называл только то, что ему
    действительно важно."""
    return _make_atom


@pytest.fixture
def target():
    return _make_target


@pytest.fixture
def policy() -> PlanPolicy:
    return PlanPolicy(policy_version="test-policy-1")
