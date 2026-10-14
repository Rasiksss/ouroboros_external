"""Shared plumbing for the five WorkStation-backed tools (spec §19.3)."""

from __future__ import annotations

import pathlib
import shutil

from new_core.tools.context import ToolContext
from ouroboros.egress.workstation import WorkstationAccessDenied, WorkstationError


def unavailable_result(tool_name: str) -> str:
    return f"⚠️ TOOL_UNAVAILABLE ({tool_name}): no WorkStation session is bound to this turn."


def render_error(tool_name: str, exc: WorkstationError) -> str:
    if isinstance(exc, WorkstationAccessDenied):
        return f"⚠️ ACCESS_DENIED ({tool_name}): {exc}"
    return f"⚠️ WORKSTATION_ERROR ({tool_name}): {exc}"


_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"\xff\xd8", "JPEG"),
    (b"GIF8", "GIF"),
)


def is_image_bytes(data: bytes) -> bool:
    """True when the first bytes match a known image magic (PNG/JPEG/GIF/WEBP)."""
    for magic, _label in _IMAGE_MAGIC:
        if data.startswith(magic):
            return True
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def deliver_bytes(ctx: ToolContext, filename: str, data: bytes, caption: str = "") -> str | None:
    """Deliver sandbox bytes to the user by MIME: images via send_image, others via send_file.

    Stages the bytes into the current task workspace so the messaging senders can range over them. Returns
    ``None`` when there is no task workspace to stage into (caller falls back to returning content).
    """
    import uuid

    from ouroboros.tools.service.messaging import _send_file, _send_image

    workspace = ctx.task_workspace
    if workspace is None:
        return None
    safe_name = pathlib.PurePosixPath(str(filename or "file")).name or "file"
    staged = pathlib.Path(workspace).resolve() / ".workstation" / uuid.uuid4().hex / safe_name
    staged.parent.mkdir(parents=True, exist_ok=True)
    try:
        staged.write_bytes(data)
    except OSError as exc:
        return f"⚠️ FILE_DELIVERY_ERROR: failed to stage {safe_name!r}: {exc}"
    try:
        if is_image_bytes(data):
            return _send_image(ctx, file_path=str(staged), caption=caption)
        return _send_file(ctx, file_path=str(staged), caption=caption)
    finally:
        shutil.rmtree(staged.parent, ignore_errors=True)


__all__ = ["deliver_bytes", "is_image_bytes", "render_error", "unavailable_result"]
