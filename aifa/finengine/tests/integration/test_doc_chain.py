"""`doc.textify → doc.index` — Срез 5a: два новых атома проверены КАК ЕДИНОЕ
ЦЕЛОЕ через настоящий `PlanGraph`+`GraphExecutor` (не изолированные функции),
на маленьком синтетическом ASCII-PDF — не на золотых excerpt'ах МСФО
(кириллица в PyMuPDF без встроенного шрифта не читается обратно, см.
докстринг `graph_atoms.py` и `tests/atoms/test_doc_probe.py`).

`locate`/`read.value` в этой сессии НЕ подключены как зависимости этих двух
атомов (см. докстринг `graph_atoms.py`) — их собственная интеграция в целевой
маршрут проверяется тремя существующими сценариями Среза 3/4
(`test_graph_point_metric.py` и соседи), через `document_text`, не через
`doc.textify`."""
from __future__ import annotations

import fitz

from finengine.skills.atoms.graph_atoms import build_registry, default_policy
from finengine.skills.spec.graph_executor import GraphExecutor, RunInputs
from finengine.skills.spec.graph_planner import GraphPlanner
from finengine.skills.spec.target import Target


def _ascii_pdf_bytes() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Annual report cover page for the doc.textify chain test.", fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def test_doc_textify_and_doc_index_run_as_real_connected_graph_nodes():
    registry = build_registry(profile_id="ifrs_nf_ru", table_labeler=lambda _summaries: {})
    policy = default_policy("ifrs_nf_ru")
    planner = GraphPlanner(registry=registry, policy=policy)
    goal = Target(output_type="doc.textify:index", entity_ref="doc-1", period_ref="v1")

    result, node_args = planner.plan_ex(goal)
    assert result.feasibility.feasible, result.feasibility.reason
    node_names = {n.node_key.atom_name for n in result.graph.nodes}
    assert {"doc.textify", "doc.index"} <= node_names, "doc.index обязан раскрыть doc.textify как свою зависимость"

    inputs = RunInputs(pdf_bytes=_ascii_pdf_bytes())
    exec_result = GraphExecutor(registry=registry).run(
        result.graph, node_args, run_id="run-doc-chain", inputs=inputs,
    )
    assert exec_result.succeeded, exec_result.node_runs[result.graph.root].error

    textify_node = next(n for n in result.graph.nodes if n.node_key.atom_name == "doc.textify")
    pages = exec_result.evidence_by_key[textify_node.node_key]
    assert len(pages) == 1
    assert pages[0].status == "ok"
    assert pages[0].extraction_method == "digital"

    index = exec_result.evidence_by_key[result.graph.root][0]
    assert index.total_pages == 1
    assert index.document_id == pages[0].document_id
    assert index.document_version == pages[0].document_version
