"""Сценарий «полный анализ» (B3): корень раскрывается в ПРИМЕНИМЫЙ набор
зависимостей только ПОСЛЕ фиксации `RunPassport` (A1.5, двухпроходное
раскрытие), расчётный показатель проходит обязательные контрольные этапы
Среза 6a (`verify.identity` → `verify.cross` → `norm.time` → `verify.bounds`,
заменившие `verify.arithmetic` в цепочке `calc.formula`) до попадания в
итоговый `EvidenceBundleV2` (A1.7)."""
from __future__ import annotations

from finengine.contracts import DerivedFact, ScalarFact
from finengine.skills.atoms.graph_atoms import build_registry, default_policy
from finengine.skills.spec.graph_executor import GraphExecutor, RunInputs
from finengine.skills.spec.graph_planner import GraphPlanner
from finengine.skills.spec.target import Target

# NET_INTEREST_PAID (расчётный) = INTEREST_PAID - INTEREST_RECEIVED — реальная
# формула ifrs_nf_ru, ТОЛЬКО расчётная (не «двойной природы» — в отличие от
# DIVIDENDS, которая одновременно извлекаемая, и по умолчанию (`default_
# policy`) разрешается в пользу прямого извлечения, минуя `calc.formula`
# вовсе): нужен именно enum, у которого нет альтернативы `calc.formula`,
# чтобы тест реально прошёл через расчёт и гейты, а не только через извлечение.
_FAKE_VALUES = {
    "SALES_REVENUE": 1_000_000.0,
    "NET_INCOME": 120_000.0,
    "INTEREST_PAID": 9_000.0,
    "INTEREST_RECEIVED": 2_000.0,
}


def test_full_analysis_expands_only_after_run_passport_and_gates_derived_facts(
    severstal_text, make_fake_locate_and_read_value_executors, make_fake_structure_executor,
    make_fake_entities_executor,
):
    # Срез 6b: `analyze.full_bundle` теперь собирает сегменты/состав группы в
    # том же изолированном scope, что и показатели (критерий 5 «Критерия
    # готовности связки анализа», 16b) — те же подмены модельной границы, что
    # уже применяются к `locate`/`read.value`, нужны и здесь, иначе исполнение
    # реально ходит в сеть (`OPENROUTER_API_KEY is not set`).
    locate_exec, read_value_exec = make_fake_locate_and_read_value_executors(_FAKE_VALUES)
    registry = build_registry(
        profile_id="ifrs_nf_ru", locate_executor=locate_exec, read_value_executor=read_value_exec,
        structure_executor=make_fake_structure_executor({}), entities_executor=make_fake_entities_executor([]),
    )
    policy = default_policy("ifrs_nf_ru")
    planner = GraphPlanner(registry=registry, policy=policy)
    goal = Target(output_type="EvidenceBundleV2", entity_ref="severstal", period_ref="p-2024-fy")
    inputs = RunInputs(document_text=severstal_text, role="other")
    executor = GraphExecutor(registry=registry)

    first, first_args = planner.plan_ex(goal, root_extra={"role": "other"})
    assert first.pending_run_passport
    # A1.5: до RunPassport состав ниже identify.classify НЕ раскрыт — граф
    # первого прохода узкий (ingest.document + identify.classify + сам корень).
    assert len(first.graph.nodes) <= 3

    first_exec = executor.run(first.graph, first_args, run_id="run-full", inputs=inputs)
    classify_node = next(n for n in first.graph.nodes if n.node_key.atom_name == "identify.classify")
    passport = first_exec.evidence_by_key[classify_node.node_key][0]

    second, second_args = planner.plan_ex(goal, run_passport=passport, root_extra={"role": "other"})
    assert second.graph.root == first.graph.root, "тот же идентификатор корневой вершины (A1.11.9)"
    assert not second.pending_run_passport
    assert second.feasibility.feasible

    scalar_nodes = [n for n in second.graph.nodes if n.node_key.atom_name == "read.value"]
    assert len(scalar_nodes) > 1, "полный анализ обязан раскрыть больше одного показателя (в отличие от точечного)"
    for gate_name in ("verify.identity", "verify.cross", "norm.time", "verify.bounds"):
        gate_nodes = [n for n in second.graph.nodes if n.node_key.atom_name == gate_name]
        assert gate_nodes, f"обязательный контрольный этап {gate_name!r} должен присутствовать как явный узел (A1.7)"

    second_exec = executor.run(
        second.graph, second_args, run_id="run-full", run_passport=passport, inputs=inputs,
        prior_node_runs=first_exec.node_runs, prior_evidence=first_exec.evidence_by_key,
    )

    assert second_exec.succeeded, second_exec.node_runs[second.graph.root].error
    bundle = second_exec.evidence_by_key[second.graph.root][0]
    scalar_by_concept = {f.concept: f for f in bundle.scalar_facts}
    derived_by_concept = {f.concept: f for f in bundle.derived_facts}

    assert isinstance(scalar_by_concept.get("SALES_REVENUE"), ScalarFact)
    assert scalar_by_concept["SALES_REVENUE"].value == 1_000_000.0
    assert isinstance(derived_by_concept.get("NET_INTEREST_PAID"), DerivedFact)
    assert derived_by_concept["NET_INTEREST_PAID"].value == 7_000.0
    # Срез 6a: `.inputs` несёт evidence_id зависимостей (dependency graph),
    # не голые имена концептов — сверяем с реальными evidence_id фактов,
    # через которые узел `calc.formula` реально прошёл (после norm.unit).
    assert set(derived_by_concept["NET_INTEREST_PAID"].inputs) == {
        scalar_by_concept["INTEREST_PAID"].evidence_id, scalar_by_concept["INTEREST_RECEIVED"].evidence_id,
    }
    assert derived_by_concept["NET_INTEREST_PAID"].formula_version == passport.registry_version
    assert "INTEREST_PAID" in derived_by_concept["NET_INTEREST_PAID"].expression

    # Гейт реально исполнился и подтвердил именно этот расчёт — не пропущен.
    gate_node = next(
        n for n in second.graph.nodes
        if n.node_key.atom_name == "verify.bounds"
        and second_args[n.node_key].concept == "NET_INTEREST_PAID"
    )
    finding = second_exec.evidence_by_key[gate_node.node_key][0]
    assert finding.status == "pass"
    assert finding.concept == "NET_INTEREST_PAID"
