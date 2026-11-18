"""Physical page evidence: explicit markers or OCR table metadata, never text chunks."""

from __future__ import annotations

import json
import re

_PAGE = re.compile(r"(?m)^===PAGE[ \t]+([1-9][0-9]*)===[ \t]*\r?\n?")


def pages_from_text(text: str) -> dict[int, str]:
    marks = list(_PAGE.finditer(text or ""))
    return {int(mark.group(1)): text[mark.end():marks[i + 1].start() if i + 1 < len(marks) else len(text)]
            for i, mark in enumerate(marks)}


def page_bound_text(text: str, raw_ocr: str = "") -> str:
    """For page-verified citations, expose only fragments with an actual page address."""
    if pages_from_text(text):
        return text
    if not raw_ocr:
        return ""
    raw = json.loads(raw_ocr)
    payload = raw.get("ocr_result", raw)
    pages: dict[int, list[str]] = {}
    for table in payload.get("tables", []):
        page = table.get("page")
        if type(page) is not int or page < 1:
            continue
        body = table.get("text", table.get("formatted_str", ""))
        if body:
            pages.setdefault(page, []).append(body)
    return "\n\n".join(f"===PAGE {page}===\n" + "\n\n".join(parts) for page, parts in sorted(pages.items()))
