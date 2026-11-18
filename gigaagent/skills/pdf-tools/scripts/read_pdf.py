#!/usr/bin/env python3
"""Extract the text of a PDF that lives in the task workspace."""

from __future__ import annotations

import argparse
import sys

import common
from pypdf import PdfReader


def literal(text: str) -> str:
    """Escape what make_pdf.py would take for markup, so extracted text rebuilds as itself."""
    return "\n".join(common.escape_line_start(common.escape_text(line)) for line in text.split("\n"))


def extract_text(reader: PdfReader) -> str:
    """Join page texts; stop once the output budget is spent so a huge PDF is not read to the end."""
    pages: list[str] = []
    length = 0
    for page in reader.pages:
        text = literal(page.extract_text() or "")
        pages.append(text)
        length += len(text)
        if length > common.MAX_OUTPUT_CHARS:
            break
    return "\n\n".join(pages)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Extract the text of a PDF.")
    parser.add_argument("--in", dest="source", required=True, help="drive path of a PDF this skill built")
    args = parser.parse_args(argv)

    path = common.open_input(args.source)
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            common.fail(f"{path.name} is encrypted; a password-protected PDF cannot be read", status="invalid_input")
        page_count = len(reader.pages)
        text, truncated = common.bound_output(extract_text(reader))
    except Exception as exc:  # pypdf has no single error class for a malformed file
        common.fail(f"{path.name} is not a readable PDF: {exc}", status="invalid_input")

    common.emit(
        {
            "status": "ok",
            "file": path.name,
            "bytes": path.stat().st_size,
            "pages": page_count,
            "text": text,
            "truncated": truncated,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
