"""Retrofit v9 — общий migration gate (`RETROFIT-REPORT-v9.md`,
`docs/build-plan/09-retrofit-realizovannyh-srezov-01-05a.md`, раздел «Общий
migration gate»): «архитектурный grep/test: no runtime `TargetNode`; no
`concept.infer`/`formula.infer` in `AtomRegistry`; no optional requires; no
first-registered-producer fallback».

Один файл — по одному тесту на каждый из четырёх пунктов, чтобы падение
называло РОВНО тот пункт, который нарушен, а не общий «что-то не так»."""
from __future__ import annotations

import dataclasses
import inspect

from finengine.contracts import PlanPolicy
from finengine.skills.atoms.graph_atoms import build_registry
from finengine.skills.spec import feasibility as feasibility_module
from finengine.skills.spec import graph_planner as graph_planner_module
from finengine.skills.spec.atom_registry import AtomDef


def test_no_runtime_target_node_in_the_planner_or_feasibility_modules():
    """`TargetNode` — DTO «показатель, упомянутый в тексте запроса»
    (`GoalIR.targets`, Срез 4), НЕ исполняемая planning-семантика
    `GraphPlanner`/`intent.contract`. Ни один из двух модулей не должен
    ИМПОРТИРОВАТЬ его или принимать/возвращать как тип — они работают с
    `Target` (`skills/spec/target.py`). Docstring'и, объясняющие ЭТОТ САМЫЙ
    retrofit, законно упоминают имя `TargetNode` в прозе — проверяем код
    (сигнатуры/аннотации через `inspect.signature`), не текст комментариев."""
    assert "TargetNode" not in dir(graph_planner_module)
    assert "TargetNode" not in dir(feasibility_module)
    plan_params = inspect.signature(graph_planner_module.GraphPlanner.plan).parameters
    assert "TargetNode" not in str(plan_params["root"].annotation)
    check_params = inspect.signature(feasibility_module.check_feasibility).parameters
    assert "TargetNode" not in str(check_params["goal"].annotation)


def test_no_concept_infer_or_formula_infer_atoms_in_the_registry():
    """R4: ad-hoc concept/formula spec создаёт semantic-слой (`TargetFactory`
    / `root_from_goal`), не отдельные atoms — `concept.infer`/`formula.infer`
    не должны попасть в `AtomRegistry` ни для одной ячейки продукта."""
    registry = build_registry()
    for name in ("concept.infer", "formula.infer"):
        assert registry.get(name) is None, f"{name} не должен существовать в AtomRegistry"


def test_atom_def_contract_carries_no_optional_requires_field():
    field_names = {f.name for f in dataclasses.fields(AtomDef)}
    assert "optional_requires" not in field_names
    assert "dynamic_optional_requires" not in field_names


def test_producers_of_never_falls_back_to_registration_order():
    """Поведенческая версия проверки (не текстовый grep по исходнику,
    который путает единственно-возможный `candidates[0]` при ОДНОМ
    кандидате с настоящим fallback при нескольких): реестр с ДВУМЯ
    равностоящими кандидатами и БЕЗ policy-приоритета обязан дать
    `ChoicePoint` независимо от порядка регистрации — если бы резолвер
    когда-либо брал «первого по списку», перестановка проявила бы это
    сменой имени в `graph.root`, а не одинаковым `ChoicePoint`."""
    from finengine.skills.spec.atom_registry import AtomDef, AtomRegistry
    from finengine.skills.spec.graph_planner import GraphPlanner
    from finengine.skills.spec.target import OutputSpec, Target

    def atom(name, produces, **kwargs):
        return AtomDef(
            name=name, version="1.0.0", produces=produces,
            sandbox_profile="compute_no_network", execution_kind="deterministic",
            cache_policy="run_scoped", executor=lambda *_a, **_k: [], **kwargs,
        )

    policy = PlanPolicy(policy_version="slice14-migration-gate")

    def _registry(order):
        return AtomRegistry([atom(name, (OutputSpec(output_type="CET1", concept="CET1"),), cost_class="free")
                              for name in order])

    target = Target(output_type="CET1", concept_id="CET1", entity_ref="e1", period_ref="p1")
    forward = GraphPlanner(registry=_registry(["a", "b"]), policy=policy).plan(target)
    backward = GraphPlanner(registry=_registry(["b", "a"]), policy=policy).plan(target)

    assert forward.feasibility.feasible is False
    assert backward.feasibility.feasible is False
    assert forward.graph.root.atom_name == backward.graph.root.atom_name == "unresolved.target"
    assert set(forward.choice_points[0].candidate_atoms) == set(backward.choice_points[0].candidate_atoms) == {"a", "b"}
