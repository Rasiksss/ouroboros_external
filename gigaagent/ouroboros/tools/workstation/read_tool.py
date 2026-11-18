"""``file_read`` — spec §19.3's ``read``: reads arbitrary content, not only code."""

from __future__ import annotations

from typing import Any

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.egress.workstation import WorkstationError
from ouroboros.tools.core import coerce_line_window, render_line_slice
from ouroboros.tools.workstation._shared import deliver_bytes, is_image_bytes, render_error, unavailable_result

_SCHEMA = ToolSchema(
    name="file_read",
    description=(
        "Read the contents of a file from the sandbox. After reading, the file is delivered "
        "to the user by its type: images via send_image, everything else "
        "via send_file. Use file_read to fetch and hand over a produced file."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read (relative or absolute)"},
        },
        "required": ["path"],
    },
)


def _render_listing(node: dict[str, Any]) -> str:
    children = node.get("children") if isinstance(node.get("children"), list) else []
    if not children:
        return f"# {node.get('path')} — directory, empty"
    lines = [f"# {node.get('path')} — directory listing"]
    for child in children:
        if not isinstance(child, dict):
            continue
        suffix = "/" if child.get("kind") == "dir" else ""
        lines.append(f"{child.get('path')}{suffix}")
    return "\n".join(lines)


def _render_text(path: str, data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    start_raw, max_raw = coerce_line_window(1, 2000)
    return render_line_slice(path, text, max_lines=max_raw, start_line=start_raw)


def _handle_file_read(ctx: ToolContext, path: str = "", **_kwargs: Any) -> str:
    handle = ctx.workstation
    if handle is None:
        return unavailable_result("file_read")
    if not str(path or "").strip():
        return "⚠️ FILE_READ_ERROR: 'path' is required."

    try:
        stat = handle.files.stat(path)
    except WorkstationError as exc:
        return render_error("file_read", exc)
    if stat is None:
        return f"⚠️ FILE_READ_ERROR: {path!r} not found."
    if stat.kind == "dir":
        try:
            node = handle.files.readdir(path)
        except WorkstationError as exc:
            return render_error("file_read", exc)
        return _render_listing(node)

    try:
        data = handle.files.read_file(path)
    except WorkstationError as exc:
        return render_error("file_read", exc)

    delivery = deliver_bytes(ctx, path.rsplit("/", 1)[-1] or path, data)
    if delivery is None:
        # No task workspace to deliver through: fall back to returning content so the
        # agent can still inspect text and text-only files.
        if is_image_bytes(data):
            return f"⚠️ FILE_READ_UNSUPPORTED: {path!r} is an image; no task workspace to deliver it through."
        return _render_text(path, data)
    return delivery


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="file_read",
            schema=_SCHEMA,
            handler=_handle_file_read,
            timeout_sec=60,
        ),
    ]


__all__ = ["get_tools"]
