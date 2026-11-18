"""Shared helpers for the xlsx-tools scripts; a skill subprocess cannot import project code, so each package carries its own copy."""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import zipfile
from typing import NoReturn

# Resource bounds for this standalone script, which cannot import the runtime.
# Runtime attachment limits are configured separately in docs/SETTINGS.md.
MAX_INPUT_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_CHARS = 100_000
MAX_ARCHIVE_ENTRIES = 10_000
MAX_ARCHIVE_ENTRY_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 200

# The XML 1.0 set openpyxl refuses (its ILLEGAL_CHARACTERS_RE); stripped before a cell is
# written so one stray control character does not fail the whole workbook.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


CellValue = str | int | float | bool | None


def emit(payload: dict[str, object]) -> None:
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


def open_input(raw: str) -> pathlib.Path:
    """Resolve a binary input and refuse it by size before any library opens it."""
    path = workdir_file(raw, must_exist=True)
    size = path.stat().st_size
    if size > MAX_INPUT_BYTES:
        fail(f"input is {size} bytes, over the {MAX_INPUT_BYTES} limit", status="input_too_large")
    return path


def guard_package(path: pathlib.Path, required: str) -> None:
    """Refuse a zip that lacks *required* or would inflate far beyond its size on disk, before any parser opens it."""
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
    except zipfile.BadZipFile as exc:
        fail(f"{path.name} is not a zip package: {exc}", status="invalid_input")
    if required not in {entry.filename for entry in entries}:
        fail(f"{path.name} is not the expected package: {required} is missing", status="invalid_input")
    if len(entries) > MAX_ARCHIVE_ENTRIES:
        fail(f"{path.name} has {len(entries)} entries, over the {MAX_ARCHIVE_ENTRIES} limit", status="input_too_large")
    unpacked = sum(entry.file_size for entry in entries)
    if unpacked > MAX_ARCHIVE_TOTAL_BYTES or any(entry.file_size > MAX_ARCHIVE_ENTRY_BYTES for entry in entries):
        fail(f"{path.name} unpacks to {unpacked} bytes, over the limit", status="input_too_large")
    if any(entry.file_size > MAX_ARCHIVE_COMPRESSION_RATIO * max(1, entry.compress_size) for entry in entries):
        fail(f"{path.name} inflates beyond {MAX_ARCHIVE_COMPRESSION_RATIO}:1", status="input_too_large")


def strip_control_characters(text: str) -> str:
    return _CONTROL_RE.sub("", text)
