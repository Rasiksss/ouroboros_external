"""Classify PDF origin: digital-born, scanned, or mixed.

Analyses each page using PyMuPDF signals:
  - text character count
  - image area vs page area ratio
  - embedded font presence

Returns a PDFClassification with recommended extraction strategy per page.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PageClassification:
    page_num: int          # 1-based
    origin: str            # "digital" | "scanned" | "empty"
    text_chars: int
    image_area_ratio: float  # 0..1
    has_fonts: bool
    confidence: float      # 0..1


@dataclass
class PDFClassification:
    path: str
    total_pages: int
    origin: str            # "digital" | "scanned" | "mixed"
    digital_pages: int
    scanned_pages: int
    empty_pages: int
    avg_text_chars: float
    recommended_extractor: str  # "pdfplumber" | "ocr_tesseract" | "ocr_claude_vision"
    confidence: float
    pages: list[PageClassification] = field(default_factory=list)

    def scanned_ratio(self) -> float:
        content_pages = self.digital_pages + self.scanned_pages
        if content_pages == 0:
            return 0.0
        return self.scanned_pages / content_pages


# Thresholds
_MIN_TEXT_CHARS_DIGITAL = 80   # pages with ≥ this many chars are digital
_MIN_TEXT_CHARS_ANY = 10       # pages with < this are considered image/empty
_MAX_IMAGE_RATIO_DIGITAL = 0.6 # if image covers > 60% and few chars → likely scanned


def classify_pdf(path: Path) -> PDFClassification:
    """Analyse every page and return a classification with recommended extractor."""
    import fitz  # PyMuPDF

    doc = fitz.open(str(path))
    total = len(doc)
    page_results: list[PageClassification] = []

    for i, page in enumerate(doc):
        text = page.get_text("text")
        text_chars = len(text.strip())
        has_fonts = bool(page.get_fonts())

        # Image area: sum of all image rects clipped to page
        page_area = page.rect.width * page.rect.height
        img_area = 0.0
        for img in page.get_image_info():
            r = fitz.Rect(img.get("bbox", (0, 0, 0, 0)))
            img_area += r.width * r.height
        image_ratio = min(img_area / page_area, 1.0) if page_area > 0 else 0.0

        # Classify page
        if text_chars >= _MIN_TEXT_CHARS_DIGITAL:
            origin = "digital"
            confidence = min(1.0, text_chars / 500)
        elif text_chars >= _MIN_TEXT_CHARS_ANY and has_fonts:
            # Sparse text but fonts present — probably digital with sparse content
            origin = "digital"
            confidence = 0.6
        elif image_ratio > 0.3 or (text_chars < _MIN_TEXT_CHARS_ANY and not has_fonts):
            origin = "scanned"
            confidence = min(1.0, image_ratio + 0.3)
        else:
            origin = "empty"
            confidence = 0.7

        page_results.append(PageClassification(
            page_num=i + 1,
            origin=origin,
            text_chars=text_chars,
            image_area_ratio=round(image_ratio, 3),
            has_fonts=has_fonts,
            confidence=round(confidence, 3),
        ))

    doc.close()

    digital = sum(1 for p in page_results if p.origin == "digital")
    scanned = sum(1 for p in page_results if p.origin == "scanned")
    empty = sum(1 for p in page_results if p.origin == "empty")
    avg_chars = sum(p.text_chars for p in page_results) / max(total, 1)

    content = digital + scanned
    scanned_ratio = scanned / content if content > 0 else 0.0

    if scanned_ratio == 0.0:
        origin = "digital"
        extractor = "pdfplumber"
        conf = 0.95
    elif scanned_ratio >= 0.7:
        origin = "scanned"
        # Prefer Claude Vision for financial reports; Tesseract as fallback
        extractor = "ocr_claude_vision"
        conf = 0.90
    else:
        origin = "mixed"
        extractor = "pdfplumber"  # pdfplumber handles page-by-page; OCR applied per-page
        conf = 0.75

    return PDFClassification(
        path=str(path),
        total_pages=total,
        origin=origin,
        digital_pages=digital,
        scanned_pages=scanned,
        empty_pages=empty,
        avg_text_chars=round(avg_chars, 1),
        recommended_extractor=extractor,
        confidence=round(conf, 3),
        pages=page_results,
    )
