from __future__ import annotations
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field
import uuid


class DocType(str, Enum):
    IFRS_REPORT = "ifrs_report"
    RSBU_REPORT = "rsbu_report"
    PRESS_RELEASE = "press_release"
    TRANSCRIPT = "transcript"
    PRESENTATION = "presentation"
    EXCEL = "excel"
    OTHER = "other"


class CompanyType(str, Enum):
    BANK = "bank"
    NON_FINANCIAL = "non_financial"
    HYBRID = "hybrid"  # e.g. MTS, Yandex
    UNKNOWN = "unknown"


class DocumentMeta(BaseModel):
    doc_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    filename: str
    doc_type: DocType = DocType.OTHER
    company_name: str = ""
    company_type: CompanyType = CompanyType.UNKNOWN
    period: str = ""  # e.g. "2024", "Q3 2024", "H1 2023"
    language: str = "ru"  # "ru" or "en"
    total_pages: int = 0
    file_path: str = ""
    pdf_origin: str = ""  # "digital" | "scanned" | "mixed" | "" (unknown)
    # Which OCR backend produced the .txt sidecar:
    #   "pymupdf" — built-in PyMuPDF + pdfplumber pipeline (fast, weak on tables)
    #   "idp"     — IDP-OCR cloud (per-page PNG, strong tables, slow)
    # Empty / missing = legacy upload before the field existed; treat as pymupdf.
    ocr_engine: str = ""
    # Owner (auth user id). 0 = legacy/unowned (only the bootstrap admin can see).
    owner_id: int = 0
    # Метакарта (#189): что за документ, о ком, за какой период и сколько его.
    # Снимается ОДИН раз при приёме, ПОСЛЕ приведения к тексту, и хранится с
    # документом: классификация — свойство документа, а не действие анализа
    # (сегодня `detect_profile` вызывается заново на каждом прогоне).
    # Пусто = карта ещё не снималась (документ принят до появления поля либо
    # обращение к модели не удалось — причина тогда лежит в ключе `error`).
    meta_card: dict[str, Any] = Field(default_factory=dict)
    # Срез 7 (A5, docs/build-plan/17-srez-07-otchetnost-iz-interneta.md):
    # sha256 байтов документа — основа идентичности/версии («doc.probe»
    # использует то же правило: sha256 = основа document_version). Пусто =
    # ручная загрузка ДО этого среза (/documents/upload его не считает —
    # осознанное сужение объёма, см. Nedodelano.md) либо документ, для
    # которого хэш ещё не посчитан.
    sha256: str = ""
    # Происхождение приёма, когда документ пришёл через web-поиск (A5), а не
    # через ручную загрузку. Пусто = ручная загрузка. Не смешивается с
    # `meta_card` (та карта — про содержание документа: вид/эмитент/период;
    # эта — про то, ОТКУДА и КАК байты попали в систему).
    acquisition: dict[str, Any] = Field(default_factory=dict)


class Chunk(BaseModel):
    chunk_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    doc_id: str
    content: str
    chunk_type: str = "text"  # "text" or "table"
    page: int = 0
    section: str = ""
    speaker: str = ""  # for transcripts
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def source_ref(self) -> str:
        parts = [self.metadata.get("filename", self.doc_id)]
        if self.page:
            parts.append(f"стр. {self.page}")
        if self.section:
            parts.append(self.section)
        if self.speaker:
            parts.append(f"спикер: {self.speaker}")
        return " | ".join(parts)


class ExtractedItem(BaseModel):
    content: str | float
    metadata: dict[str, Any] = Field(default_factory=dict)
    score: float = 0.0
    chunk_type: str = "text"

    @property
    def source_ref(self) -> str:
        parts = []
        if filename := self.metadata.get("filename"):
            parts.append(filename)
        if page := self.metadata.get("page"):
            parts.append(f"стр. {page}")
        if speaker := self.metadata.get("speaker"):
            parts.append(f"спикер: {speaker}")
        return " | ".join(parts) if parts else "неизвестный источник"
