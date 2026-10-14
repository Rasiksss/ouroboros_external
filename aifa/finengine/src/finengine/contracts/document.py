"""Область «документ»: карточка, текст постранично, семантический индекс
документа, технический паспорт до OCR (`doc.probe`) и устойчивая идентичность
субъекта документа (`id.entity`, A6 — см. 11-srez-01-run-passport.md).
`DocumentText`/`DocumentIndex`/`DocumentIndexEntry` — контракты Среза 5a
(`doc.textify`/`doc.index`, 15a-srez-05a-locate-read-value-yadro.md)."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .base import Contract, NonEmptyStr, new_id

TextExtractionMethod = Literal["digital", "ocr"]
TextExtractionStatus = Literal["ok", "error"]


class DocumentProbe(Contract):
    """Дешёвый технический паспорт файла до OCR и до финансовой классификации
    (`doc.probe`). `technical_status` — явный статус, а не пустой «успешный»
    документ: `ok | not_a_pdf | corrupted | encrypted | empty | too_large`.
    """

    sha256: NonEmptyStr
    byte_size: int = Field(ge=0)
    technical_status: NonEmptyStr
    total_pages: int = Field(ge=0)
    pdf_origin: str = ""  # "digital" | "scanned" | "mixed" | "" когда статус не ok
    needs_ocr: bool = False
    doc_type_labels: list[str] = Field(default_factory=list)
    reason: str = ""

    @model_validator(mode="after")
    def _ok_status_is_not_an_empty_success(self):
        if self.technical_status == "ok" and self.total_pages < 1:
            raise ValueError("technical_status 'ok' requires at least one page")
        return self


class EntityIdentity(Contract):
    """Устойчивая идентичность юридического субъекта (`id.entity`). Название
    само по себе не считается устойчивым ключом: `entity_id` — производная от
    проверяемых внешних маркеров, если они найдены в тексте, иначе — от
    канонического имени с явным `confidence_low`, а не тихое склеивание
    одноимённых разных сущностей.
    """

    entity_id: NonEmptyStr
    canonical_name: NonEmptyStr
    raw_name: NonEmptyStr
    entity_type: NonEmptyStr = "issuer"
    external_ids: dict[str, str] = Field(default_factory=dict)
    confidence_low: bool = False
    reason: str = ""
    source: NonEmptyStr = "document_text"


class DocumentCard(Contract):
    """Метакарта документа — снимается один раз при приёме (02, §2.1)."""

    document_id: NonEmptyStr
    filename: NonEmptyStr
    company_name: str = ""
    period_ref: str | None = None
    language: NonEmptyStr
    document_version: NonEmptyStr


class DocumentText(Contract):
    """Текст одной страницы документа конкретной версии (`doc.textify`).

    `extraction_method`/`quality` — как получен текст этой СТРАНИЦЫ (не
    документа в целом: `doc.probe.needs_ocr` — грубый агрегат, здесь решение
    постраничное, карточка `doc.textify`). `raw_ocr_text` хранит ответ OCR ДО
    детерминированной корректировки чисел (`_fix_ocr_numbers`), когда
    корректировка что-то поменяла — «исправления чисел не должны уничтожать
    исходное значение». `status`/`error` — локальный статус страницы: ошибка
    одной страницы не должна выглядеть как отсутствие текста без объяснения и
    не должна ronять остальные уже успешно обработанные страницы.
    """

    document_id: NonEmptyStr
    document_version: NonEmptyStr
    page: int = Field(ge=1)
    text: str
    extraction_method: TextExtractionMethod = "digital"
    quality: float = Field(default=1.0, ge=0.0, le=1.0)
    raw_ocr_text: str | None = None
    status: TextExtractionStatus = "ok"
    error: str | None = None


class DocumentIndexEntry(Contract):
    """Один структурный блок документа: таблица, заголовок, раздел (`doc.index`).

    `kind="unknown"` — физически найденный блок, которому модель не смогла
    присвоить известную семантическую метку; карточка `doc.index` требует
    сохранять его как неизвестный, а не подгонять под ближайший известный тип.
    """

    entry_id: str = Field(default_factory=new_id)
    kind: Literal["table", "heading", "section", "unknown"]
    label: str = ""
    title: str = ""
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)

    @model_validator(mode="after")
    def _page_range_is_ordered(self):
        if self.page_end < self.page_start:
            raise ValueError(
                f"DocumentIndexEntry {self.entry_id}: page_end={self.page_end} < page_start={self.page_start}"
            )
        return self


class DocumentIndex(Contract):
    """Семантическая карта документа, независимая от конкретного показателя
    (`doc.index`). Кэшируется по паре (`document_version`, `indexer_version`) —
    оба поля обязаны совпасть, чтобы переиспользовать индекс без пересканирования.
    """

    document_id: NonEmptyStr
    document_version: NonEmptyStr
    indexer_version: NonEmptyStr
    total_pages: int = Field(ge=0)
    entries: list[DocumentIndexEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _entries_reference_existing_pages(self):
        for entry in self.entries:
            if entry.page_start > self.total_pages or entry.page_end > self.total_pages:
                raise ValueError(
                    f"DocumentIndexEntry {entry.entry_id} ссылается на страницу "
                    f"{entry.page_end} за пределами документа (total_pages={self.total_pages})"
                )
        return self
