"""Срез 31, П4: ad-hoc формула может опираться на РАСЧЁТНЫЙ показатель реестра.

Живая проверка (M2, dev, РСБУ ЛУКОЙЛ): «посчитай долю краткосрочных обязательств в
общей сумме обязательств» → модель составила `SHORT / TOTAL * 100`, где TOTAL —
«Итого обязательства» = `TOTAL_LIABILITIES` (в реестре РСБУ он расчётный:
`TOTAL_ASSETS - TOTAL_EQUITY`, оба входа извлекаемые). План отказывал:
«цикл зависимостей на узле calc.formula(concept=TOTAL_LIABILITIES)».

Причина: ad-hoc-ключи (`ad_hoc_formula_expression`, …) лежат в `extra` КОРНЯ, а `extra`
наследуют дочерние узлы. Дочерний `calc.formula(TOTAL_LIABILITIES)` находил эти ключи,
считал себя той же ad-hoc формулой и требовал `TOTAL_LIABILITIES` у самого себя.
"""
from __future__ import annotations

from finengine.contracts import AdHocConceptSpec, AdHocFormulaSpec, ClassificationResult, EntityIdentity, RunPassport
from finengine.skills.atoms.calc_formula import (
    _ad_hoc_spec_from_extra,
    calc_formula_requirement_extra,
)
from finengine.skills.atoms.graph_atoms import build_registry, default_policy
from finengine.skills.atoms.ad_hoc_concepts import EXTRA_KEY, dump_specs
from finengine.skills.registry_composition import RegistrySelection, compile_registry
from finengine.skills.spec.atom_registry import NodeArgs
from finengine.skills.spec.graph_planner import GraphPlanner
from finengine.skills.spec.target import InputSpec, Target

_TERM = "Доля краткосрочных обязательств в общей сумме обязательств"


def _formula() -> AdHocFormulaSpec:
    return AdHocFormulaSpec(
        term=_TERM, expression="TOTAL_CURRENT_LIABILITIES / TOTAL_LIABILITIES * 100",
        dependencies=["TOTAL_CURRENT_LIABILITIES", "TOTAL_LIABILITIES"], unit="%",
        components=[
            AdHocConceptSpec(term="Итого краткосрочных обязательств", key="TOTAL_CURRENT_LIABILITIES"),
            AdHocConceptSpec(term="Итого обязательства", key="TOTAL_LIABILITIES"),
        ],
    )


def _root_extra(spec: AdHocFormulaSpec) -> dict[str, str]:
    return {
        "ad_hoc_spec_id": spec.spec_id,
        "ad_hoc_formula_expression": spec.expression,
        "ad_hoc_formula_dependencies": ",".join(spec.dependencies),
        "ad_hoc_formula_term": spec.term,
        "ad_hoc_formula_assumptions": spec.assumptions,
        "ad_hoc_formula_unit": spec.unit,
        EXTRA_KEY: dump_specs(spec.components),
    }


def _passport() -> RunPassport:
    classification = ClassificationResult(
        profile_id="rsbu_nf_ru", standard="rsbu", company_type="non_financial", language="ru", reason="fixture",
    )
    return RunPassport(
        goal_ir_id="goal-1", classification=classification,
        entity=EntityIdentity(entity_id="ent-1", canonical_name="Тест", raw_name="Тест"),
        registry_version=compile_registry(RegistrySelection.from_classification(classification)).version,
        methodology_version="m-1", model_version="m", prompt_version="p", sandbox_image_version="s",
    )


def test_the_ad_hoc_root_reads_its_own_formula_but_a_registry_dependency_does_not():
    extra = _root_extra(_formula())

    root = _ad_hoc_spec_from_extra(NodeArgs(concept=_TERM, extra=extra))
    dependency = _ad_hoc_spec_from_extra(NodeArgs(concept="TOTAL_LIABILITIES", extra=extra))

    assert root is not None and root.expression == "TOTAL_CURRENT_LIABILITIES / TOTAL_LIABILITIES * 100"
    assert dependency is None, "унаследованный extra корня не делает дочерний узел той же ad-hoc формулой"


def test_formula_metadata_is_projected_per_requirement():
    extra = _root_extra(_formula())
    root_args = NodeArgs(concept=_TERM, extra=extra)

    registry_input = calc_formula_requirement_extra(
        root_args, InputSpec(input_type="MetricEvidence", concept="TOTAL_LIABILITIES"),
    )
    ad_hoc_input = calc_formula_requirement_extra(
        root_args, InputSpec(input_type="ExtractedMetric", concept="TOTAL_CURRENT_LIABILITIES"),
    )

    assert registry_input == {}, "registered input must be shareable with an ordinary extraction goal"
    assert "ad_hoc_formula_expression" not in ad_hoc_input
    assert "TOTAL_CURRENT_LIABILITIES" in ad_hoc_input[EXTRA_KEY]
    assert "TOTAL_LIABILITIES" not in ad_hoc_input[EXTRA_KEY]


def test_an_ad_hoc_formula_over_a_calculated_registry_metric_is_plannable():
    spec = _formula()
    root = Target(output_type="CalculatedMetric", concept_id=_TERM, entity_ref="ent-1", period_ref=None)
    planner = GraphPlanner(registry=build_registry(), policy=default_policy())

    build, _ = planner.plan_ex(root, run_passport=_passport(), root_extra=_root_extra(spec))

    assert build.feasibility.feasible, build.feasibility.reason
    formulas = {n.node_key.args_fingerprint for n in build.graph.nodes if n.node_key.atom_name == "calc.formula"}
    assert any("TOTAL_LIABILITIES" in fp for fp in formulas), "показатель считается формулой реестра из своих входов"
    assert any(_TERM in fp for fp in formulas), "и поверх него — сама ad-hoc формула"
