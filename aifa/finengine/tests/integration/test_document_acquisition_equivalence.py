"""Срез 7 (A5) — DoD: «Найденный в интернете официальный PDF и вручную
загруженный тот же файл дают одинаковый `doc_version` и тот же доменный
результат, различаются только происхождением загрузки».

Байты одинаковые -> sha256 одинаковый (Срез 1, карточка `doc.probe`: «sha256
использовать как основу версии документа») -> `document_version`, поданный в
РЕАЛЬНЫЙ `GraphPlanner`+`GraphExecutor` (не изолированные функции — тот же
принцип, что `tests/integration/test_doc_chain.py`), даёт идентичный
`DocumentIndex`. `DocumentMeta.acquisition` — единственное различие, и оно
живёт вне графа (в реестре документов), поэтому здесь не участвует."""
from __future__ import annotations

import hashlib

import fitz

from finengine.models.document import DocumentMeta
from finengine.skills.atoms.graph_atoms import build_registry, default_policy
from finengine.skills.document_acquisition.registration import (
    AcquisitionProvenance,
    register_acquired_document,
)
from finengine.skills.spec.graph_executor import GraphExecutor, RunInputs
from finengine.skills.spec.graph_planner import GraphPlanner
from finengine.skills.spec.target import Target


class _FakeDocumentStore:
    def __init__(self):
        self._docs: dict[str, DocumentMeta] = {}

    def add_document(self, meta, chunks):
        self._docs[meta.doc_id] = meta

    def get_document(self, doc_id):
        return self._docs.get(doc_id)

    def list_documents(self, owner_id=None):
        if owner_id is None:
            return list(self._docs.values())
        return [d for d in self._docs.values() if d.owner_id == owner_id]


class _FakeBlobStore:
    def put(self, key, data):
        pass


def _ascii_pdf_bytes() -> bytes:
    """Тот же генератор, что `test_doc_chain.py` — маленький синтетический
    ASCII-PDF (кириллица в PyMuPDF без встроенного шрифта не читается
    обратно, см. докстринг `graph_atoms.py`/`test_doc_probe.py`)."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Annual report cover page — acquisition equivalence test.", fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def _run_doc_chain(registry, policy, *, document_version: str, pdf_bytes: bytes):
    planner = GraphPlanner(registry=registry, policy=policy)
    goal = Target(output_type="doc.textify:index", entity_ref="doc-equiv", period_ref=document_version)
    result, node_args = planner.plan_ex(goal)
    assert result.feasibility.feasible, result.feasibility.reason
    exec_result = GraphExecutor(registry=registry).run(
        result.graph, node_args, run_id=f"run-{document_version}", inputs=RunInputs(pdf_bytes=pdf_bytes),
    )
    assert exec_result.succeeded, exec_result.node_runs[result.graph.root].error
    return exec_result.evidence_by_key[result.graph.root][0]


def test_manual_and_web_acquired_identical_bytes_share_document_version():
    pdf_bytes = _ascii_pdf_bytes()

    # "Ручная загрузка": sha256 считается на приёме, ровно как doc.probe его
    # считает сегодня (probe_document._sha256_bytes — тот же hashlib.sha256).
    manual_sha256 = hashlib.sha256(pdf_bytes).hexdigest()

    # "Найдено в интернете" — через РЕАЛЬНУЮ регистрацию A5, не имитацию.
    reg = register_acquired_document(
        content=pdf_bytes, filename_hint="report.pdf", owner_id=1,
        provenance=AcquisitionProvenance(
            source_url="https://www.cbr.ru/report.pdf", source_domain="www.cbr.ru",
            source_tier="official_regulator", search_query="test", fetched_at=0.0,
            http_status=200, content_type="application/pdf", redirect_chain=(),
        ),
        store=_FakeDocumentStore(), blob_store=_FakeBlobStore(),
        document_key=lambda doc_id, filename: f"documents/{doc_id}_{filename}",
    )

    assert reg.sha256 == manual_sha256, (
        "A5 обязан вычислять sha256 ТЕМ ЖЕ алгоритмом, что doc.probe — иначе "
        "«одинаковый doc_version» невозможен даже в принципе"
    )

    registry = build_registry(profile_id="ifrs_nf_ru", table_labeler=lambda _summaries: {})
    policy = default_policy("ifrs_nf_ru")

    manual_index = _run_doc_chain(registry, policy, document_version=manual_sha256, pdf_bytes=pdf_bytes)
    web_index = _run_doc_chain(registry, policy, document_version=reg.sha256, pdf_bytes=pdf_bytes)

    assert manual_index.document_version == web_index.document_version == reg.sha256
    assert manual_index.total_pages == web_index.total_pages
    assert len(manual_index.entries) == len(web_index.entries)
    assert manual_index.indexer_version == web_index.indexer_version


def test_different_bytes_get_different_document_version():
    """Отрицательный контроль: разные байты — разный sha256/document_version,
    а не совпадение по случайности схемы теста выше."""
    a = hashlib.sha256(_ascii_pdf_bytes()).hexdigest()

    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "A different report entirely.", fontsize=11)
    other_bytes = doc.tobytes()
    doc.close()
    b = hashlib.sha256(other_bytes).hexdigest()

    assert a != b
