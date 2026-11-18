"""Explicit, isolated parsing for the attachments a run may reach."""

from __future__ import annotations

import logging
import pathlib
import tempfile

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.contracts.attachments import AttachmentMetadata
from ouroboros.storage import attachment_validation
from ouroboros.storage.attachment_validation import AttachmentError
from ouroboros.storage.blob.factory import get_storage_provider
from ouroboros.storage.blob.protocol import ObjectTooLargeError
from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
from ouroboros.storage.file_catalog import FileCatalog, FileError
from ouroboros.storage.file_models import attachment_metadata
from ouroboros.storage.file_transfers import get_transfer_limits
from ouroboros.storage.runtime_engine import resolve_runtime_engine
from ouroboros.storage.settings.store import load_settings
from ouroboros.tools.catalog import spec_for_tool

log = logging.getLogger(__name__)


def resolve_reachable_attachment(
    ctx: ToolContext,
    attachment_id: str,
    *,
    allow_reference: bool = False,
    fallback_path: str = "",
) -> AttachmentMetadata | None:
    """Resolve through current message links or the run's explicitly granted attachments."""

    with resolve_runtime_engine("file reachability").connect() as conn:
        try:
            file = FileCatalog(conn, ctx.scope).resolve_reference(
                attachment_id,
                fallback_path=fallback_path,
                explicit_ids=[str(item["id"]) for item in (ctx.attachments or []) if "id" in item],
            )
        except FileError:
            return None
        if file is None or (not allow_reference and file.id != attachment_id):
            log.info("event=attachment_reach_refused task_id=%s reason=not_reachable", ctx.task_id)
            return None
        return attachment_metadata(file)


def _attachment_read(ctx: ToolContext, attachment_id: str) -> str:

    limit = load_settings().OUROBOROS_ATTACHMENT_DOCUMENT_MAX_BYTES
    provider = get_storage_provider()
    metadata = resolve_reachable_attachment(ctx, attachment_id)
    if metadata is None:
        return "Attachment metadata was not found."
    mime = str(metadata.get("mime") or "application/octet-stream").lower()
    if str(metadata.get("kind") or "") == "image" or mime.startswith("image/"):
        return "Image attachment. Analyze it explicitly with vlm_query using this attachment_id."
    storage_key = str(metadata.get("storage_key") or "")
    filename = str(metadata.get("filename") or "attachment")
    try:
        with tempfile.TemporaryDirectory(prefix="ouroboros-attachment-") as temp_dir:
            path = pathlib.Path(temp_dir) / pathlib.PurePosixPath(filename).name
            with get_transfer_limits().slot(ctx.scope):
                provider.copy_to_path(
                    ctx.scope,
                    SCOPE_UPLOADS,
                    storage_key,
                    path,
                    max_bytes=limit,
                )
            content = attachment_validation.run_parser(path, mime, mode="extract")
    except FileNotFoundError:
        return "Attachment content was not found."
    except ObjectTooLargeError:
        return f"Attachment is too large to parse. Max {limit} bytes."
    except FileError:
        return "⚠️ TOOL_ATTACHMENT_PARSER_UNAVAILABLE: retryable=true; transfer capacity exhausted"
    except AttachmentError as exc:
        return f"⚠️ TOOL_{exc.code.upper()}: retryable={str(exc.retryable).lower()}; Failed to parse attachment: {exc.reason}"
    return f"Attachment: {filename}\nMIME: {mime}\n\n{content}"


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="attachment_read",
            schema=ToolSchema(
                name="attachment_read",
                description="Read an accessible TXT, MD, CSV, JSON, PDF, XLSX, DOCX, or PPTX attachment. "
                "Images must be analyzed with vlm_query.",
                parameters={
                    "type": "object",
                    "properties": {
                        "attachment_id": {
                            "type": "string",
                            "description": "Attachment id from the current user message",
                        }
                    },
                    "required": ["attachment_id"],
                },
            ),
            handler=_attachment_read,
            timeout_sec=spec_for_tool("attachment_read").timeout_sec,
        )
    ]


__all__ = [
    "get_tools",
    "resolve_reachable_attachment",
]
