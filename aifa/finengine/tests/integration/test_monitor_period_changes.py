"""Карточка 32 (T2/T3/T6): `PeriodChangeReport` собирается настоящим `GraphPlanner`
и исполняется настоящим `GraphExecutor`; единственные подмены — модельные границы
(`locate`/`read.value`/`assess.judgment`/`explain.internal`/`explain.external`/
`out.narrative`), как в `test_graph_analysis_narrative.py`.

Критично проверяет то, что юнит-тесты атома (ручной `ctx`) не могут: что `out.changes`
с его `requires` + `dynamic_collection_scope` складывается в ДОСТИЖИМЫЙ граф, а пары
периодов реально приходят от `ref.prior`, а не из моей имитации.
"""
from __future__ import annotations

from finengine.contracts import NarrativeOutput, NumericClaimRecord, PeriodChangeReport, ScalarFact
from finengine.skills.atoms.graph_atoms import build_registry, default_policy
from finengine.skills.spec.graph_executor import GraphExecutor, RunInputs
from finengine.skills.spec.graph_planner import GraphPlanner
from finengine.skills.spec.target import Target
from tests.integration.test_graph_analysis_narrative import (
    _fake_explanation_batch,
    _fake_external_executor,
    _fake_judgment_batch,
    _fake_narrative_executor,
)

# Пара периодов, которую детектор `_select_run_periods` возвращает для `severstal_text`
# (`cls-023`): проверено на стенде — подписи фактов обязаны совпасть с ней, иначе
# `ref.prior` честно не найдёт сравнение.
CURRENT, PRIOR = "31.12.2024", "31.12.2023"
VALUES = {"SALES_REVENUE": (1500.0, 1200.0), "NET_INCOME": (90.0, 120.0)}
# Живой прогон 25.09.2026: интервальные факты подписаны `FY …`, а детектор называет период
# прогона датой. NET_INCOME несёт такие подписи, чтобы сквозной граф проходил и путь
# «пара по значениям самого показателя» (`ref.prior`), не только путь «пара прогона».
LABELS = {"NET_INCOME": ("FY 2025", "FY 2024")}
ROOT_EXTRA = {
    "requested_concepts": "INTEREST_PAID,NET_INCOME,SALES_REVENUE",
    "external_context": "off",
    "structures": "off",
    "role": "other",
}


def _two_period_read_value(items, ctx):  # noqa: ARG001
    out: dict[str, tuple] = {}
    for args in items:
        pair = VALUES.get(args.concept or "")
        periods = LABELS.get(args.concept or "", (CURRENT, PRIOR))
        out[args.concept] = () if pair is None else tuple(
            ScalarFact(
                produced_by_atom="read.value", produced_by_atom_version="1.0.0",
                source_ref=f"fake-read-value:{args.concept}:{period}", concept=args.concept,
                period_ref=period, value=value, unit="RUB_MLN",
            )
            for period, value in zip(periods, pair)
        )
    return out


def _registry(locate_exec, make_structure, make_entities, *, narrative=_fake_narrative_executor):
    return build_registry(
        profile_id="ifrs_nf_ru", locate_executor=locate_exec, read_value_executor=_two_period_read_value,
        structure_executor=make_structure({}), entities_executor=make_entities([]),
        judgment_executor=_fake_judgment_batch, explanation_executor=_fake_explanation_batch,
        external_executor=_fake_external_executor, narrative_executor=narrative,
    )


def _plan_and_run(registry, document_text):
    """Двухпроходное раскрытие (A1.5): паспорт рождается первым проходом."""
    planner = GraphPlanner(registry=registry, policy=default_policy("ifrs_nf_ru"))
    executor = GraphExecutor(registry=registry)
    goal = Target(output_type="PeriodChangeReport", entity_ref="severstal")
    inputs = RunInputs(document_text=document_text, role="other")

    first, first_args = planner.plan_ex(goal, root_extra=ROOT_EXTRA)
    assert first.pending_run_passport
    first_exec = executor.run(first.graph, first_args, run_id="run-changes", inputs=inputs)
    classify = next(n for n in first.graph.nodes if n.node_key.atom_name == "identify.classify")
    passport = first_exec.evidence_by_key[classify.node_key][0]

    second, second_args = planner.plan_ex(goal, run_passport=passport, root_extra=ROOT_EXTRA)
    assert second.feasibility.feasible, second.feasibility.reason
    second_exec = executor.run(
        second.graph, second_args, run_id="run-changes", run_passport=passport, inputs=inputs,
        prior_node_runs=first_exec.node_runs, prior_evidence=first_exec.evidence_by_key,
    )
    return second, second_exec


def _report(graph, execution) -> PeriodChangeReport:
    reports = [v for v in execution.evidence_by_key[graph.graph.root] if isinstance(v, PeriodChangeReport)]
    assert len(reports) == 1, "корень обязан дать ровно один PeriodChangeReport"
    return reports[0]


def _row(report, concept):
    return next(row for row in report.rows if row.concept == concept)


def test_the_root_resolves_and_the_percentages_come_from_the_period_pair_of_ref_prior(
    severstal_text, make_fake_locate_and_read_value_executors, make_fake_structure_executor,
    make_fake_entities_executor,
):
    locate_exec, _ = make_fake_locate_and_read_value_executors({k: 1.0 for k in VALUES})
    registry = _registry(locate_exec, make_fake_structure_executor, make_fake_entities_executor)

    graph, execution = _plan_and_run(registry, severstal_text)

    atoms = {node.node_key.atom_name for node in graph.graph.nodes}
    assert {"analyze.full_bundle", "ref.prior", "assess.judgment", "explain.internal",
            "out.narrative", "verify.grounding", "out.changes"} <= atoms
    assert "explain.external" not in atoms, "объяснение — по данным документа, без веб-поиска"
    assert not {"read.structure", "read.entities"} & atoms, (
        "сегменты и состав группы мониторингу не нужны и не должны ронять отчёт"
    )
    assert not {"watch.diff", "calc.series", "monitor.compose"} & atoms, "снимки субъекта не нужны"
    assert graph.graph.root.atom_name == "out.changes"
    assert execution.succeeded, execution.node_runs[graph.graph.root].error

    report = _report(graph, execution)
    revenue, income = _row(report, "SALES_REVENUE"), _row(report, "NET_INCOME")
    assert (revenue.status, revenue.relative_change_pct, revenue.absolute_change) == ("computed", 25.0, 300.0)
    assert (income.status, income.relative_change_pct, income.absolute_change) == ("computed", -25.0, -30.0)
    assert (revenue.current_period_ref, revenue.prior_period_ref) == (CURRENT, PRIOR)
    assert (income.current_period_ref, income.prior_period_ref) == ("FY 2025", "FY 2024"), (
        "пара строится по подписям периодов самого показателя, а не только по паре прогона"
    )
    # Равные |Δ %| упорядочены по коду показателя; отсутствующий INTEREST_PAID — не строка.
    assert [row.concept for row in report.rows] == ["NET_INCOME", "SALES_REVENUE"]
    assert [gap.code for gap in report.gaps] == ["concepts_not_found"]
    assert "1 из 3" in report.gaps[0].message
    assert report.explanation_status == "ready" and report.status == "complete"


def test_the_structures_switch_removes_only_segments_and_group_from_the_bundle_scope():
    """`structures=off` (MONITOR_CHANGES) убирает ветви сегментов и состава группы; без флага
    ролевой полный анализ по-прежнему их содержит; показатели не затронуты."""
    from finengine.skills.atoms.graph_atoms import _analyze_full_bundle_scope
    from finengine.skills.atoms.run_passport import build_run_passport
    from finengine.skills.spec.atom_registry import NodeArgs

    passport = build_run_passport("IFRS annual report", goal_ir_id="goal-structures")
    base = {"role": "other", "requested_concepts": "SALES_REVENUE,NET_INCOME"}

    with_structures = _analyze_full_bundle_scope(NodeArgs(extra=base), passport)
    without = _analyze_full_bundle_scope(NodeArgs(extra={**base, "structures": "off"}), passport)

    kinds = lambda specs: {spec.input_type for spec in specs}  # noqa: E731
    assert {"ExtractedStructure", "EntityRelation"} <= kinds(with_structures)
    assert kinds(without) == {"MetricEvidence"}
    assert [s for s in with_structures if s.input_type == "MetricEvidence"] == list(without)


def test_a_fabricated_number_blocks_the_text_but_not_the_table(
    severstal_text, make_fake_locate_and_read_value_executors, make_fake_structure_executor,
    make_fake_entities_executor,
):
    def fabricating_narrative(args, ctx):  # noqa: ARG001
        claim = NumericClaimRecord(text="выручка выросла на 40%", kind="financial", value=0.40, evidence_id=None)
        return [NarrativeOutput(run_id=ctx.run_id, text="Выручка выросла на 40%.", claims=[claim])]

    locate_exec, _ = make_fake_locate_and_read_value_executors({k: 1.0 for k in VALUES})
    registry = _registry(
        locate_exec, make_fake_structure_executor, make_fake_entities_executor,
        narrative=fabricating_narrative,
    )

    graph, execution = _plan_and_run(registry, severstal_text)

    assert execution.succeeded, "заблокированный текст — не отказ всего графа"
    report = _report(graph, execution)
    assert report.explanation is None and report.explanation_status == "unavailable"
    assert "заблокировано проверкой опоры" in report.explanation_note
    assert report.status == "partial"
    assert _row(report, "SALES_REVENUE").relative_change_pct == 25.0, "цифры таблицы не зависят от текста модели"


def test_a_model_that_writes_nothing_leaves_the_table_and_says_so(
    severstal_text, make_fake_locate_and_read_value_executors, make_fake_structure_executor,
    make_fake_entities_executor,
):
    locate_exec, _ = make_fake_locate_and_read_value_executors({k: 1.0 for k in VALUES})
    registry = _registry(
        locate_exec, make_fake_structure_executor, make_fake_entities_executor,
        narrative=lambda args, ctx: [],  # noqa: ARG005
    )

    graph, execution = _plan_and_run(registry, severstal_text)

    report = _report(graph, execution)
    assert report.explanation_status == "unavailable" and "не сформировано" in report.explanation_note
    assert _row(report, "NET_INCOME").relative_change_pct == -25.0
