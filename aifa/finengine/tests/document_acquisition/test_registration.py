"""registration.py — sha256, дедуп по хэшу, регистрация Document.

Фейковый store/blob store — не реальный Postgres/S3 (тот же дух, что
`ocr_pages`-двойник у doc.textify): проверяем РЕШЕНИЕ (дедуп/не дедуп,
что записано в meta), а не SQL/S3. Интеграционный прогон против реального
`SqlDocumentStore` — `бд`-фикстура (`tests/conftest.py`), требует Postgres;
пропускается локально без него, ровно как остальные 107 тестов на базе."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from finengine.models.document import DocumentMeta
from finengine.skills.document_acquisition.registration import (
    AcquisitionProvenance,
    find_existing_by_hash,
    register_acquired_document,
)
from finengine.store.blobs import document_key as real_document_key

_SECURITY_CASES = json.loads(
    (Path(__file__).parents[1] / "golden" / "security" / "cases.json").read_text(encoding="utf-8")
)


class FakeDocumentStore:
    def __init__(self):
        self._docs: dict[str, DocumentMeta] = {}
        self.add_calls = 0

    def add_document(self, meta: DocumentMeta, chunks: list) -> None:
        self.add_calls += 1
        self._docs[meta.doc_id] = meta

    def get_document(self, doc_id: str):
        return self._docs.get(doc_id)

    def list_documents(self, owner_id: int | None = None):
        if owner_id is None:
            return list(self._docs.values())
        return [d for d in self._docs.values() if d.owner_id == owner_id]


class FakeBlobStore:
    def __init__(self):
        self.put_calls: list[tuple[str, bytes]] = []

    def put(self, key: str, data) -> None:
        self.put_calls.append((key, bytes(data)))


def _provenance(url: str = "https://www.cbr.ru/report.pdf") -> AcquisitionProvenance:
    return AcquisitionProvenance(
        source_url=url, source_domain="www.cbr.ru", source_tier="official_regulator",
        search_query="Северсталь отчёт", fetched_at=1_726_000_000.0, http_status=200,
        content_type="application/pdf", redirect_chain=(),
    )


def _document_key(doc_id: str, filename: str) -> str:
    return f"documents/{doc_id}_{filename}"


def test_register_new_document_computes_sha256_and_stores_blob():
    store, blobs = FakeDocumentStore(), FakeBlobStore()
    content = b"%PDF-1.4 some report bytes"
    result = register_acquired_document(
        content=content, filename_hint="Severstal Q1 2025.pdf", owner_id=7,
        provenance=_provenance(), store=store, blob_store=blobs, document_key=_document_key,
    )
    assert result.duplicate is False
    assert result.sha256 == hashlib.sha256(content).hexdigest()
    assert len(blobs.put_calls) == 1
    meta = store.get_document(result.doc_id)
    assert meta is not None
    assert meta.sha256 == result.sha256
    assert meta.owner_id == 7
    assert meta.acquisition["source_domain"] == "www.cbr.ru"
    assert meta.acquisition["source_tier"] == "official_regulator"
    assert meta.meta_card == {}  # не трогаем поле метакарты — своя семантика (#189)


def test_register_same_bytes_twice_same_owner_is_deduplicated():
    store, blobs = FakeDocumentStore(), FakeBlobStore()
    content = b"%PDF-1.4 identical bytes"
    first = register_acquired_document(
        content=content, filename_hint="a.pdf", owner_id=7, provenance=_provenance(),
        store=store, blob_store=blobs, document_key=_document_key,
    )
    second = register_acquired_document(
        content=content, filename_hint="a-found-again.pdf", owner_id=7,
        provenance=_provenance(url="https://e-disclosure.ru/report.pdf"),
        store=store, blob_store=blobs, document_key=_document_key,
    )
    assert first.duplicate is False
    assert second.duplicate is True
    assert second.doc_id == first.doc_id
    assert store.add_calls == 1  # второй вызов НЕ создал второй Document
    assert len(blobs.put_calls) == 1  # и не залил второй раз байты


def test_register_same_bytes_different_owner_is_not_deduplicated():
    store, blobs = FakeDocumentStore(), FakeBlobStore()
    content = b"%PDF-1.4 identical bytes"
    first = register_acquired_document(
        content=content, filename_hint="a.pdf", owner_id=7, provenance=_provenance(),
        store=store, blob_store=blobs, document_key=_document_key,
    )
    second = register_acquired_document(
        content=content, filename_hint="a.pdf", owner_id=99, provenance=_provenance(),
        store=store, blob_store=blobs, document_key=_document_key,
    )
    assert second.duplicate is False
    assert second.doc_id != first.doc_id


def test_dedup_by_bytes_not_by_url_or_name():
    """Тот же контент, ДРУГОЙ URL и ДРУГОЕ имя — всё равно дедуп (п.17-srez-07:
    "duplicate detection by bytes/version, not URL/name")."""
    store, blobs = FakeDocumentStore(), FakeBlobStore()
    content = b"%PDF-1.4 same report, mirrored"
    register_acquired_document(
        content=content, filename_hint="original-name.pdf", owner_id=1,
        provenance=_provenance(url="https://www.moex.com/mirror1.pdf"),
        store=store, blob_store=blobs, document_key=_document_key,
    )
    second = register_acquired_document(
        content=content, filename_hint="completely-different-name.pdf", owner_id=1,
        provenance=_provenance(url="https://e-disclosure.ru/mirror2.pdf"),
        store=store, blob_store=blobs, document_key=_document_key,
    )
    assert second.duplicate is True


def test_different_bytes_are_not_deduplicated_even_with_same_url_and_name():
    store, blobs = FakeDocumentStore(), FakeBlobStore()
    register_acquired_document(
        content=b"%PDF-1.4 version A", filename_hint="report.pdf", owner_id=1,
        provenance=_provenance(), store=store, blob_store=blobs, document_key=_document_key,
    )
    second = register_acquired_document(
        content=b"%PDF-1.4 version B (restated)", filename_hint="report.pdf", owner_id=1,
        provenance=_provenance(), store=store, blob_store=blobs, document_key=_document_key,
    )
    assert second.duplicate is False


@pytest.mark.parametrize(
    ("hint", "expected"),
    [
        ("../../../../etc/passwd", "_.._.._.._etc_passwd.pdf"),
        ("..\\..\\Windows\\System32\\hosts", "_.._Windows_System32_hosts.pdf"),
        ("Северсталь Q1 2025.pdf", "Северсталь Q1 2025.pdf"),
        ("report%2e%2e%2f.pdf", "report%2e%2e%2f.pdf"),  # без URL-декодирования — просто текст в имени
    ],
)
def test_filename_hint_is_sanitized_no_path_traversal(hint, expected):
    store, blobs = FakeDocumentStore(), FakeBlobStore()
    result = register_acquired_document(
        content=b"%PDF-1.4 x", filename_hint=hint, owner_id=1, provenance=_provenance(),
        store=store, blob_store=blobs, document_key=_document_key,
    )
    meta = store.get_document(result.doc_id)
    assert "/" not in meta.filename and "\\" not in meta.filename
    assert meta.filename == expected


@pytest.mark.parametrize("case_id", ["sec-009", "sec-010", "sec-011"])
def test_golden_path_traversal_cases_do_not_escape_the_storage_key(case_id):
    """sec-009/010/011: payload из golden security набора как подсказка
    имени (роль Content-Disposition) — ни санитайзер `_safe_filename`, ни
    реальный `store.blobs.document_key` (используемый и `/documents/upload`)
    не дают traversal выйти за пределы ключа объектного хранилища."""
    case = next(c for c in _SECURITY_CASES if c["id"] == case_id)
    store, blobs = FakeDocumentStore(), FakeBlobStore()
    result = register_acquired_document(
        content=b"%PDF-1.4 x", filename_hint=case["payload"], owner_id=1, provenance=_provenance(),
        store=store, blob_store=blobs, document_key=real_document_key,
    )
    meta = store.get_document(result.doc_id)
    # Разделители пути убраны — оставшиеся ".." (если есть) не могут больше
    # действовать как traversal-сегмент без "/"/"\" вокруг них, это просто
    # текст в имени файла.
    assert "/" not in meta.filename and "\\" not in meta.filename

    key = blobs.put_calls[0][0]
    assert key.startswith(f"documents/{result.doc_id}/")
    assert "/" not in key[len(f"documents/{result.doc_id}/"):]  # ключ не ушёл глубже одного сегмента


def test_find_existing_by_hash_ignores_documents_without_sha256():
    """Ручная загрузка (`/documents/upload`) сегодня sha256 не пишет —
    пустое поле НЕ должно матчиться само с собой между двумя такими
    документами (иначе первый же ручной аплоад "дедуплицировался" бы со
    вторым просто потому, что оба пустые)."""
    store = FakeDocumentStore()
    manual_1 = DocumentMeta(doc_id="d1", filename="a.pdf", owner_id=1, sha256="")
    manual_2 = DocumentMeta(doc_id="d2", filename="b.pdf", owner_id=1, sha256="")
    store.add_document(manual_1, [])
    store.add_document(manual_2, [])
    assert find_existing_by_hash(store, owner_id=1, sha256="") is None
