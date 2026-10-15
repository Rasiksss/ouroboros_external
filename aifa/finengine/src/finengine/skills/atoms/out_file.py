"""``out.file``: render and persist an existing typed result.

The module deliberately has no access to OCR, registries, formulas or metric
calculators. It copies typed fields into a file and records immutable lineage.
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import textwrap
from collections.abc import Callable

from finengine.contracts import (
    FileArtifact,
    FormOutput,
    NarrativeOutput,
    ReportOutput,
    ResultReference,
    canonical_result_hash,
)
from finengine.skills import sources
from finengine.skills.spec.room_files import describe_file

ATOM_VERSION = "1.0.0"

SourceResult = ReportOutput | FormOutput | NarrativeOutput
ArtifactWriter = Callable[[str, bytes, str], str]

_MIME = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
    "csv": "text/csv",
    "json": "application/json",
}


def render_xlsx(source: SourceResult) -> bytes:
    """Copy values/status/provenance to cells; never derive a domain value."""
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    if isinstance(source, FormOutput):
        sheet.title = source.layout_id[:31]
        sheet.append(["row_id", "order", "label", "enum", "status", "value", "display", "evidence_ids"])
        for row in source.rows:
            sheet.append([
                row.row_id,
                row.order,
                row.label,
                row.enum or "",
                row.status,
                row.value,
                row.display_value or "",
                ",".join(ref.evidence_id for ref in row.evidence_refs),
            ])
    elif isinstance(source, ReportOutput):
        sheet.title = "Report"
        sheet.append(["section_id", "section", "thesis_id", "status", "text", "evidence_ids"])
        for section in source.sections:
            for thesis in section.theses:
                sheet.append([
                    section.section_id,
                    section.title,
                    thesis.thesis_id,
                    thesis.status,
                    thesis.text,
                    ",".join(ref.evidence_id for ref in thesis.evidence_refs),
                ])
    else:
        sheet.title = "Narrative"
        sheet.append(["status", "text", "evidence_ids"])
        sheet.append([source.status, source.text, ",".join(source.cited_evidence_ids)])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def render_csv(source: SourceResult) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    if isinstance(source, FormOutput):
        writer.writerow(["row_id", "order", "label", "enum", "status", "value", "display", "evidence_ids"])
        for row in source.rows:
            writer.writerow([
                row.row_id, row.order, row.label, row.enum or "", row.status,
                row.value, row.display_value or "", ",".join(ref.evidence_id for ref in row.evidence_refs),
            ])
    elif isinstance(source, ReportOutput):
        writer.writerow(["section_id", "section", "thesis_id", "status", "text", "evidence_ids"])
        for section in source.sections:
            for thesis in section.theses:
                writer.writerow([
                    section.section_id, section.title, thesis.thesis_id, thesis.status,
                    thesis.text, ",".join(ref.evidence_id for ref in thesis.evidence_refs),
                ])
    else:
        writer.writerow(["status", "text", "evidence_ids"])
        writer.writerow([source.status, source.text, ",".join(source.cited_evidence_ids)])
    return stream.getvalue().encode("utf-8-sig")


def render_pdf(source: SourceResult) -> bytes:
    """Plain presentation of supplied strings; no metric computation."""
    import pymupdf

    if isinstance(source, FormOutput):
        lines = [f"{row.order + 1}. {row.label}: {row.display_value or row.status}" for row in source.rows]
    elif isinstance(source, ReportOutput):
        lines = [
            f"{section.title}: {thesis.text}"
            for section in source.sections for thesis in section.theses
        ]
    else:
        lines = [source.text]
    visual_lines = [
        wrapped
        for line in lines
        for wrapped in (textwrap.wrap(line, width=110) or [""])
    ]
    document = pymupdf.open()
    for offset in range(0, len(visual_lines) or 1, 48):
        page = document.new_page()
        body = "".join(
            f"<div>{html.escape(line)}</div>"
            for line in visual_lines[offset:offset + 48]
        )
        page.insert_htmlbox(
            pymupdf.Rect(36, 36, page.rect.width - 36, page.rect.height - 36),
            f"<style>div {{ font-size: 9pt; line-height: 1.3; }}</style>{body}",
        )
    try:
        return document.tobytes()
    finally:
        document.close()


def render_typed_result(source: SourceResult, file_kind: str) -> bytes:
    if file_kind == "xlsx":
        return render_xlsx(source)
    if file_kind == "csv":
        return render_csv(source)
    if file_kind == "pdf":
        return render_pdf(source)
    if file_kind == "json":
        return json.dumps(source.model_dump(mode="json"), ensure_ascii=False, indent=2).encode("utf-8")
    raise ValueError(f"unsupported file kind: {file_kind!r}")


def _default_writer(ctx, filename: str, content: bytes, mime_type: str) -> str:
    owner_id = ctx.inputs.extra.get("owner_id") or "system"
    project_id = ctx.inputs.extra.get("project_id") or ctx.run_id
    _meta, object_key = sources.save_generated_bytes(
        owner_id, project_id, filename, content, mime_type,
    )
    return object_key


def build_file_artifact(
    *, source: SourceResult, file_kind: str, template_id: str, template_version: str,
    location: str, content: bytes, filename: str,
) -> FileArtifact:
    metadata = source.run_metadata
    return FileArtifact(
        run_id=source.run_id,
        file_kind=file_kind,
        mime_type=_MIME[file_kind],
        filename=filename,
        template_id=template_id,
        template_version=template_version,
        source_result=ResultReference(
            result_type=source.output_type,
            result_id=source.output_id,
            schema_version=source.schema_version,
            run_id=source.run_id,
            evidence_version=metadata.evidence_version if metadata else source.schema_version,
            result_hash=canonical_result_hash(source),
        ),
        content_hash=hashlib.sha256(content).hexdigest(),
        object_store_location=location,
        size_bytes=len(content),
        summary=describe_file(filename, content),
    )


def make_out_file_executor(writer: ArtifactWriter | None = None):
    def _executor(args, ctx) -> list[FileArtifact]:
        source = next((
            item for item in ctx.dependencies()
            if isinstance(item, (ReportOutput, FormOutput, NarrativeOutput))
        ), None)
        if source is None:
            raise ValueError("out.file: a typed source result is required")
        file_kind = args.extra.get("file_kind") or "xlsx"
        if file_kind not in _MIME:
            raise ValueError(f"out.file: unsupported file_kind={file_kind!r}")
        template_id = args.extra.get("template_id") or f"{source.output_type}.{file_kind}"
        template_version = args.extra.get("template_version") or "1.0"
        filename = args.extra.get("filename") or f"{source.output_id}.{file_kind}"
        content = render_typed_result(source, file_kind)
        location = (
            writer(filename, content, _MIME[file_kind])
            if writer is not None
            else _default_writer(ctx, filename, content, _MIME[file_kind])
        )
        return [build_file_artifact(
            source=source,
            file_kind=file_kind,
            template_id=template_id,
            template_version=template_version,
            location=location,
            content=content,
            filename=filename,
        )]
    return _executor


out_file_executor = make_out_file_executor()

__all__ = [
    "ATOM_VERSION", "render_xlsx", "render_csv", "render_pdf", "render_typed_result",
    "build_file_artifact", "make_out_file_executor", "out_file_executor",
]
