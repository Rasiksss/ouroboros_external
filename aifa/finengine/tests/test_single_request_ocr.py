"""AEFClaw wire contract and honest provenance for unpaginated OCR text."""

import asyncio
from contextlib import nullcontext
import json
from types import SimpleNamespace

import httpx
import pytest

import test_document_extractor_ocr as fixtures


@pytest.fixture
def ocr(monkeypatch):
    module = fixtures.ocr.__wrapped__(monkeypatch)
    monkeypatch.setattr(module.pymupdf, "open", lambda path: nullcontext(SimpleNamespace(page_count=70)))
    return module


@pytest.fixture
def raw():
    return {"spec":{"page_range":[1,70], "engine":{"output_type":"markdown"}},
            "text":"Narrative without page boundaries.\n\n" + fixtures.TABLE,
            "tables":[{"page":3, "formatted_str":fixtures.TABLE, "position_range":[40,100],
                       "caption":"Balance sheet", "annotation":"", "width":100,"height":50}],
            "pictures":[], "page_images":[], "markdowns":[]}


def test_one_extract_for_70_pages_accepts_real_flat_response(monkeypatch, tmp_path, ocr, raw):
    calls = []
    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            assert request.url.path == "/api/v2/files/upload" and b"application/pdf" in request.content
            assert request.headers["client-id"] == "aifa-test-agent"
            return httpx.Response(200, json={"resource_id":"source.pdf"})
        if len(calls) == 2:
            assert request.url.path == "/api/extract"
            assert json.loads(request.content) == {"resource_id":"source.pdf", "spec":{}}
            return httpx.Response(200, json={"resource_id":"result.json"})
        assert len(calls) == 3 and request.url.params["resource_id"] == "result.json"
        return httpx.Response(200, json=raw)
    original = httpx.AsyncClient
    monkeypatch.setattr(ocr.httpx,"AsyncClient",lambda **kw:original(**kw,transport=httpx.MockTransport(respond)))
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF synthetic complete document")
    destination = tmp_path / "raw.json"
    result = asyncio.run(ocr.call_idp_ocr(pdf, save_raw_to=destination))
    assert len(calls) == 3 and result.full_text == raw["text"]
    assert not result.pages  # No invented page 1 for a complete report.
    assert result.diag["pages_total"] == 70 and result.diag["pages_failed"] == 0
    assert result.diag["text_page_binding"] == "unavailable"
    assert result.full_tables[0]["page"] == 3 and result.full_tables[0]["text"] == fixtures.TABLE
    saved = json.loads(destination.read_text(encoding="utf-8"))
    assert saved == raw and ocr.markdown_from_saved_idp_response(saved) == result.full_text


def test_table_without_page_stays_unknown_and_bad_physical_page_is_rejected(ocr, raw):
    raw["tables"][0]["page"] = None
    assert ocr._document_extractor_result(raw,70).full_tables[0]["page"] is None
    raw["tables"][0]["page"] = 71
    with pytest.raises(ValueError):
        ocr._document_extractor_result(raw,70)


def test_runtime_passes_saved_ocr_into_graph_inputs(monkeypatch, raw):
    from finengine.skills.production_runtime import _run_inputs
    from finengine.store import blobs
    encoded = json.dumps(raw).encode()
    monkeypatch.setattr(blobs,"document_idp_raw",lambda doc_id:encoded)
    ctx = SimpleNamespace(owner_id=7,case_id="case",document_id="doc",trace_id="trace")
    inputs = _run_inputs(ctx,document_text=raw["text"],role="other",extra={})
    assert json.loads(inputs.extra["ocr_result"]) == raw
    monkeypatch.setattr(blobs,"document_idp_raw",lambda doc_id:None)
    inputs = _run_inputs(ctx,document_text=raw["text"],role="other",extra={"ocr_result":"untrusted override"})
    assert "ocr_result" not in inputs.extra


def test_locate_reuses_structured_table_page_and_does_not_invent_quote_page(monkeypatch,raw):
    from finengine.skills.atoms import locate
    async def model(shards,**kwargs):
        assert kwargs["tables"][0].page == 3
        return [{"metrics":{"ASSETS":{"tables":[{"table_id":"T001"}],
                    "quotes":[{"page":99,"text":"Narrative without page boundaries."}]}}}]
    monkeypatch.setattr(locate,"_locate_all_shards",model)
    result = locate.locate_batch(["ASSETS"],document_id="doc",document_version="v1",document_text=raw["text"],
                                raw_idp=json.dumps(raw).encode(),metrics=[{"enum":"ASSETS","name_ru":"Активы"}])
    candidates = result["ASSETS"]
    assert candidates[0].locator.page == 3
    assert candidates[-1].locator.page is None


@pytest.mark.parametrize("module",["read_quote","read_condition","verify_citation"])
def test_flat_text_cannot_be_verified_as_physical_page_one(module):
    import importlib
    atom = importlib.import_module("finengine.skills.atoms."+module)
    assert atom._pages_from_document_text("A complete 70-page report") == {}
    assert atom._pages_from_document_text("===PAGE 3===\nKnown text") == {3:"Known text"}


def test_quotable_text_uses_only_table_pages_that_ocr_identified(raw):
    from finengine.skills.ocr_source import page_bound_text, pages_from_text
    text = page_bound_text(raw["text"],json.dumps(raw))
    pages = pages_from_text(text)
    assert list(pages) == [3] and fixtures.TABLE in pages[3]
    assert "Narrative without page boundaries" not in pages[3]


def test_unknown_text_parts_are_not_presented_as_pdf_pages():
    from finengine.assistant import doc_pages, ouro_view
    parts = doc_pages.pages_of("An unpaginated report")
    assert not parts[0].physical
    assert doc_pages.find("report",parts)[0].page is None
    shown = ouro_view.doc_pages("doc",parts,"1",limit=12000)
    assert "===PAGE 1===" not in shown
    assert "PDF" in shown
    assert "стр. 1" not in ouro_view.doc_matches("doc","report",doc_pages.find("report",parts))


def test_ingestion_and_index_do_not_assign_a_fake_page():
    from finengine.contracts import DocumentText
    from finengine.skills.atoms.graph_atoms import _ingest_document, NodeArgs
    from finengine.skills.atoms.document_index import build_document_index
    ctx = SimpleNamespace(inputs=SimpleNamespace(document_text="Unpaginated report"),run_id="run")
    result = _ingest_document(NodeArgs(entity_ref="doc"),ctx)
    assert len(result) == 1 and result[0].page is None
    index = build_document_index(result,document_id="doc",document_version="run",total_pages=70,table_labeler=lambda _: {})
    assert not index.entries
    assert DocumentText(document_id="doc",document_version="v1",page=None,text="Unknown page").page is None


def test_model_cannot_assign_a_page_absent_from_candidate_evidence(monkeypatch):
    from finengine.contracts import CandidateLocator, SourceLocator
    from finengine.skills.atoms.read_value import _scalar_facts_for_concept
    from finengine.skills import bulk_extractor as bulk
    monkeypatch.setattr(bulk,"_coerce_period_values",lambda *a,**k:([
        {"value":100,"period":"2024","unit":"RUB","page":99}],"RUB"))
    candidate = CandidateLocator(document_id="doc",document_version="v1",concept="ASSETS",
        produced_by_atom="locate",produced_by_atom_version="1",confidence=0.6,
        locator=SourceLocator(page=3,text_fragment="Assets 100"))
    facts = _scalar_facts_for_concept("ASSETS",{},[candidate],document_version="v1",period_current="2024",
        period_prev="2023",default_unit="RUB",period_calendar="calendar",max_report_year=2024)
    assert facts[0].page is None


def test_citation_verifier_uses_the_same_table_pages_as_quote_reader(raw):
    from finengine.contracts import QuoteEvidence
    from finengine.skills.atoms.verify_citation import verify_citation_gate
    evidence = QuoteEvidence(produced_by_atom="read.quote",produced_by_atom_version="1",source_ref="doc@page:3",
                             document_version="v1",concept="ASSETS",quote_text="Активы",page=3)
    ctx = SimpleNamespace(inputs=SimpleNamespace(document_text=raw["text"],extra={"ocr_result":json.dumps(raw)}),
                          dependencies=lambda:[evidence])
    result = verify_citation_gate(None,ctx)
    assert result[0].status == "pass"


def test_real_graph_adapters_forward_ocr_sidecar_to_numeric_and_structure_atoms(monkeypatch, raw):
    from finengine.skills.atoms import graph_atoms, locate, read_structure
    from finengine.skills import bulk_extractor as bulk
    calls = []
    monkeypatch.setattr(locate,"locate_batch",lambda *a,**k:calls.append(k) or {})
    monkeypatch.setattr(read_structure,"read_structure_batch",lambda *a,**k:calls.append(k) or {})
    monkeypatch.setattr(graph_atoms,"registry_for_passport",lambda p:{"extractable":[{"enum":"ASSETS"}]})
    monkeypatch.setattr(bulk,"_select_run_periods",lambda *a,**k:(["2024","2023"],"2024","2023"))
    monkeypatch.setattr(bulk,"_detect_reporting_standard",lambda text:"ifrs")
    monkeypatch.setattr(bulk,"_segment_metric_scope",lambda *a:[])
    passport = SimpleNamespace(entity=SimpleNamespace(entity_id="bank"),
        classification=SimpleNamespace(company_type="bank",standard="ifrs",language="ru"))
    ctx = SimpleNamespace(run_passport=passport,run_id="run",
        inputs=SimpleNamespace(document_text=raw["text"],extra={"ocr_result":json.dumps(raw)}))
    graph_atoms._locate_batch_real([graph_atoms.NodeArgs(concept="ASSETS")],ctx)
    graph_atoms._read_structure_batch_real([graph_atoms.NodeArgs(concept="segments")],ctx)
    assert len(calls) == 2 and all(json.loads(call["raw_idp"]) == raw for call in calls)
