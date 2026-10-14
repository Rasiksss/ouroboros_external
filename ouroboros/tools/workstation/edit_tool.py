"""``file_edit`` — spec §19.3's ``edit``: a search/replace edit over arbitrary
content, not only code."""

from __future__ import annotations

from itertools import pairwise
from typing import Any

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.egress.workstation import WorkstationError
from ouroboros.tools.workstation._shared import render_error, unavailable_result

_SCHEMA = ToolSchema(
    name="file_edit",
    description=(
        "Edit a single file using exact text replacement. Every edits[].oldText must match "
        "a unique, non-overlapping region of the original file. If two changes affect the "
        "same block or nearby lines, merge them into one edit instead of emitting "
        "overlapping edits. Do not include large unchanged regions just to connect distant "
        "changes. Use file_edit for precise changes (edits[].oldText must match exactly). "
        "When changing multiple separate locations in one file, use one file_edit call with "
        "multiple entries in edits[] instead of multiple file_edit calls. Each edits[].oldText "
        "is matched against the original file, not after earlier edits are applied. Do not "
        "emit overlapping or nested edits. Merge nearby changes into one edit. Keep "
        "edits[].oldText as small as possible while still being unique in the file. Do not "
        "pad with large unchanged regions."
    ),
    parameters={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "path": {"type": "string", "description": "Path to the file to edit (relative or absolute)"},
            "edits": {
                "type": "array",
                "description": (
                    "One or more targeted replacements. Each edit is matched against the "
                    "original file, not incrementally. Do not include overlapping or nested "
                    "edits. If two changes touch the same block or nearby lines, merge them "
                    "into one edit instead."
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "oldText": {
                            "type": "string",
                            "description": (
                                "Exact text for one targeted replacement. It must be unique "
                                "in the original file and must not overlap with any other "
                                "edits[].oldText in the same call."
                            ),
                        },
                        "newText": {"type": "string", "description": "Replacement text for this targeted edit."},
                    },
                    "required": ["oldText", "newText"],
                },
            },
        },
        "required": ["path", "edits"],
    },
)

#: A 1:1 character remap for typographic quotes/dashes — this keeps every
#: found offset valid in the original string.
_LENIENT_TRANSLATION = str.maketrans(
    {
        "\u201c": '"',  # left double quotation mark
        "\u201d": '"',  # right double quotation mark
        "\u2018": "'",  # left single quotation mark
        "\u2019": "'",  # right single quotation mark
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
    }
)


def _find_all(content: str, needle: str) -> list[int]:
    if not needle:
        return []
    positions: list[int] = []
    start = 0
    while True:
        idx = content.find(needle, start)
        if idx == -1:
            break
        positions.append(idx)
        start = idx + 1
    return positions


def _locate(content: str, old_text: str) -> list[int]:
    positions = _find_all(content, old_text)
    if positions:
        return positions
    lenient_content = content.translate(_LENIENT_TRANSLATION)
    lenient_old = old_text.translate(_LENIENT_TRANSLATION)
    return _find_all(lenient_content, lenient_old)


def _apply_edits(content: str, edits: list[dict[str, Any]]) -> str:
    """Returns the new content, or raises ValueError describing the first problem."""
    spans: list[tuple[int, int, str]] = []
    for edit in edits:
        old_text = str(edit["oldText"])
        new_text = str(edit["newText"])
        positions = _locate(content, old_text)
        if not positions:
            raise ValueError(f"text not found: {old_text!r}")
        if len(positions) > 1:
            raise ValueError(f"oldText is not unique in the file ({len(positions)} occurrences): {old_text!r}")
        start = positions[0]
        spans.append((start, start + len(old_text), new_text))

    spans.sort(key=lambda span: span[0])
    for (_start1, end1, _text1), (start2, _end2, _text2) in pairwise(spans):
        if start2 < end1:
            raise ValueError("overlapping edits in the same call")

    pieces: list[str] = []
    cursor = 0
    for start, end, new_text in spans:
        pieces.append(content[cursor:start])
        pieces.append(new_text)
        cursor = end
    pieces.append(content[cursor:])
    return "".join(pieces)


def _handle_file_edit(ctx: ToolContext, path: str = "", edits: list[Any] | None = None, **_kwargs: Any) -> str:
    handle = ctx.workstation
    if handle is None:
        return unavailable_result("file_edit")
    if not str(path or "").strip():
        return "⚠️ FILE_EDIT_ERROR: 'path' is required."
    if not isinstance(edits, list) or not edits:
        return "⚠️ FILE_EDIT_ERROR: 'edits' must be a non-empty list of {oldText, newText}."
    for item in edits:
        if not isinstance(item, dict) or not item.get("oldText") or "newText" not in item:
            return "⚠️ FILE_EDIT_ERROR: each edits[] entry needs a non-empty 'oldText' and a 'newText'."

    try:
        original = handle.files.read_file(path)
    except WorkstationError as exc:
        return render_error("file_edit", exc)
    text = original.decode("utf-8", errors="replace")

    try:
        new_text = _apply_edits(text, edits)
    except ValueError as exc:
        return f"⚠️ FILE_EDIT_NOT_FOUND ({path}): {exc}\n\nCurrent file content:\n{text}"

    try:
        handle.files.write_file(path, new_text.encode("utf-8"), mime="text/plain; charset=utf-8")
    except WorkstationError as exc:
        return render_error("file_edit", exc)
    return f"OK: applied {len(edits)} edit(s) to {path}"


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="file_edit",
            schema=_SCHEMA,
            handler=_handle_file_edit,
            timeout_sec=60,
        ),
    ]


__all__ = ["get_tools"]
