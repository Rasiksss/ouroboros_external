"""Сценарий «форма роли» (A11): `out.form` собирает ДГР со всеми строками
роли, включая недоступные (нерушимое решение №8, сквозной приёмочный сценарий
#5 в 99-matrica.md — «форма ДГР возвращает ровно N строк, включая строки без
значения»)."""
from __future__ import annotations

from finengine.roles.scope import layouts
from finengine.skills.atoms.graph_atoms import build_registry, default_policy
from finengine.skills.spec.graph_executor import GraphExecutor, RunInputs
from finengine.skills.spec.graph_planner import GraphPlanner
from finengine.skills.spec.target import Target

_ROW_SPECS = layouts()["roles"]["dgr"]["formats"]["bank"]["rows"]
_METRIC_ROW_SPECS = [row for row in _ROW_SPECS if row.get("enum")]
_AVAILABLE = {row["enum"]: float(100 + i) for i, row in enumerate(_METRIC_ROW_SPECS[:5])}


def test_role_form_keeps_unavailable_rows_instead_of_dropping_them(sberbank_text, make_fake_locate_and_read_value_executors):
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors(_AVAILABLE)
    registry = build_registry(
        profile_id="ifrs_bank_ru", locate_executor=locate_exec, read_value_executor=read_value_exec,
    )
    policy = default_policy("ifrs_bank_ru")
    planner = GraphPlanner(registry=registry, policy=policy)
    goal = Target(output_type="FormOutput", entity_ref="sberbank", period_ref="p-2024-fy")
    inputs = RunInputs(document_text=sberbank_text, role="other")
    executor = GraphExecutor(registry=registry)

    first, first_args = planner.plan_ex(goal, root_extra={"role": "dgr"})
    assert first.pending_run_passport
    first_exec = executor.run(first.graph, first_args, run_id="run-form", inputs=inputs)
    classify_node = next(n for n in first.graph.nodes if n.node_key.atom_name == "identify.classify")
    passport = first_exec.evidence_by_key[classify_node.node_key][0]
    assert passport.classification.company_type == "bank"

    second, second_args = planner.plan_ex(goal, run_passport=passport, root_extra={"role": "dgr"})
    assert second.graph.root == first.graph.root
    assert second.feasibility.feasible, second.feasibility.reason

    second_exec = executor.run(
        second.graph, second_args, run_id="run-form", run_passport=passport, inputs=inputs,
        prior_node_runs=first_exec.node_runs, prior_evidence=first_exec.evidence_by_key,
    )

    assert second_exec.succeeded, second_exec.node_runs[second.graph.root].error
    form = second_exec.evidence_by_key[second.graph.root][0]

    assert form.role == "dgr"
    assert form.layout_id == "dgr.bank"
    assert form.preserve_empty is True
    assert len(_ROW_SPECS) == 309, "приёмочный baseline ДГР обязан оставаться явным"
    assert len(form.rows) == 309, "форма обязана вернуть ВСЕ строки макета, не только enum-строки"
    assert [row.order for row in form.rows] == list(range(309))
    assert [row.label for row in form.rows] == [
        row.get("label") or row.get("enum") or f"Строка {index + 1}"
        for index, row in enumerate(_ROW_SPECS)
    ]

    available_rows = [r for r in form.rows if r.available]
    unavailable_rows = [r for r in form.rows if not r.available]
    assert available_rows, "хотя бы одна строка обязана найтись"
    assert unavailable_rows, "хотя бы одна строка обязана остаться недоступной, а не выброситься"
    for row in unavailable_rows:
        assert row.value is None
    for row in available_rows:
        assert row.value == _AVAILABLE[row.enum]
    static_rows = [row for row in form.rows if row.enum is None]
    assert len(static_rows) == 199
    assert all(row.status == "not_applicable" and row.value is None for row in static_rows)


def test_role_form_target_does_not_appear_as_an_unrelated_full_analysis_bundle(sberbank_text, make_fake_locate_and_read_value_executors):
    """Разные корни — разные узлы (A1.11.2): запрос формы не производит
    попутно `EvidenceBundleV2` и наоборот."""
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors(_AVAILABLE)
    registry = build_registry(profile_id="ifrs_bank_ru", locate_executor=locate_exec, read_value_executor=read_value_exec)
    policy = default_policy("ifrs_bank_ru")
    planner = GraphPlanner(registry=registry, policy=policy)
    goal = Target(output_type="FormOutput", entity_ref="sberbank", period_ref="p-2024-fy")
    inputs = RunInputs(document_text=sberbank_text, role="other")
    executor = GraphExecutor(registry=registry)

    first, first_args = planner.plan_ex(goal, root_extra={"role": "dgr"})
    first_exec = executor.run(first.graph, first_args, run_id="run-form-2", inputs=inputs)
    classify_node = next(n for n in first.graph.nodes if n.node_key.atom_name == "identify.classify")
    passport = first_exec.evidence_by_key[classify_node.node_key][0]
    second, _ = planner.plan_ex(goal, run_passport=passport, root_extra={"role": "dgr"})

    assert not any(n.node_key.atom_name == "analyze.full_bundle" for n in second.graph.nodes)
