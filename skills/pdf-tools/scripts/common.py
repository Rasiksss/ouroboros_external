"""Shared helpers for the pdf-tools scripts; a skill subprocess cannot import project code, so each package carries its own copy."""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import NoReturn

# Resource bounds for this standalone script, which cannot import the runtime.
# Runtime attachment limits are configured separately in docs/SETTINGS.md.
MAX_INPUT_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_CHARS = 100_000
TRUNCATION_MARKER = "\n[TRUNCATED]"

_ORDERED_RE = re.compile(r"^(\d+)[.)]\s+(.*)$")
_SEPARATOR_CELL_RE = re.compile(r":?-{3,}:?")
# A backslash before a markup character keeps it literal, so read output survives a rebuild:
# `2 \* 3` stays a product, `1\. Предмет` stays a paragraph. The lookbehinds treat an escaped
# star as text, not as the neighbour that would turn a lone `*` into part of `**`.
_ESCAPED_RE = re.compile(r"\\([\\*|#\-.)])")
_ESCAPE = r"\\[\\*|#\-.)]"
_STAR = r"(?<!(?<!\\)\*)\*(?!\*)"
_EMPHASIS_RE = re.compile(rf"({_ESCAPE}|\*\*(?:{_ESCAPE}|.)+?\*\*|{_STAR}(?:{_ESCAPE}|.)+?{_STAR})", re.DOTALL)
_CELL_SPLIT_RE = re.compile(r"(?<!(?<!\\)\\)\|")
_MARKUP_CHAR_RE = re.compile(r"[\\*|]")
_LINE_MARKER_RE = re.compile(r"^(\d*)(?=#|- |[.)]\s)")
# The XML 1.0 control set: no glyph in any font, and the same rule as the docx sibling.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass(frozen=True)
class Heading:
    level: int
    text: str


@dataclass(frozen=True)
class Paragraph:
    text: str


@dataclass(frozen=True)
class ListItem:
    text: str
    ordered: bool


@dataclass(frozen=True)
class Table:
    rows: list[list[str]]
    header: bool


Block = Heading | Paragraph | ListItem | Table


def emit(payload: dict[str, str | int | bool]) -> None:
    """Print one JSON object on stdout, the only output channel of a skill script."""
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def fail(message: str, *, status: str = "error") -> NoReturn:
    emit({"status": status, "error": message})
    raise SystemExit(2)


def workdir_file(name: str, *, must_exist: bool) -> pathlib.Path:
    """Resolve a bare file name inside the call's working directory.

    skill_exec puts the inputs there and publishes what is left, so a name with directories is refused.
    """
    workdir = os.environ.get("OUROBOROS_SKILL_WORKDIR", "")
    if not workdir:
        fail("OUROBOROS_SKILL_WORKDIR is not set: run this script through skill_exec")
    root = pathlib.Path(workdir).resolve()
    path = (root / name).resolve()
    if must_exist:
        # skill_exec hands over a drive path untouched when it is not an artifact of this task.
        if path.parent != root or not path.is_file():
            fail(f"{name} is not among this call's inputs: pass the uploads/... path from the result that built it")
    elif path.parent != root:
        fail(f"not a bare file name: {name}")
    return path


def clean_text(parts: list[str]) -> str:
    """Join the --text parts in order and drop the characters no font has a glyph for."""
    return _CONTROL_RE.sub("", "\n".join(parts))


def open_input(raw: str) -> pathlib.Path:
    """Resolve a binary input and refuse it by size before any library opens it."""
    path = workdir_file(raw, must_exist=True)
    size = path.stat().st_size
    if size > MAX_INPUT_BYTES:
        fail(f"input is {size} bytes, over the {MAX_INPUT_BYTES} limit", status="input_too_large")
    return path


def bound_output(text: str) -> tuple[str, bool]:
    """Cut *text* to what the tool result can carry and report whether it was cut."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    return text[: MAX_OUTPUT_CHARS - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER, True


def escape_text(text: str) -> str:
    """Escape the characters that carry markup inside a line, so document text reads back as itself."""
    return _MARKUP_CHAR_RE.sub(r"\\\g<0>", text)


def escape_line_start(line: str) -> str:
    """Escape a heading, bullet or number marker at the start of a plain text line."""
    return _LINE_MARKER_RE.sub(lambda match: match.group(1) + "\\", line, count=1)


def split_row(line: str) -> list[str]:
    body = line.strip().strip("|")
    return [cell.strip() for cell in _CELL_SPLIT_RE.split(body)]


def is_separator_row(line: str) -> bool:
    cells = split_row(line)
    return bool(cells) and all(_SEPARATOR_CELL_RE.fullmatch(cell) for cell in cells)


def split_emphasis(text: str) -> list[tuple[str, str]]:
    """Split *text* into (fragment, style) pairs where style is bold, italic or plain; escapes are resolved here."""
    fragments: list[tuple[str, str]] = []
    for token in _EMPHASIS_RE.split(text):
        if not token:
            continue
        if token.startswith("**") and token.endswith("**") and len(token) > 4:
            fragment, style = token[2:-2], "bold"
        elif token.startswith("*") and token.endswith("*") and len(token) > 2:
            fragment, style = token[1:-1], "italic"
        else:
            fragment, style = token, "plain"
        fragment = _ESCAPED_RE.sub(r"\1", fragment)
        if fragments and fragments[-1][1] == style:
            fragments[-1] = (fragments[-1][0] + fragment, style)
        else:
            fragments.append((fragment, style))
    return fragments


def parse_blocks(raw: str) -> list[Block]:
    """Translate the supported light markup into ordered blocks."""
    lines = raw.replace("\r\n", "\n").split("\n")
    blocks: list[Block] = []
    paragraph_lines: list[str] = []
    index = 0

    def flush_paragraph() -> None:
        if not paragraph_lines:
            return
        text = " ".join(line.strip() for line in paragraph_lines if line.strip())
        paragraph_lines.clear()
        if text:
            blocks.append(Paragraph(text=text))

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        index += 1

        if not stripped:
            flush_paragraph()
            continue
        if stripped.startswith("|") and "|" in stripped[1:]:
            flush_paragraph()
            rows = [split_row(stripped)]
            header = False
            while index < len(lines) and lines[index].strip().startswith("|"):
                candidate = lines[index].strip()
                index += 1
                if len(rows) == 1 and is_separator_row(candidate):
                    header = True
                    continue
                rows.append(split_row(candidate))
            blocks.append(Table(rows=rows, header=header))
            continue
        if stripped.startswith("#"):
            flush_paragraph()
            level = len(stripped) - len(stripped.lstrip("#"))
            blocks.append(Heading(level=min(4, level), text=stripped[level:].strip()))
            continue
        if stripped[:2] in ("- ", "* "):
            flush_paragraph()
            blocks.append(ListItem(text=stripped[2:].strip(), ordered=False))
            continue
        ordered = _ORDERED_RE.match(stripped)
        if ordered:
            flush_paragraph()
            blocks.append(ListItem(text=ordered.group(2).strip(), ordered=True))
            continue
        paragraph_lines.append(line)

    flush_paragraph()
    return blocks
