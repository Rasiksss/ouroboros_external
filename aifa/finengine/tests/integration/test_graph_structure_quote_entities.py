"""Три атома Среза 5b (`read.structure`/`read.quote`/`read.entities`) через
настоящий `PlanGraph`+`GraphExecutor`, не изолированный код — тот же
Definition of Done, что уже проверен для `read.value` в `test_graph_point_
metric.py`. Единственная подмена — модельная граница; `GraphPlanner`/
`GraphExecutor`/`AtomRegistry` настоящие."""
from __future__ import annotations

from finengine.contracts import QuoteEvidence
from finengine.skills.atoms.graph_atoms import build_registry, default_policy
from finengine.skills.spec.graph_executor import RunInputs
from finengine.skills.spec.graph_planner import GraphPlanner
from finengine.skills.spec.target import Target


def _plan_and_run(registry, policy, goal, *, text):
    """`read.quote` не несёт `needs_run_passport` (не нужна классификация для
    поиска цитаты) — первый проход у него уже финальный, в отличие от
    `read.structure`/`read.entities`, которым нужен `RunPassport` (двухпроходное
    раскрытие A1.5, тот же путь, что у `read.value`)."""
    planner = GraphPlanner(registry=registry, policy=policy)
    inputs = RunInputs(document_text=text, role="other")
    from finengine.skills.spec.graph_executor import GraphExecutor
    executor = GraphExecutor(registry=registry)

    first, first_args = planner.plan_ex(goal)
    first_exec = executor.run(first.graph, first_args, run_id="run-1", inputs=inputs)
    if not first.pending_run_passport:
        return first, first_exec

    classify_node = next(n for n in first.graph.nodes if n.node_key.atom_name == "identify.classify")
    passport = first_exec.evidence_by_key[classify_node.node_key][0]

    second, second_args = planner.plan_ex(goal, run_passport=passport)
    assert not second.pending_run_passport
    second_exec = executor.run(
        second.graph, second_args, run_id="run-1", run_passport=passport, inputs=inputs,
        prior_node_runs=first_exec.node_runs, prior_evidence=first_exec.evidence_by_key,
    )
    return second, second_exec


def test_extracted_structure_flows_through_the_real_planner_and_executor(
    severstal_text, make_fake_structure_executor,
):
    rows = [
        {"dimensions": {"operating_segment": "Розница"}, "concept": "SALES_REVENUE",
         "value": 600.0, "unit": "RUB_MLN", "period_ref": "FY2024", "page": 8,
         "is_total": False, "is_elimination": False, "is_open_ended": False, "raw_label": "Розница"},
        {"dimensions": {"operating_segment": "TOTAL"}, "concept": "SALES_REVENUE",
         "value": 1000.0, "unit": "RUB_MLN", "period_ref": "FY2024", "page": 8,
         "is_total": True, "is_elimination": False, "is_open_ended": False, "raw_label": "Итого"},
    ]
    registry = build_registry(profile_id="ifrs_nf_ru", structure_executor=make_fake_structure_executor(
        {"segments": rows},
    ))
    goal = Target(output_type="ExtractedStructure", concept_id="segments",
                  entity_ref="severstal", period_ref="p-2024-fy")
    graph, execution = _plan_and_run(registry, default_policy("ifrs_nf_ru"), goal, text=severstal_text)

    assert execution.succeeded
    root_evidence = execution.evidence_by_key[graph.graph.root]
    assert len(root_evidence) == 1
    extracted_structure = root_evidence[0]
    assert extracted_structure.table_kind == "segments"
    # R2/5b: число живёт в `.structured_fact.rows`, не задвоено в обёртке.
    assert extracted_structure.structured_fact.rows == rows
    assert any(r["is_total"] for r in extracted_structure.structured_fact.rows)

    structure_nodes = [n for n in graph.graph.nodes if n.node_key.atom_name == "read.structure"]
    assert len(structure_nodes) == 1, "точечный запрос структуры не должен раскрыть другие table_kind"


def test_quote_evidence_flows_through_the_real_planner_and_executor(
    severstal_text, make_fake_quote_executor,
):
    registry = build_registry(profile_id="ifrs_nf_ru", quote_executor=make_fake_quote_executor(
        {"dividends": ("The Board recommended not to pay dividends.", 3)},
    ))
    goal = Target(output_type="QuoteEvidence", concept_id="dividends",
                  entity_ref="severstal", period_ref="p-2024-fy")
    graph, execution = _plan_and_run(registry, default_policy("ifrs_nf_ru"), goal, text=severstal_text)

    assert execution.succeeded
    # Срез 6a: `read.quote` теперь несёт mandatory_gate `verify.citation` —
    # корень становится этим гейтом, который пропускает и находку, и саму
    # цитату (карточка: «неуспешная проверка не удаляет цитату»). Фикстура
    # даёт синтетический текст цитаты, которого нет буквально в `severstal_
    # text` на заявленной странице — сама сверка здесь намеренно не
    # проверяется (см. `tests/atoms/test_verify_citation.py`), только то,
    # что цитата доезжает до потребителя через гейт неповреждённой.
    root_evidence = execution.evidence_by_key[graph.graph.root]
    quote = next(e for e in root_evidence if isinstance(e, QuoteEvidence))
    assert quote.quote_text == "The Board recommended not to pay dividends."
    assert quote.page == 3
    assert quote.concept == "dividends"


def test_entity_relation_flows_through_the_real_planner_and_executor(
    severstal_text, make_fake_entities_executor,
):
    registry = build_registry(profile_id="ifrs_nf_ru", entities_executor=make_fake_entities_executor(
        [("Subsidiary LLC", "subsidiary", 100.0)],
    ))
    goal = Target(output_type="EntityRelation", entity_ref="severstal", period_ref="p-2024-fy")
    graph, execution = _plan_and_run(registry, default_policy("ifrs_nf_ru"), goal, text=severstal_text)

    assert execution.succeeded
    root_evidence = execution.evidence_by_key[graph.graph.root]
    assert len(root_evidence) == 1
    relation = root_evidence[0]
    assert relation.subject_entity == "Subsidiary LLC"
    assert relation.ownership_percent == 100.0
