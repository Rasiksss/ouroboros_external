"""Versioned final-result contracts (A11 / Slice 12).

These models are the boundary between domain execution and presentation.
Renderers may format supplied values, but must not read OCR or derive financial
values. Legacy v0 payloads are accepted on input; serialization emits v1.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from .base import Contract, NonEmptyStr, new_id, utc_now

RESULT_SCHEMA_VERSION = "1.0"

NumericClaimKind = Literal["financial", "date", "page_number", "other_id", "narrative"]
ResultStatus = Literal["complete", "partial", "blocked"]
EvidenceStatus = Literal["supported", "missing", "conflicting", "not_applicable", "error"]
GateStatus = Literal["pass", "fail", "not_applicable"]
# Формат файла — расширение без точки: платформа не привязана к списку форматов (xlsx, pptx, docx, ...),
# исполняемые отдаются только в виде явного отказа.
FileKind = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]{1,8}$")]
EXECUTABLE_FILE_KINDS = frozenset({
    "exe", "bat", "cmd", "com", "scr", "msi", "dll", "sh", "ps1", "vbs", "js", "jar", "app", "apk", "bin",
})
ResultType = Literal["ReportOutput", "FormOutput", "NarrativeOutput", "DynamicArtifact"]


class NumericClaimRecord(Contract):
    text: NonEmptyStr
    kind: NumericClaimKind
    value: float | None = None
    period_ref: str | None = None
    unit: str | None = None
    evidence_id: str | None = None


class EvidenceRef(Contract):
    """Minimal provenance needed by UI/export without reopening source OCR."""

    evidence_id: NonEmptyStr
    concept: str | None = None
    period_ref: str | None = None
    source_ref: str | None = None
    document_version: str | None = None


class OutputWarning(Contract):
    code: NonEmptyStr
    message: NonEmptyStr
    severity: Literal["info", "warning", "error"] = "warning"
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)


class OutputGap(Contract):
    code: NonEmptyStr
    message: NonEmptyStr
    concept: str | None = None
    required: bool = True


class PublicationChecks(Contract):
    grounding: GateStatus = "not_applicable"
    citation: GateStatus = "not_applicable"


class OutputRunMetadata(Contract):
    run_id: NonEmptyStr
    goal_ir_id: str | None = None
    entity_id: str | None = None
    registry_version: str | None = None
    methodology_version: str | None = None
    model_version: str | None = None
    prompt_version: str | None = None
    sandbox_image_version: str | None = None
    plan_graph_hash: str | None = None
    evidence_version: NonEmptyStr = RESULT_SCHEMA_VERSION
    produced_at: datetime = Field(default_factory=utc_now)

    @classmethod
    def from_passport(
        cls, run_id: str, passport: Any | None, *, plan_graph_hash: str | None = None,
    ) -> "OutputRunMetadata":
        if passport is None:
            return cls(run_id=run_id, plan_graph_hash=plan_graph_hash)
        entity = getattr(passport, "entity", None)
        entity_id = (
            getattr(entity, "entity_id", None)
            or getattr(entity, "canonical_id", None)
            or getattr(entity, "canonical_name", None)
        )
        return cls(
            run_id=run_id,
            goal_ir_id=getattr(passport, "goal_ir_id", None),
            entity_id=entity_id,
            registry_version=getattr(passport, "registry_version", None),
            methodology_version=getattr(passport, "methodology_version", None),
            model_version=getattr(passport, "model_version", None),
            prompt_version=getattr(passport, "prompt_version", None),
            sandbox_image_version=getattr(passport, "sandbox_image_version", None),
            plan_graph_hash=plan_graph_hash,
        )


class ReportThesis(Contract):
    thesis_id: str = Field(default_factory=new_id)
    text: NonEmptyStr
    status: EvidenceStatus = "supported"
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    claims: list[NumericClaimRecord] = Field(default_factory=list)


class ReportSection(Contract):
    section_id: NonEmptyStr
    title: NonEmptyStr
    status: EvidenceStatus = "supported"
    theses: list[ReportThesis] = Field(default_factory=list)


class ReportOutput(Contract):
    schema_version: Literal["1.0"] = RESULT_SCHEMA_VERSION
    output_type: Literal["ReportOutput"] = "ReportOutput"
    output_id: str = Field(default_factory=new_id)
    run_id: NonEmptyStr
    status: ResultStatus = "complete"
    sections: list[ReportSection]
    gaps: list[OutputGap] = Field(default_factory=list)
    warnings: list[OutputWarning] = Field(default_factory=list)
    publication_checks: PublicationChecks = Field(default_factory=PublicationChecks)
    run_metadata: OutputRunMetadata | None = None

    @model_validator(mode="before")
    @classmethod
    def _upgrade_v0(cls, data: Any) -> Any:
        """Read Phase-0 ``sections: list[dict]`` without exposing v0 to UI."""
        if not isinstance(data, dict):
            return data
        out = dict(data)
        sections = []
        for index, raw in enumerate(out.get("sections") or []):
            if isinstance(raw, ReportSection):
                sections.append(raw)
                continue
            if not isinstance(raw, dict):
                continue
            if "theses" in raw:
                sections.append(raw)
                continue
            body = str(raw.get("body") or raw.get("text") or "").strip()
            title = str(raw.get("title") or f"Раздел {index + 1}").strip()
            sections.append({
                "section_id": str(raw.get("section_id") or f"legacy-{index}"),
                "title": title,
                "status": "supported" if body else "missing",
                "theses": ([{"text": body}] if body else []),
            })
        out["sections"] = sections
        out.setdefault("schema_version", RESULT_SCHEMA_VERSION)
        out.setdefault("output_type", "ReportOutput")
        return out


class FormRow(Contract):
    row_id: NonEmptyStr
    order: int = Field(ge=0)
    kind: NonEmptyStr
    enum: str | None = None
    label: NonEmptyStr
    value: float | str | None = None
    display_value: str | None = None
    status: EvidenceStatus
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    missing_reason: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _upgrade_v0(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        has_legacy_available = "available" in out
        available = out.pop("available", None)
        enum = str(out.get("enum") or "").strip() or None
        out["enum"] = enum
        digest = hashlib.sha256(str(out.get("label", "")).encode()).hexdigest()[:12]
        out.setdefault("row_id", f"legacy:{enum or digest}")
        out.setdefault("order", 0)
        if "status" not in out:
            out["status"] = "supported" if (available is not False and out.get("value") is not None) else "missing"
        if out["status"] == "missing" and has_legacy_available:
            out.setdefault("missing_reason", "value_not_available")
        return out

    @property
    def available(self) -> bool:
        """Read-only v0 compatibility; canonical wire format uses ``status``."""
        return self.status == "supported"

    @model_validator(mode="after")
    def _status_matches_value(self):
        if self.status in {"missing", "not_applicable", "error"} and self.value is not None:
            raise ValueError(f"row with status={self.status!r} cannot carry a value")
        if self.status == "supported" and self.value is None:
            raise ValueError("supported row must carry a value")
        if self.status == "missing" and not self.missing_reason:
            raise ValueError("missing row must explain why the value is absent")
        return self


class FormOutput(Contract):
    schema_version: Literal["1.0"] = RESULT_SCHEMA_VERSION
    output_type: Literal["FormOutput"] = "FormOutput"
    output_id: str = Field(default_factory=new_id)
    run_id: NonEmptyStr
    status: ResultStatus = "complete"
    role: NonEmptyStr
    layout_id: NonEmptyStr = "legacy"
    layout_version: NonEmptyStr = "0"
    preserve_empty: bool = False
    rows: list[FormRow]
    gaps: list[OutputGap] = Field(default_factory=list)
    warnings: list[OutputWarning] = Field(default_factory=list)
    run_metadata: OutputRunMetadata | None = None

    @model_validator(mode="before")
    @classmethod
    def _upgrade_v0(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        out.setdefault("schema_version", RESULT_SCHEMA_VERSION)
        out.setdefault("output_type", "FormOutput")
        rows = []
        for index, row in enumerate(out.get("rows") or []):
            if isinstance(row, FormRow):
                rows.append(
                    row.model_copy(update={"order": index})
                    if row.row_id.startswith("legacy:")
                    else row
                )
            elif isinstance(row, dict):
                rows.append({**row, "order": row.get("order", index)})
        out["rows"] = rows
        return out

    @model_validator(mode="after")
    def _ordered_once(self):
        orders = [row.order for row in self.rows]
        if orders != list(range(len(self.rows))):
            raise ValueError("form rows must be present exactly once in contiguous layout order")
        if len({row.row_id for row in self.rows}) != len(self.rows):
            raise ValueError("form row_id values must be unique")
        return self


class NarrativeOutput(Contract):
    schema_version: Literal["1.0"] = RESULT_SCHEMA_VERSION
    output_type: Literal["NarrativeOutput"] = "NarrativeOutput"
    output_id: str = Field(default_factory=new_id)
    run_id: NonEmptyStr
    status: ResultStatus = "complete"
    text: NonEmptyStr
    cited_evidence_ids: list[str] = Field(default_factory=list)
    claims: list[NumericClaimRecord] = Field(default_factory=list)
    gaps: list[OutputGap] = Field(default_factory=list)
    warnings: list[OutputWarning] = Field(default_factory=list)
    run_metadata: OutputRunMetadata | None = None


ChangeStatus = Literal["computed", "zero_base", "no_prior_period", "not_comparable"]
ExplanationStatus = Literal["ready", "unavailable"]


class PeriodChangeRow(Contract):
    """Сравнение одного показателя между двумя периодами ВНУТРИ одного отчёта.

    Числа посчитаны кодом (`out.changes`) из уже проверенных фактов: ни модель,
    ни клиент проценты не считают. `computed` — оба значения и процент;
    `zero_base` — предыдущее значение 0: абсолютная разница есть, процента нет;
    `no_prior_period`/`not_comparable` — сравнения нет, причина в `reason`."""

    row_id: NonEmptyStr
    order: int = Field(ge=0)
    concept: NonEmptyStr
    label: NonEmptyStr
    status: ChangeStatus
    unit: str | None = None
    current_period_ref: str | None = None
    prior_period_ref: str | None = None
    current_value: float | None = None
    prior_value: float | None = None
    absolute_change: float | None = None
    relative_change_pct: float | None = None
    reason: str | None = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _status_matches_numbers(self):
        compared = self.status in {"computed", "zero_base"}
        if compared:
            missing = [
                name for name in (
                    "current_value", "prior_value", "absolute_change",
                    "current_period_ref", "prior_period_ref",
                ) if getattr(self, name) is None
            ]
            if missing or len(self.evidence_refs) < 2:
                raise ValueError(
                    f"row with status={self.status!r} requires both values, both periods, "
                    f"the change and two evidence refs (missing: {missing})"
                )
            if self.status == "computed" and self.relative_change_pct is None:
                raise ValueError("row with status='computed' requires relative_change_pct")
            if self.status == "zero_base" and self.relative_change_pct is not None:
                raise ValueError("row with status='zero_base' cannot carry a percentage")
        else:
            if self.absolute_change is not None or self.relative_change_pct is not None:
                raise ValueError(f"row with status={self.status!r} cannot carry a change")
            if not (self.reason or "").strip():
                raise ValueError(f"row with status={self.status!r} requires a reason")
        return self


class PeriodChangeReport(Contract):
    """Итог MONITOR_CHANGES по одному документу: таблица изменений между периодами
    (детерминированная) и объяснение модели (best-effort).

    Таблица не зависит от объяснения: текст заблокирован проверкой опоры или модель
    ничего не вернула → `explanation_status="unavailable"` с причиной, таблица остаётся."""

    schema_version: Literal["1.0"] = RESULT_SCHEMA_VERSION
    output_type: Literal["PeriodChangeReport"] = "PeriodChangeReport"
    output_id: str = Field(default_factory=new_id)
    run_id: NonEmptyStr
    status: ResultStatus = "complete"
    role: NonEmptyStr
    rows: list[PeriodChangeRow]
    explanation: NarrativeOutput | None = None
    explanation_status: ExplanationStatus
    explanation_note: str | None = None
    gaps: list[OutputGap] = Field(default_factory=list)
    warnings: list[OutputWarning] = Field(default_factory=list)
    run_metadata: OutputRunMetadata | None = None

    @model_validator(mode="after")
    def _consistent(self):
        if [row.order for row in self.rows] != list(range(len(self.rows))):
            raise ValueError("change rows must be present exactly once in contiguous order")
        if self.explanation_status == "ready" and self.explanation is None:
            raise ValueError("explanation_status='ready' requires an explanation")
        if self.explanation_status == "unavailable":
            if self.explanation is not None:
                raise ValueError("explanation_status='unavailable' cannot carry an explanation")
            if not (self.explanation_note or "").strip():
                raise ValueError("explanation_status='unavailable' requires explanation_note")
        return self


class ResultReference(Contract):
    result_type: ResultType
    result_id: NonEmptyStr
    schema_version: NonEmptyStr
    run_id: NonEmptyStr
    evidence_version: NonEmptyStr
    result_hash: NonEmptyStr


class FileArtifact(Contract):
    schema_version: Literal["1.0"] = RESULT_SCHEMA_VERSION
    output_type: Literal["FileArtifact"] = "FileArtifact"
    artifact_id: str = Field(default_factory=new_id)
    run_id: NonEmptyStr
    file_kind: FileKind
    mime_type: NonEmptyStr
    filename: NonEmptyStr
    template_id: NonEmptyStr
    template_version: NonEmptyStr
    source_result: ResultReference
    content_hash: NonEmptyStr
    object_store_location: NonEmptyStr
    size_bytes: int = Field(ge=0)
    # Краткое описание структуры файла (листы, слайды, ...): его видит планировщик цикла.
    summary: str = ""
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("file_kind")
    @classmethod
    def _not_executable(cls, value: str) -> str:
        if value in EXECUTABLE_FILE_KINDS:
            raise ValueError(f"file_kind {value!r}: исполняемые файлы не выдаются")
        return value

    @model_validator(mode="before")
    @classmethod
    def _upgrade_v0(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "file_kind" in data:
            return data
        out = dict(data)
        kind = str(out.pop("kind", "json"))
        storage = str(out.pop("storage_ref", "legacy://unknown"))
        checksum = str(out.pop("checksum", "") or hashlib.sha256(b"").hexdigest())
        run_id = str(out.get("run_id") or "legacy")
        out.update({
            "schema_version": RESULT_SCHEMA_VERSION,
            "output_type": "FileArtifact",
            "file_kind": kind if kind in {"xlsx", "pdf", "csv", "json"} else "json",
            "mime_type": "application/octet-stream",
            "filename": f"result.{kind}",
            "template_id": "legacy",
            "template_version": "0",
            "source_result": {
                "result_type": "NarrativeOutput", "result_id": "legacy", "schema_version": "0",
                "run_id": run_id, "evidence_version": "0", "result_hash": checksum,
            },
            "content_hash": checksum,
            "object_store_location": storage,
            "size_bytes": 0,
        })
        return out

    @field_validator("content_hash")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value.lower()):
            raise ValueError("content_hash must be a hexadecimal sha256")
        return value.lower()

    @property
    def kind(self) -> str:
        return self.file_kind

    @property
    def storage_ref(self) -> str:
        return self.object_store_location

    @property
    def checksum(self) -> str:
        return self.content_hash


TypedSourceOutput = ReportOutput | FormOutput | NarrativeOutput


def canonical_result_hash(result: TypedSourceOutput) -> str:
    payload = json.dumps(
        result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "RESULT_SCHEMA_VERSION", "NumericClaimKind", "NumericClaimRecord", "EvidenceStatus",
    "EvidenceRef", "OutputWarning", "OutputGap", "PublicationChecks", "OutputRunMetadata",
    "ReportThesis", "ReportSection", "ReportOutput", "FormRow", "FormOutput", "NarrativeOutput",
    "ResultReference", "FileArtifact", "FileKind", "EXECUTABLE_FILE_KINDS", "ResultType", "TypedSourceOutput",
    "canonical_result_hash",
]
