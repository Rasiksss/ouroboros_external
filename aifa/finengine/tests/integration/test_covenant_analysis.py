"""Срез 9 (B10) — `read.condition → ref.norm/ref.plan → assess.rule` через
настоящий `PlanGraph`+`GraphExecutor`, композицией уже существующих атомов
(`read.value`/`locate` для факта, `verify.citation` как обязательный гейт
`read.condition`), БЕЗ специального «оркестратора ковенантного анализа»
(брифинг сессии: «covenant analysis should be composition of existing
read/normalize/calc/ref/assess atoms, not special end-to-end orchestrator»).

**Показатель — `SALES_REVENUE`** (реальный, чисто извлекаемый концепт
реестра `ifrs_nf_ru`, тот же, что уже использует `test_graph_analysis_
narrative.py`) — выбран НЕ произвольно: показатель, который в этом реестре
одновременно и расчётный, и извлекаемый (например `RATIO_NET_DEBT_EBITDA`),
дал бы `MetricEvidence` через `calc.formula`, а не через подставной `read.
value` этого теста, — сценарий проверял бы не то, что заявлено.

**Provenance chain, проверяемая end-to-end** (брифинг: «финальный отчёт
отдельно покажи provenance chain: ConditionEvidence/ReferenceValue ->
comparable MetricEvidence -> RuleComparison -> later judgment boundary»):
`ConditionEvidence` (`read.condition`, за `verify.citation`) → `ReferenceValue`
(`ref.norm`) → сопоставимая `MetricEvidence` (`read.value`, здесь — детерминированный
стенд-ин `make_fake_locate_and_read_value_executors`, тот же приём, что
`test_graph_analysis_narrative.py`) → `RuleComparison` (`assess.rule`) →
граница `assess.judgment` (смысловое суждение — Срез 6b, вне периметра этой
сессии: `RuleComparison` намеренно останавливается на формальном результате).

Единственная подменённая модельная граница — `two_stage_extractor.
_call_json_llm`, тот же приём, что `tests/atoms/test_read_condition.py`
(реальный `read_condition_batch` со своей настоящей проверкой цитаты
остаётся неизменным, не заменяется на кастомный `condition_executor`)."""
from __future__ import annotations

import json

import pytest

from finengine.contracts import ExtractedMetric, ReferenceValue, RuleComparison, VerificationFinding
from finengine.skills import two_stage_extractor
from finengine.skills.atoms.graph_atoms import build_registry, default_policy
from finengine.skills.spec.graph_executor import GraphExecutor, RunInputs
from finengine.skills.spec.graph_planner import GraphPlanner
from finengine.skills.spec.target import Target

_CONCEPT = "SALES_REVENUE"
_CLAUSE_TEXT = "The Borrower shall maintain Sales Revenue of at least 1000 (RUB mln), tested annually."
_CONTRACT_TEXT = f"===PAGE 1===\nLoan Agreement between Lender and the Borrower.\n===PAGE 5===\n{_CLAUSE_TEXT}\n"


def _patch_condition_llm(monkeypatch):
    # "млн. руб." — уже КАНОНИЧЕСКАЯ форма единицы (та же, в которую `norm.
    # unit` приводит сырой "RUB_MLN" факта read.value, `value_parsing.
    # _normalise_money_value`): `read.condition` не проходит через `norm.
    # unit` сам (гейт привязан только к `read.value`), поэтому единица
    # условия сравнивается с УЖЕ нормализованной единицей факта как есть —
    # намеренный выбор фикстуры, не обход проверки.
    response = {"conditions": {_CONCEPT: [{
        "page": 5, "quote": _CLAUSE_TEXT, "operator_text": "at least", "threshold_text": "1000",
        "unit_text": "млн. руб.", "subject": "the Borrower",
    }]}}

    async def _fake(**kwargs):  # noqa: ARG001
        return response, {}, json.dumps(response)

    monkeypatch.setattr(two_stage_extractor, "_call_json_llm", _fake)


def _run(registry, target):
    policy = default_policy("ifrs_nf_ru")
    planner = GraphPlanner(registry=registry, policy=policy)
    executor = GraphExecutor(registry=registry)
    inputs = RunInputs(document_text=_CONTRACT_TEXT)

    first, first_args = planner.plan_ex(target, root_extra={})
    assert first.pending_run_passport
    first_exec = executor.run(first.graph, first_args, run_id="run-1", inputs=inputs)
    classify_node = next(n for n in first.graph.nodes if n.node_key.atom_name == "identify.classify")
    passport = first_exec.evidence_by_key[classify_node.node_key][0]

    second, second_args = planner.plan_ex(target, run_passport=passport, root_extra={})
    assert second.feasibility.feasible, second.feasibility.reason
    second_exec = executor.run(
        second.graph, second_args, run_id="run-1", run_passport=passport, inputs=inputs,
        prior_node_runs=first_exec.node_runs, prior_evidence=first_exec.evidence_by_key,
    )
    return second, second_exec


def test_covenant_compliance_flows_through_the_real_planner_and_executor(
    monkeypatch, make_fake_locate_and_read_value_executors,
):
    _patch_condition_llm(monkeypatch)
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors({_CONCEPT: 1200.0})
    registry = build_registry(
        profile_id="ifrs_nf_ru", locate_executor=locate_exec, read_value_executor=read_value_exec,
    )
    target = Target(output_type="RuleComparison", concept_id=_CONCEPT, entity_ref="doc-1", period_ref="FY2025")

    result, execution = _run(registry, target)

    assert execution.succeeded, execution.node_runs[result.graph.root].error
    node_names = {n.node_key.atom_name for n in result.graph.nodes}
    assert {"read.condition", "verify.citation", "ref.norm", "assess.rule", "read.value", "locate"} <= node_names

    root_evidence = execution.evidence_by_key[result.graph.root]
    comparisons = [e for e in root_evidence if isinstance(e, RuleComparison)]
    assert len(comparisons) == 1
    rc = comparisons[0]
    assert rc.operator == ">="
    assert rc.actual_value == 1200.0
    assert rc.threshold_value == 1000.0
    assert rc.passed is True
    assert rc.delta == pytest.approx(200.0)
    assert rc.headroom == pytest.approx(200.0)

    # Provenance chain: RuleComparison -> ReferenceValue(norm) -> ConditionEvidence -> verify.citation(pass).
    all_evidence = [e for tup in execution.evidence_by_key.values() for e in tup]
    reference = next(
        e for e in all_evidence if isinstance(e, ReferenceValue) and e.reference_id == rc.threshold_reference_id
    )
    assert reference.source_kind == "norm"
    condition = next(
        e for e in all_evidence if getattr(e, "evidence_id", None) == reference.source_evidence_id
    )
    assert condition.condition_text == _CLAUSE_TEXT
    citation_finding = next(
        e for e in all_evidence
        if isinstance(e, VerificationFinding) and e.check_kind == "citation" and e.source_ref == condition.evidence_id
    )
    assert citation_finding.status == "pass"
    # `read.value` проходит цепочку гейтов (`norm.unit → norm.time → verify.
    # bounds`), каждый из которых может пересоздать `ExtractedMetric` с новым
    # `evidence_id` (`supersedes` на предыдущий) — `assess.rule` обязан
    # трассироваться к ПОСЛЕДНЕЙ версии, которую он реально использовал, не к
    # любой копии того же концепта.
    actual_metric = next(
        e for e in all_evidence
        if isinstance(e, ExtractedMetric) and e.scalar_fact.evidence_id == rc.actual_evidence_id
    )
    assert actual_metric.concept == _CONCEPT


def test_covenant_breach_is_reported_mechanically_without_a_verdict_label(
    monkeypatch, make_fake_locate_and_read_value_executors,
):
    _patch_condition_llm(monkeypatch)
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors({_CONCEPT: 800.0})
    registry = build_registry(
        profile_id="ifrs_nf_ru", locate_executor=locate_exec, read_value_executor=read_value_exec,
    )
    target = Target(output_type="RuleComparison", concept_id=_CONCEPT, entity_ref="doc-1", period_ref="FY2025")

    result, execution = _run(registry, target)

    assert execution.succeeded, execution.node_runs[result.graph.root].error
    comparisons = [e for e in execution.evidence_by_key[result.graph.root] if isinstance(e, RuleComparison)]
    assert len(comparisons) == 1
    rc = comparisons[0]
    assert rc.passed is False
    assert rc.headroom == pytest.approx(-200.0)
    # Формально сравнение — не вердикт: контракт не несёт качественного ярлыка.
    assert "risk" not in RuleComparison.model_fields and "verdict" not in RuleComparison.model_fields
