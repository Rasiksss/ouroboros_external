"""Publish and materialize task artifacts with their file identity and provenance."""

from __future__ import annotations

import pathlib
import tempfile
import uuid
from typing import TypedDict

from pydantic import BaseModel, Field, ValidationError

from new_core.identity import OwnerScope
from new_core.tools.context import ToolContext
from ouroboros.contracts.attachments import AttachmentMetadata
from ouroboros.storage.attachment_validation import AttachmentError, attachment_max_bytes, attachment_mime_for_suffix
from ouroboros.storage.blob.drive import DriveStorage, get_drive_storage
from ouroboros.storage.blob.keys import safe_key_segment
from ouroboros.storage.blob.protocol import ObjectTooLargeError
from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
from ouroboros.storage.chat_attachments import store_attachment_fileobj
from ouroboros.storage.file_catalog import FileCatalog
from ouroboros.storage.file_transfers import get_transfer_limits
from ouroboros.storage.runtime_engine import resolve_runtime_engine
from ouroboros.tools.attachments import resolve_reachable_attachment
from ouroboros.utils import file_sha256, utc_now_iso

PROVENANCE_SUFFIX = ".metadata.json"


class ArtifactProvenance(TypedDict):
    schema_version: int
    origin: str
    purpose: str
    context_eligible: bool
    authoritative: bool
    task_id: str
    chat_id: str
    created_at: str
    created_by_tool: str


class WorkspaceOrigin(BaseModel):
    file_id: str
    fingerprint: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")


def workspace_source(ctx: ToolContext, path: pathlib.Path) -> AttachmentMetadata | None:
    try:
        origin = WorkspaceOrigin.model_validate_json(provenance_path(path).read_text())
    except FileNotFoundError:
        return None
    except ValidationError as error:
        raise AttachmentError(
            "attachment_not_found", "Stored source file is unavailable", filename=path.name
        ) from error
    file = resolve_artifact(ctx, origin.file_id)
    fingerprint = file_sha256(path)
    if file is None:
        if origin.fingerprint and fingerprint != origin.fingerprint:
            return None
        raise AttachmentError("attachment_not_found", "Stored source file is unavailable", filename=path.name)
    if fingerprint == file["fingerprint"]:
        return file
    return None


def provenance_path(path: pathlib.Path) -> pathlib.Path:
    return path.with_name(path.name + PROVENANCE_SUFFIX)


def record_workspace_source(path: pathlib.Path, file_id: str) -> None:
    """Atomically replace local provenance without following a destination symlink."""
    with tempfile.TemporaryDirectory(prefix=".file-origin-", dir=path.parent) as directory:
        staged = pathlib.Path(directory) / "metadata.json"
        staged.write_text(WorkspaceOrigin(file_id=file_id, fingerprint=file_sha256(path)).model_dump_json())
        staged.replace(provenance_path(path))


def _artifact_purpose(rel: str) -> str:
    norm = str(rel or "").replace("\\", "/").lstrip("./")
    if norm.startswith("state/agent_notes/"):
        return "agent_scratch"
    if norm.startswith("skills/"):
        return "skill_payload"
    return "artifact"


def provenance_payload(ctx: ToolContext, rel: str, *, created_by_tool: str) -> ArtifactProvenance:
    """The sidecar's contents. One builder, so ``data_write`` and ``publish`` cannot drift."""
    return {
        "schema_version": 1,
        "origin": "agent_generated",
        "purpose": _artifact_purpose(rel),
        "context_eligible": False,
        "authoritative": False,
        "task_id": str(ctx.task_id or ""),
        "chat_id": ctx.scope.chat_id,
        "created_at": utc_now_iso(),
        "created_by_tool": created_by_tool,
    }


def write_provenance(
    ctx: ToolContext,
    drive: DriveStorage,
    path: pathlib.Path,
    rel: str,
    *,
    created_by_tool: str,
) -> None:
    drive.atomic_write_json(
        ctx.scope, provenance_path(path), dict(provenance_payload(ctx, rel, created_by_tool=created_by_tool))
    )


def task_artifact_prefix(task_id: str) -> str:
    """Where one task's published artifacts live, the shape tool receipts already write."""
    return f"task_artifacts/{safe_key_segment(task_id, 'task')}"


def normalize_artifact_reference(reference: str, drive_root: pathlib.Path) -> str | None:

    path = pathlib.Path(reference).expanduser()
    if path.is_absolute():
        try:
            reference = path.resolve().relative_to(drive_root.resolve()).as_posix()
        except ValueError:
            return None
    reference = reference.removeprefix("uploads/")
    if reference.startswith("chat/") and len(reference.split("/")) == 2:
        reference = reference.split("/", 1)[1]
    return reference


def resolve_artifact(ctx: ToolContext, reference: str) -> AttachmentMetadata | None:
    normalized = normalize_artifact_reference(reference, pathlib.Path(ctx.drive_root))
    if normalized is None:
        return None
    return resolve_reachable_attachment(
        ctx,
        normalized,
        allow_reference=True,
        fallback_path=f"{task_artifact_prefix(ctx.task_id)}/{normalized}" if ctx.task_id else "",
    )


def publish(
    ctx: ToolContext,
    owner_scope: OwnerScope,
    root: pathlib.Path,
    paths: list[pathlib.Path],
) -> list[str]:
    """Publish immutable files and task-local aliases after validating every path."""

    if not paths:
        return []
    task_id = str(ctx.task_id or "").strip()
    if not task_id:
        raise ValueError("artifacts are published by a task; this call has none")
    drive = get_drive_storage(ctx.drive_root)
    prefix = task_artifact_prefix(task_id)
    keys: list[str] = []
    for path in paths:
        name = path.relative_to(root).as_posix()
        # A script-authored sidecar would let the agent mark any artifact authoritative and context-eligible.
        if name.lower().endswith(PROVENANCE_SUFFIX):
            raise ValueError(f"{name}: artifact provenance is runtime-managed")
        size = path.stat().st_size
        limit = attachment_max_bytes(attachment_mime_for_suffix(name))
        if size > limit:
            raise ValueError(f"{name} is {size} bytes, over the {limit} bytes limit")
        keys.append(f"{prefix}/{name}")
    published: list[str] = []
    for path, key in zip(paths, keys, strict=True):
        rel = f"{SCOPE_UPLOADS}/{key}"
        with path.open("rb") as fileobj:
            store_attachment_fileobj(
                owner_scope,
                drive.provider,
                attachment_id=uuid.uuid4().hex,
                filename=path.name,
                mime=attachment_mime_for_suffix(path.name) or "application/octet-stream",
                fileobj=fileobj,
                size=path.stat().st_size,
                status="ready",
                source="agent_generated",
                producer_task=task_id,
                artifact_path=key,
            )
        published.append(rel)
    return published


def materialize(ctx: ToolContext, drive_rel: str, into: pathlib.Path) -> tuple[pathlib.Path | None, str]:
    """Materialize an authorized file, refusing collisions and retaining its identity."""

    try:
        path = pathlib.Path(drive_rel).expanduser()
    except RuntimeError:
        return None, ""
    if not (
        drive_rel.startswith("uploads/")
        or (path.is_absolute() and path.resolve().is_relative_to((pathlib.Path(ctx.drive_root) / "uploads").resolve()))
    ):
        return None, ""
    metadata = resolve_artifact(ctx, drive_rel)
    if metadata is None:
        return None, "⚠️ Stored file is unavailable."
    target = into / pathlib.PurePosixPath(metadata["filename"]).name
    if target.exists():
        return None, f"⚠️ {drive_rel} shares the name {target.name} with another input; inputs need distinct names."
    try:
        with get_transfer_limits().slot(ctx.scope):
            get_drive_storage(ctx.drive_root).provider.copy_to_path(
                ctx.scope,
                SCOPE_UPLOADS,
                metadata["storage_key"],
                target,
                max_bytes=attachment_max_bytes(metadata["mime"]),
            )
    except (OSError, ObjectTooLargeError) as error:
        return None, f"⚠️ Stored file is temporarily unavailable: {type(error).__name__}"
    if not metadata["fingerprint"]:
        fingerprint = file_sha256(target)
        with resolve_runtime_engine("materialize file").begin() as conn:
            FileCatalog(conn, ctx.scope).record_fingerprint(metadata["id"], fingerprint)
    record_workspace_source(target, metadata["id"])
    return target, ""
