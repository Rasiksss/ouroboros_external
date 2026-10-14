"""Retrofit v9 (R3, A1.7 «Optionality»): `AtomDef` не несёт `optional_requires`
как базовую семантику — поле удалено из контракта целиком. Каждый элемент
`requires` обязателен: его недостижимость всегда даёт `Unreachable`/
`feasible=False`, без исключений. Бизнес-необязательность (полный анализ,
форма роли) выражается ОТДЕЛЬНЫМ механизмом — `collection_scope` — не
параметром `optional=` на обычном пути `requires`."""
from __future__ import annotations

import dataclasses

import pytest

from finengine.skills.spec.atom_registry import AtomDef, AtomRegistry
from finengine.skills.spec.graph_planner import GraphPlanner


def test_atom_def_has_no_optional_requires_field():
    """Архитектурная проверка (migration gate): `optional_requires`/
    `dynamic_optional_requires` не существуют как имена полей контракта —
    их не с чем спутать с `requires`, они просто отсутствуют."""
    field_names = {f.name for f in dataclasses.fields(AtomDef)}
    assert "optional_requires" not in field_names
    assert "dynamic_optional_requires" not in field_names
    # Замена — collection_scope/dynamic_collection_scope, структурно ОТДЕЛЬНАЯ
    # от requires/dynamic_requires, не флаг на них.
    assert "collection_scope" in field_names
    assert "dynamic_collection_scope" in field_names


def test_missing_required_input_always_blocks_feasibility(atom, target, policy):
    """Каждый `requires` обязателен без исключений — недостижимый вход
    портит `feasible`, независимо от того, насколько глубоко он лежит."""
    registry = AtomRegistry([
        atom("ingest.document", ("DOC",)),
        atom("extract.scalar", ("REVENUE",), requires=("DOC", "EXTERNAL_CONTEXT")),
        # EXTERNAL_CONTEXT без производителя — но здесь она в requires, не в
        # collection_scope, значит она ОБЯЗАТЕЛЬНА.
    ])
    result = GraphPlanner(registry=registry, policy=policy).plan(target("REVENUE"))

    assert not result.feasibility.feasible
    assert any(u.concept == "EXTERNAL_CONTEXT" for u in result.unreachable)


def test_collection_scope_failure_does_not_block_feasibility_but_requires_failure_does(atom, target, policy):
    """Прямое сравнение одного и того же недостижимого концепта в двух
    ролях: в `requires` он валит план, в `collection_scope` — нет."""
    required_registry = AtomRegistry([
        atom("ingest.document", ("DOC",)),
        atom("extract.scalar", ("REVENUE",), requires=("DOC", "MISSING")),
    ])
    scoped_registry = AtomRegistry([
        atom("ingest.document", ("DOC",)),
        atom("extract.scalar", ("REVENUE",), requires=("DOC",), collection_scope=("MISSING",)),
    ])

    required_result = GraphPlanner(registry=required_registry, policy=policy).plan(target("REVENUE"))
    scoped_result = GraphPlanner(registry=scoped_registry, policy=policy).plan(target("REVENUE"))

    assert not required_result.feasibility.feasible
    assert scoped_result.feasibility.feasible
    assert len(scoped_result.incomplete_scope) == 1
    assert scoped_result.incomplete_scope[0].concept == "MISSING"


@pytest.mark.parametrize("bad_kwarg", ["optional_requires", "dynamic_optional_requires"])
def test_atom_def_rejects_the_old_optional_requires_kwarg(bad_kwarg):
    with pytest.raises(TypeError):
        AtomDef(
            name="x", version="1.0.0", produces=("X",), sandbox_profile="compute_no_network",
            execution_kind="deterministic", cache_policy="run_scoped", cost_class="free",
            executor=lambda *_a, **_k: (), **{bad_kwarg: ()},
        )
