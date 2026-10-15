"""I/O helpers — sidecar polling, file paths."""

from __future__ import annotations

from finengine.skills.bulk_extractor import _wait_for_pdf_text

__all__ = ["wait_for_pdf_text"]


wait_for_pdf_text = _wait_for_pdf_text
