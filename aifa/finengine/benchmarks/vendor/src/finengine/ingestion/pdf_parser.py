"""Parse PDF files into text chunks and tables."""
from __future__ import annotations
import re
from pathlib import Path
import fitz  # PyMuPDF
import pdfplumber

from finengine.models.document import Chunk, DocumentMeta, DocType

# Markers that identify financial statement pages — keep these pages whole
FINANCIAL_STATEMENT_MARKERS = [
    # Balance sheet
    "отчет о финансовом положении", "balance sheet", "statement of financial position",
    "бухгалтерский баланс", "обобщенный отчет о финансовом",
    # P&L — multiple Russian wordings
    "отчет о прибылях и убытках", "отчет о прибыли или убытке",
    "отчет о прибыли и убытке", "отчет о совокупном доходе",
    "income statement", "statement of profit", "statement of comprehensive income",
    # Cash flow
    "отчет о движении денежных средств", "cash flow statement", "operating activities",
    "денежные средства от операционной",
    # Changes in equity
    "отчет об изменениях капитала", "statement of changes in equity",
    # Key balance sheet words
    "внеоборотные активы", "оборотные активы", "total assets",
    "итого активов", "итого обязательств", "капитал и резервы",
    # Key P&L words
    "выручка от", "чистая прибыль", "net income", "net profit",
    "валовая прибыль", "операционная прибыль", "gross profit",
]


def detect_language(text: str) -> str:
    cyrillic = len(re.findall(r'[а-яА-ЯёЁ]', text))
    latin = len(re.findall(r'[a-zA-Z]', text))
    return "ru" if cyrillic > latin else "en"


def detect_doc_type(filename: str, text_sample: str) -> DocType:
    from finengine.ingestion.document_classifier import detect_document_type

    return detect_document_type(filename, text_sample).doc_type


def is_financial_statement_page(text: str) -> bool:
    """Detect if this page contains a core financial statement table.

    Uses two signals:
    1. Known header markers (statement titles)
    2. Numeric density — financial tables have many numbers per line
    """
    text_lower = text.lower()

    # Exclusion takes priority — these pages are never financial tables
    EXCLUSION_MARKERS = [
        "аудиторское заключение", "auditor", "аудитор",
        "независимого аудитора", "ernst & young", "deloitte", "kpmg", "pwc",
        "содержание", "table of contents", "оглавление",
    ]
    if any(m in text_lower[:500] for m in EXCLUSION_MARKERS):
        return False

    # Signal 1: header marker (after exclusion check)
    if any(marker in text_lower for marker in FINANCIAL_STATEMENT_MARKERS):
        return True

    # Signal 2: numeric density heuristic
    # A financial table page typically has >35% lines with numbers
    # and many lines with a number at the end (right-aligned value column)
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(lines) < 8:
        return False

    numeric_lines = sum(
        1 for l in lines
        if re.search(r'[\(\-]?\d[\d\s]{2,}\d\)?', l)  # numbers incl. (1 234) format
        and len(l) > 10
    )
    numeric_ratio = numeric_lines / len(lines)

    # Lines ending with a number — strong signal of a table value column
    lines_ending_number = sum(1 for l in lines if re.search(r'[\d)\-]\s*$', l))
    end_number_ratio = lines_ending_number / len(lines)

    # Signal 2a: original heuristic — dense numeric rows + right-aligned values
    if numeric_ratio > 0.35 and end_number_ratio > 0.25:
        return True

    # Signal 2b: transposed tables (one number per line) — Саммит / small-bank style.
    # Each cell on its own line means num_ratio is low (short numbers don't match
    # the 3+-digit pattern) but end_ratio is very high.
    if lines_ending_number >= 20 and end_number_ratio > 0.40:
        return True

    return False


def chunk_text(text: str, chunk_size: int = 800, overlap: int = 100) -> list[str]:
    """Split text into overlapping chunks by sentences."""
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    chunks = []
    current = []
    current_len = 0

    for sentence in sentences:
        slen = len(sentence)
        if current_len + slen > chunk_size and current:
            chunks.append(" ".join(current))
            overlap_sentences = []
            overlap_len = 0
            for s in reversed(current):
                if overlap_len + len(s) > overlap:
                    break
                overlap_sentences.insert(0, s)
                overlap_len += len(s)
            current = overlap_sentences
            current_len = overlap_len
        current.append(sentence)
        current_len += slen

    if current:
        chunks.append(" ".join(current))

    return [c for c in chunks if len(c.strip()) > 50]


def clean_financial_table_text(text: str) -> str:
    """Clean up extracted text from financial statement pages."""
    # Remove excessive whitespace but preserve line structure
    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def extract_speaker(text: str) -> str:
    match = re.match(r'^([А-ЯA-Z][а-яa-z]+(?:\s+[А-ЯA-Z][а-яa-z]+)*):\s*', text)
    if match:
        return match.group(1)
    return ""


_LETTER_RE = re.compile(r'[а-яА-ЯёЁa-zA-Z0-9]')
_CYRILLIC_RE = re.compile(r'[а-яА-ЯёЁ]')

# Minimum letter ratio to consider OCR output readable (below = garbage/wrong orientation)
_MIN_READABLE_SCORE = 0.55


def _is_garbled(text: str) -> bool:
    """Detect garbled text from CID/broken font encoding.

    Russian financial reports should contain significant Cyrillic.
    If PyMuPDF returns text with <5% Cyrillic characters among letters,
    it's likely a broken font mapping (e.g. ФосАгро page 9: "BbiPYYKa" = "Выручка").
    """
    if not text or len(text) < 50:
        return False
    letters = _LETTER_RE.findall(text)
    if len(letters) < 20:
        return False
    cyrillic = _CYRILLIC_RE.findall(text)
    ratio = len(cyrillic) / len(letters)
    return ratio < 0.15


def _auto_rotate(img, lang: str = "rus+eng"):
    """Detect and fix image orientation for OCR.

    Try all 4 rotations via tesseract OCR and pick the one with the highest
    ratio of Cyrillic/Latin/digit characters (= most readable text).
    """
    import pytesseract

    best_img = img
    best_score = -1

    for rot in [0, 90, 180, 270]:
        candidate = img.rotate(rot, expand=True) if rot else img
        try:
            text = pytesseract.image_to_string(
                candidate, lang=lang, config="--psm 6"
            )
            letters = len(_LETTER_RE.findall(text))
            total = max(len(text.strip()), 1)
            score = letters / total
            if score > best_score:
                best_score = score
                best_img = candidate
            # Early exit: rot=0 already reads well → no rotation needed
            if rot == 0 and score >= _MIN_READABLE_SCORE:
                return img
        except Exception:
            continue

    return best_img


def _ocr_page(page, lang: str = "rus+eng") -> str:
    """OCR a PDF page using Tesseract (fallback for scanned pages).

    Strategy: if the page contains multiple embedded images, OCR the largest one
    directly rather than the composited page pixmap. PDF pages that mix a high-res
    PNG with a low-res JPEG overlay produce corrupted composites — the raw largest
    image is almost always the clean content.

    Auto-rotation: scanned pages may be sideways (90°/270°). We detect orientation
    via tesseract OSD or brute-force 4 rotations before final OCR.
    """
    try:
        import pytesseract
        from PIL import Image
        import io
        doc = page.parent

        # Try per-image OCR first (avoids JPEG-overlay corruption on composited pages)
        images = page.get_images(full=True)
        if images:
            best_img = None
            best_area = 0
            for img_info in images:
                xref = img_info[0]
                try:
                    base = doc.extract_image(xref)
                    img = Image.open(io.BytesIO(base["image"]))
                    area = img.width * img.height
                    if area > best_area:
                        best_area = area
                        best_img = img
                except Exception:
                    continue
            # Require the embedded image to cover at least ~half a page
            # (scanned statements use one large image per page; decorative
            # logos/headers are tiny). This avoids OCR'ing a bank logo and
            # returning garbage like "HEPHOS" when the real scan is rendered
            # from the page pixmap below.
            page_area = abs(page.rect.width * page.rect.height) * 4  # ~px @ 2x
            if best_img is not None and best_area > max(200000, page_area * 0.3):
                w, h = best_img.size
                # Upscale if smaller than ~A4 at 150dpi equivalent
                scale = max(1, min(3, int(3000 / max(w, h)) + 1))
                if scale > 1:
                    best_img = best_img.resize((w * scale, h * scale), Image.LANCZOS)
                best_img = best_img.convert("L")
                # Auto-rotate before OCR
                best_img = _auto_rotate(best_img, lang=lang)
                text = pytesseract.image_to_string(best_img, lang=lang, config="--psm 6")
                # Only accept if we actually got meaningful content —
                # otherwise fall through to full-page render OCR.
                if len(text.strip()) >= 200:
                    return _fix_ocr_numbers(text)

        # Fallback: render full page pixmap at 2x
        mat = fitz.Matrix(2.0, 2.0)
        pix = page.get_pixmap(matrix=mat)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        img = _auto_rotate(img, lang=lang)
        text = pytesseract.image_to_string(img, lang=lang, config="--psm 6")
        return _fix_ocr_numbers(text)
    except Exception:
        return ""


def _fix_ocr_numbers(text: str) -> str:
    """Fix common OCR artifacts in financial tables.

    Russian financial tables use spaces as thousand separators: 1 234 567
    OCR sometimes merges adjacent numbers across columns: "35 192 296 751"
    when the real values are "35 192" and "296 751" in separate columns.

    Strategy: normalize groups of digits+spaces that form valid thousand-separated
    numbers (groups of exactly 3 digits after the first group).
    """
    # Collapse runs of spaces to single space within number-like sequences
    # Pattern: number with spaces as thousand separators followed by another such number
    # We DON'T try to split them — instead preserve as-is for Claude to interpret.
    # The main fix: remove non-breaking spaces and normalize whitespace in numbers.
    text = text.replace("\u00a0", " ").replace("\u2009", " ")
    return text


def parse_pdf(
    file_path: Path,
    meta: DocumentMeta,
    chunk_size: int = 800,
    chunk_overlap: int = 100,
) -> list[Chunk]:
    """Extract text and tables from PDF, return list of Chunks."""
    chunks: list[Chunk] = []
    full_text_sample = ""

    # Classify PDF before parsing so we can annotate chunks with page origin
    try:
        from finengine.ingestion.pdf_classifier import classify_pdf
        classification = classify_pdf(file_path)
        meta.pdf_origin = classification.origin
    except Exception:
        classification = None
        meta.pdf_origin = ""

    doc = fitz.open(str(file_path))
    meta.total_pages = len(doc)

    base_metadata = {
        "filename": meta.filename,
        "doc_type": meta.doc_type,
        "company": meta.company_name,
        "period": meta.period,
        "language": meta.language,
    }

    for page_num, page in enumerate(doc, start=1):
        page_class = classification.pages[page_num - 1] if classification else None
        page_origin = page_class.origin if page_class else ""

        text = page.get_text("text").strip()
        ocr_used = False
        if not text or _is_garbled(text):
            # Fallback: OCR the page (scanned image or garbled CID font)
            ocr_text = _ocr_page(page).strip()
            if ocr_text and (not text or _is_garbled(text)):
                text = ocr_text
                ocr_used = True
        if not text:
            continue

        if page_num == 1 and not full_text_sample:
            full_text_sample = text[:2000]
            if not meta.language:
                meta.language = detect_language(full_text_sample)
                base_metadata["language"] = meta.language
            if meta.doc_type == DocType.OTHER:
                meta.doc_type = detect_doc_type(meta.filename, full_text_sample)
                base_metadata["doc_type"] = meta.doc_type

        # Detect section heading
        section = ""
        for line in text.split("\n"):
            stripped = line.strip()
            if len(stripped) < 100 and stripped.isupper() and len(stripped) > 5:
                section = stripped
                break

        page_meta = {
            **base_metadata,
            "page": page_num,
            "section": section,
            "page_origin": page_origin,
            "ocr_used": ocr_used,
        }

        if is_financial_statement_page(text):
            # Keep financial statement pages WHOLE — critical for accuracy
            cleaned = clean_financial_table_text(text)
            if len(cleaned) > 50:
                chunks.append(Chunk(
                    doc_id=meta.doc_id,
                    content=cleaned,
                    chunk_type="financial_table",
                    page=page_num,
                    section=section,
                    metadata={**page_meta, "is_financial_page": True},
                ))

            # Also add smaller sliding windows so individual line items are searchable
            lines = [l for l in cleaned.split("\n") if l.strip()]
            window_size = 8  # lines per window
            stride = 4
            for i in range(0, max(1, len(lines) - window_size + 1), stride):
                window = "\n".join(lines[i:i + window_size])
                if len(window.strip()) > 30:
                    chunks.append(Chunk(
                        doc_id=meta.doc_id,
                        content=window,
                        chunk_type="financial_table_window",
                        page=page_num,
                        section=section,
                        metadata={**page_meta, "window_start": i},
                    ))
        else:
            # Normal text pages: chunk by sentences
            speaker = ""
            for chunk_content in chunk_text(text, chunk_size, chunk_overlap):
                if meta.doc_type == DocType.TRANSCRIPT:
                    speaker = extract_speaker(chunk_content)
                chunks.append(Chunk(
                    doc_id=meta.doc_id,
                    content=chunk_content,
                    chunk_type="text",
                    page=page_num,
                    section=section,
                    speaker=speaker,
                    metadata=page_meta,
                ))

    doc.close()

    # Extract tables with pdfplumber — whole tables as single chunks
    try:
        with pdfplumber.open(str(file_path)) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                tables = page.extract_tables()
                for table in tables:
                    if not table or len(table) < 2:
                        continue
                    rows = []
                    for row in table:
                        cells = [re.sub(r'\s+', ' ', str(c).strip()) if c else "" for c in row]
                        # Skip completely empty rows
                        if any(c for c in cells):
                            rows.append(" | ".join(cells))
                    table_text = "\n".join(rows)
                    if len(table_text.strip()) < 30:
                        continue
                    chunks.append(Chunk(
                        doc_id=meta.doc_id,
                        content=table_text,
                        chunk_type="table",
                        page=page_num,
                        metadata={
                            **base_metadata,
                            "page": page_num,
                            "rows": len(rows),
                        },
                    ))
    except Exception:
        pass

    return chunks
