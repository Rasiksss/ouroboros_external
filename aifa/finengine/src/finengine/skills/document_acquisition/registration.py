"""Регистрация web-найденного документа как ОБЫЧНОГО Document — Срез 7 A5.

Тот же реестр (`store/documents.py::SqlDocumentStore`) и то же объектное
хранилище (`store/blobs.py`), что `/documents/upload` (`api/main.py`) —
никакой отдельной таблицы или маршрута для «веб-документов». Единственное
отличие от ручной загрузки — заполненное поле `DocumentMeta.acquisition`.

ДЕДУПЛИКАЦИЯ — ПО СОДЕРЖИМОМУ (sha256), НЕ ПО URL/ИМЕНИ: два разных URL с
одинаковыми байтами дают ОДИН Document, а не два. Сравнение — с документами,
у которых `sha256` уже проставлен, то есть с прошлыми находками A5 ЭТОГО ЖЕ
владельца. `/documents/upload` (ручная загрузка) сегодня `sha256` не пишет —
осознанно оставленная асимметрия этого среза, зафиксирована в Nedodelano.md:
трогать 443-строчный продакшен-эндпоинт с историей инцидентов ради
симметричного дедупа владелец решил не в этой сессии. Равенство «то же самое
содержимое» между ручной загрузкой и веб-находкой доказывается не
переиспользованием одного `doc_id`, а одинаковым `document_version` (=sha256)
на шаге `doc.probe` — см. `acquire.py` и
`tests/integration/test_document_acquisition_equivalence.py`."""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Callable, Protocol

from finengine.models.document import DocumentMeta
from finengine.store.blobs import document_key as _default_document_key
from finengine.store.blobs import get_blob_store as _default_get_blob_store
from finengine.ingestion.document_store import get_store as _default_get_store


class DocumentStoreProtocol(Protocol):
    def add_document(self, meta: DocumentMeta, chunks: list) -> None: ...
    def get_document(self, doc_id: str) -> DocumentMeta | None: ...
    def list_documents(self, owner_id: int | None = None) -> list[DocumentMeta]: ...


class BlobStoreProtocol(Protocol):
    def put(self, key: str, data) -> None: ...


@dataclass(frozen=True)
class AcquisitionProvenance:
    """Происхождение приёма. НИКАКИХ секретов/внутренних сетевых деталей —
    только то, что и так наблюдаемо в самом URL/HTTP-ответе (C12: журнал не
    должен раскрывать больше, чем видел бы человек, открывший этот URL в
    браузере)."""

    source_url: str
    source_domain: str
    source_tier: str
    search_query: str
    fetched_at: float
    http_status: int
    content_type: str
    redirect_chain: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "source_url": self.source_url,
            "source_domain": self.source_domain,
            "source_tier": self.source_tier,
            "search_query": self.search_query,
            "fetched_at": self.fetched_at,
            "http_status": self.http_status,
            "content_type": self.content_type,
            "redirect_chain": list(self.redirect_chain),
        }


@dataclass(frozen=True)
class RegistrationResult:
    doc_id: str
    sha256: str
    duplicate: bool  # True = переиспользован существующий Document A5, новый не создавался


def _safe_filename(hint: str) -> str:
    """Имя файла — только для отображения/расширения. Content-Disposition
    может нести что угодно (путь, управляющие символы) — здесь оно приводится
    к безопасной строке ДО того, как попадёт в `DocumentMeta.filename`; ключ
    объектного хранилища (`document_key`) строит `doc_id` (server-side uuid),
    не имя, так что traversal через это поле и так не достиг бы файловой
    системы, но само поле обязано быть безопасным и для UI/Excel-экспорта."""
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", hint).strip().strip(".") or "report"
    stem = stem[:120]
    if not stem.lower().endswith(".pdf"):
        stem = f"{stem}.pdf"
    return stem


def find_existing_by_hash(store: DocumentStoreProtocol, *, owner_id: int, sha256: str) -> DocumentMeta | None:
    for meta in store.list_documents(owner_id=owner_id):
        if meta.sha256 and meta.sha256 == sha256:
            return meta
    return None


def register_acquired_document(
    *,
    content: bytes,
    filename_hint: str,
    owner_id: int,
    provenance: AcquisitionProvenance,
    store: DocumentStoreProtocol | None = None,
    blob_store: BlobStoreProtocol | None = None,
    document_key: Callable[[str, str], str] = _default_document_key,
) -> RegistrationResult:
    """Зарегистрировать байты как Document. Возвращает СУЩЕСТВУЮЩИЙ doc_id,
    если байты уже регистрировались этим владельцем через A5 ранее.

    `store`/`blob_store` — точки подмены для тестов (fake, тот же duck-typed
    интерфейс, что `SqlDocumentStore`/`LocalBlobStore`); по умолчанию — реальные
    `ingestion.document_store.get_store()`/`store.blobs.get_blob_store()`,
    ровно те, что использует `/documents/upload`."""
    store = store if store is not None else _default_get_store()
    blob_store = blob_store if blob_store is not None else _default_get_blob_store()

    digest = hashlib.sha256(content).hexdigest()

    existing = find_existing_by_hash(store, owner_id=owner_id, sha256=digest)
    if existing is not None:
        return RegistrationResult(doc_id=existing.doc_id, sha256=digest, duplicate=True)

    doc_id = str(uuid.uuid4())
    filename = _safe_filename(filename_hint)
    key = document_key(doc_id, filename)
    blob_store.put(key, content)

    meta = DocumentMeta(
        doc_id=doc_id, filename=filename, owner_id=owner_id, file_path=key,
        sha256=digest, acquisition=provenance.to_dict(),
    )
    store.add_document(meta, [])
    return RegistrationResult(doc_id=doc_id, sha256=digest, duplicate=False)


__all__ = [
    "AcquisitionProvenance", "RegistrationResult", "DocumentStoreProtocol", "BlobStoreProtocol",
    "find_existing_by_hash", "register_acquired_document",
]
