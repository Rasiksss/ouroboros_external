"""``apply_patch`` — spec §19.3.

The format is this integration's own; spec §19.3 requires it to be fully
documented since there is nothing else the model could read it from, so the
schema description below spells it out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.egress.workstation import WorkstationError, WorkstationHandle
from ouroboros.tools.workstation._shared import render_error, unavailable_result
from ouroboros.tools.workstation.edit_tool import _locate

_SCHEMA = ToolSchema(
    name="apply_patch",
    description=(
        "Apply a patch to one or more files using the apply_patch format. The input must "
        "start with the line '*** Begin Patch' and end with '*** End Patch'. Between them, "
        "one section per affected file: '*** Add File: <path>' followed by the full new "
        "content with every line prefixed with '+'; '*** Delete File: <path>' with no body; "
        "or '*** Update File: <path>', optionally followed by '*** Move to: <new path>' to "
        "rename/move it, then one or more hunks. A hunk may start with an optional "
        "'@@ ...' context marker line and is a sequence of lines prefixed with ' ' "
        "(unchanged context), '-' (removed), or '+' (added). Every hunk's context/removed "
        "lines must match a unique region of the file exactly, or the whole call fails with "
        "no file changed."
    ),
    parameters={
        "type": "object",
        "properties": {
            "input": {
                "type": "string",
                "description": "Patch content using the *** Begin Patch/End Patch format.",
            },
        },
        "required": ["input"],
    },
)

_BEGIN = "*** Begin Patch"
_END = "*** End Patch"
_ADD_PREFIX = "*** Add File: "
_DELETE_PREFIX = "*** Delete File: "
_UPDATE_PREFIX = "*** Update File: "
_MOVE_PREFIX = "*** Move to: "


@dataclass
class _AddOp:
    path: str
    content: str


@dataclass
class _DeleteOp:
    path: str


@dataclass
class _UpdateOp:
    path: str
    new_path: str | None
    hunks: list[list[str]]


def _parse_patch(input_text: str) -> list[_AddOp | _DeleteOp | _UpdateOp]:
    lines = input_text.splitlines()
    if not lines or lines[0].strip() != _BEGIN:
        raise ValueError(f"patch must start with {_BEGIN!r}")
    if lines[-1].strip() != _END:
        raise ValueError(f"patch must end with {_END!r}")
    body = lines[1:-1]

    ops: list[_AddOp | _DeleteOp | _UpdateOp] = []
    i = 0
    while i < len(body):
        line = body[i]
        if line.startswith(_ADD_PREFIX):
            path = line[len(_ADD_PREFIX) :].strip()
            i += 1
            content_lines: list[str] = []
            while i < len(body) and not body[i].startswith("*** "):
                raw = body[i]
                if not raw.startswith("+"):
                    raise ValueError(f"Add File {path!r}: expected a '+'-prefixed line, got {raw!r}")
                content_lines.append(raw[1:])
                i += 1
            ops.append(_AddOp(path=path, content="\n".join(content_lines)))
        elif line.startswith(_DELETE_PREFIX):
            ops.append(_DeleteOp(path=line[len(_DELETE_PREFIX) :].strip()))
            i += 1
        elif line.startswith(_UPDATE_PREFIX):
            path = line[len(_UPDATE_PREFIX) :].strip()
            i += 1
            new_path: str | None = None
            if i < len(body) and body[i].startswith(_MOVE_PREFIX):
                new_path = body[i][len(_MOVE_PREFIX) :].strip()
                i += 1
            hunks: list[list[str]] = []
            current_hunk: list[str] | None = None
            while i < len(body) and not body[i].startswith("*** "):
                raw = body[i]
                if raw.startswith("@@"):
                    if current_hunk is not None:
                        hunks.append(current_hunk)
                    current_hunk = []
                    i += 1
                    continue
                if current_hunk is None:
                    current_hunk = []
                if not raw or raw[0] not in " +-":
                    raise ValueError(f"Update File {path!r}: hunk line must start with ' ', '+' or '-': {raw!r}")
                current_hunk.append(raw)
                i += 1
            if current_hunk is not None and current_hunk:
                hunks.append(current_hunk)
            if not hunks:
                raise ValueError(f"Update File {path!r}: no hunks found")
            ops.append(_UpdateOp(path=path, new_path=new_path, hunks=hunks))
        elif not line.strip():
            i += 1
        else:
            raise ValueError(f"unrecognized patch line: {line!r}")
    return ops


def _apply_hunk(content: str, hunk: list[str], path: str) -> str:
    old_lines = [line[1:] for line in hunk if line[0] in " -"]
    new_lines = [line[1:] for line in hunk if line[0] in " +"]
    old_block = "\n".join(old_lines)
    if not old_block:
        raise ValueError(f"{path}: hunk has no context/removed lines to anchor it")
    positions = _locate(content, old_block)
    if not positions:
        raise ValueError(f"{path}: hunk context not found: {old_block!r}")
    if len(positions) > 1:
        raise ValueError(f"{path}: hunk context is not unique ({len(positions)} occurrences): {old_block!r}")
    start = positions[0]
    end = start + len(old_block)
    return content[:start] + "\n".join(new_lines) + content[end:]


def _compute_update(handle: WorkstationHandle, op: _UpdateOp) -> tuple[str, str]:
    try:
        original = handle.files.read_file(op.path)
    except WorkstationError as exc:
        raise ValueError(f"{op.path}: could not read for update: {exc}") from exc
    content = original.decode("utf-8", errors="replace")
    for hunk in op.hunks:
        content = _apply_hunk(content, hunk, op.path)
    return op.new_path or op.path, content


def _handle_apply_patch(ctx: ToolContext, input: str = "", **_kwargs: Any) -> str:
    handle = ctx.workstation
    if handle is None:
        return unavailable_result("apply_patch")
    if not str(input or "").strip():
        return "⚠️ APPLY_PATCH_ERROR: 'input' is required."

    try:
        ops = _parse_patch(input)
    except ValueError as exc:
        return f"⚠️ APPLY_PATCH_PARSE_ERROR: {exc}"
    if not ops:
        return "⚠️ APPLY_PATCH_ERROR: patch contains no file operations."

    # Phase 1: compute every operation's outcome (including reading current
    # content for updates) without writing anything — a mismatch anywhere
    # must fail the whole call with no file changed (spec §19.3).
    planned: list[tuple[Any, ...]] = []
    try:
        for op in ops:
            if isinstance(op, _AddOp):
                planned.append(("add", op.path, op.content))
            elif isinstance(op, _DeleteOp):
                planned.append(("delete", op.path))
            else:
                final_path, content = _compute_update(handle, op)
                planned.append(("update", op.path, final_path, content))
    except (ValueError, WorkstationError) as exc:
        return f"⚠️ APPLY_PATCH_FAILED: {exc}"

    # Phase 2: apply. A failure here is a protocol-level surprise (e.g. the
    # session died mid-call), not a content mismatch — surfaced as-is.
    added = deleted = updated = 0
    try:
        for entry in planned:
            kind = entry[0]
            if kind == "add":
                _, path, content = entry
                handle.files.write_file(path, content.encode("utf-8"), mime="text/plain; charset=utf-8")
                added += 1
            elif kind == "delete":
                _, path = entry
                handle.files.remove(path)
                deleted += 1
            else:
                _, old_path, new_path, content = entry
                handle.files.write_file(new_path, content.encode("utf-8"), mime="text/plain; charset=utf-8")
                if new_path != old_path:
                    handle.files.remove(old_path)
                updated += 1
    except WorkstationError as exc:
        return render_error("apply_patch", exc)

    return f"OK: {added} added, {updated} updated, {deleted} deleted"


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="apply_patch",
            schema=_SCHEMA,
            handler=_handle_apply_patch,
            timeout_sec=120,
        ),
    ]


__all__ = ["get_tools"]
