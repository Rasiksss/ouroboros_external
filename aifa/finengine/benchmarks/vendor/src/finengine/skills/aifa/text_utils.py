"""Small text utils ported from `financier_extractor.utils.text`."""
from __future__ import annotations

import re


_MD_INLINE = re.compile(r"[\*_`~|]")
_MD_HEADERS = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_LINKS = re.compile(r"\[([^\]]+)\]\([^\)]+\)")


def clean_markdown(text: str) -> str:
    """Strip the lightweight markdown that pdfplumber/AIFA tables ship with.

    Removes inline emphasis chars, header hashes, and link wrappers — leaves
    plain text that the n-gram extractor can tokenize.
    """
    if not text:
        return ""
    text = _MD_LINKS.sub(r"\1", text)
    text = _MD_HEADERS.sub("", text)
    text = _MD_INLINE.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
