from __future__ import annotations
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field
import uuid


class DocType(str, Enum):
    FINANCIAL_REPORT = "financial_report"
    PRESS_RELEASE = "press_release"
    TRANSCRIPT = "transcript"
    PRESENTATION = "presentation"
    EQUITY_RESEARCH = "equity_research"
    EXCEL = "excel"
    OTHER = "other"

    @classmethod
    def _missing_(cls, value):
        # One-release input/pickle bridge for metadata created before the
        # standard and document-kind axes were separated.
        if isinstance(value, str) and value in {"ifrs_report", "rsbu_report"}:
            return cls.FINANCIAL_REPORT
        return None


class ProcessingMode(str, Enum):
    """Document-purpose mode that controls the available extraction tracks."""

    FINANCIAL_REPORTING = "financial_reporting"
    RESULTS_MATERIAL = "results_material"
    NARRATIVE = "narrative"

    @classmethod
    def _missing_(cls, value):
        # One-release bridge for metadata produced by classification v2.
        legacy = {
            "full": cls.FINANCIAL_REPORTING,
            "summary": cls.RESULTS_MATERIAL,
            "none": cls.NARRATIVE,
        }
        if isinstance(value, str):
            return legacy.get(value)
        return None


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
    # Which source produced the .txt sidecar. New PDFs always use "idp";
    # "text_file" is used for direct TXT/MD uploads. Other values are legacy.
    ocr_engine: str = ""
    # Owner (auth user id). 0 = legacy/unowned (only the bootstrap admin can see).
    owner_id: int = 0
    # How the document entered the system: "email" for files picked up from the
    # intake mailbox, "" for browser uploads / legacy docs.
    source: str = ""
    # Persisted top-level document/profile classification. A version of 0 means
    # a legacy document that should be classified lazily before extraction.
    classification_version: int = 0
    processing_mode: ProcessingMode = ProcessingMode.NARRATIVE
    profile_id: str = ""
    reporting_standard: str = ""
    classification_low_axes: list[str] = Field(default_factory=list)
    classification_reason: str = ""
    classification_fallback_used: bool = False


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
