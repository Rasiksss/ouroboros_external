"""`intent.contract` — критерий готовности атома (docs/build-plan/
13-srez-03-graph-planner-executor.md, раздел «Атом, вводимый здесь»):
отсутствующий вход даёт `needs_input`, цикл выявляется до исполнения, два
производителя создают неоднозначность, точечный запрос не разворачивает
полный анализ."""
from __future__ import annotations

from finengine.contracts import PlanPolicy
from finengine.skills.spec.atom_registry import AtomDef, AtomRegistry
from finengine.skills.spec.feasibility import check_feasibility
from finengine.skills.spec.target import Target


def _atom(name, produces, *, requires=(), **kwargs) -> AtomDef:
    return AtomDef(
        name=name, version="1.0.0", produces=produces, requires=requires,
        sandbox_profile=kwargs.pop("sandbox_profile", "compute_no_network"),
        execution_kind=kwargs.pop("execution_kind", "deterministic"),
        cache_policy="run_scoped", cost_class="free", executor=lambda *_: (), **kwargs,
    )


def _target(concept: str, **kwargs) -> Target:
    return Target(output_type=concept, entity_ref="e1", period_ref="p1", **kwargs)


_POLICY = PlanPolicy(policy_version="test-1")


def test_missing_producer_yields_needs_input():
    registry = AtomRegistry([_atom("ingest.document", ("DOC",))])
    result = check_feasibility(_target("SALES_REVENUE"), registry=registry, policy=_POLICY)

    assert not result.feasible
    assert (result.reason or "").startswith("needs_input:")
    assert "SALES_REVENUE" in result.reason


def test_cycle_is_caught_before_any_atom_would_run():
    registry = AtomRegistry([
        _atom("calc.a", ("A",), requires=("B",)),
        _atom("calc.b", ("B",), requires=("A",)),
    ])
    result = check_feasibility(_target("A"), registry=registry, policy=_POLICY)

    assert not result.feasible
    assert (result.reason or "").startswith("cycle:")


def test_two_producers_yield_ambiguous_producer():
    registry = AtomRegistry([
        _atom("extract.scalar", ("CET1",)),
        _atom("extract.capital_prompt", ("CET1",)),
    ])
    result = check_feasibility(_target("CET1"), registry=registry, policy=_POLICY)

    assert not result.feasible
    assert (result.reason or "").startswith("ambiguous_producer:")


def test_point_request_does_not_require_the_full_registry():
    """Точечный запрос по одному показателю не должен считаться недостижимым
    из-за отсутствия производителей для показателей, которых НИКТО не
    запрашивал — `intent.contract` смотрит на requires ОДНОЙ цели, не на
    полноту всего реестра."""
    registry = AtomRegistry([
        _atom("ingest.document", ("DOC",)),
        _atom("extract.scalar", ("SALES_REVENUE",), requires=("DOC",)),
        # UNRELATED_METRIC намеренно без производителя — не должно влиять.
    ])
    result = check_feasibility(_target("SALES_REVENUE"), registry=registry, policy=_POLICY)

    assert result.feasible


def test_optional_dependency_does_not_block_feasibility():
    """Карточка атома: «Необязательная зависимость не делает цель
    недостижимой». Retrofit v9 (R3, A1.7): `collection_scope`, не
    `optional_requires` — недостижимая ветка раскрывается изолированным
    пробным проходом и уходит в `incomplete_scope`, не в `unreachable`."""
    registry = AtomRegistry([
        _atom("ingest.document", ("DOC",)),
        _atom(
            "extract.scalar", ("REVENUE",), requires=("DOC",),
            collection_scope=("EXTERNAL_CONTEXT",),
        ),
        # EXTERNAL_CONTEXT — без производителя, но она необязательная.
    ])
    result = check_feasibility(_target("REVENUE"), registry=registry, policy=_POLICY)

    assert result.feasible


def test_result_is_the_same_across_repeated_calls_with_the_same_inputs():
    """Критерий готовности: «результат одинаков для одинаковых входов и
    версий политик»."""
    registry = AtomRegistry([
        _atom("ingest.document", ("DOC",)),
        _atom("extract.scalar", ("REVENUE",), requires=("DOC",)),
    ])
    results = [check_feasibility(_target("REVENUE"), registry=registry, policy=_POLICY) for _ in range(20)]
    assert all(r == results[0] for r in results)
