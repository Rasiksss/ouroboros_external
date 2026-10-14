"""HTTP client for the AIFA IDP-OCR cloud service — page-by-page PNG mode.

Endpoint:  http://188.72.106.8:8896/predict   (set via FINENGINE_IDP_OCR_URL)
Reference (verbatim upstream pipeline):
  - `Репа в репе/document_extractor_idp_ocr_Расула/src/document_extractor_idp_ocr/idp_ocr.py`
  - `Репа в репе/.../utils_html.py`  (HTML <table> → markdown)
  - `Репа в репе/.../utils_md.py`     (markdown table substring extraction)

Why per-page PNG instead of whole-PDF upload:
  Earlier we sent the full PDF as `application/pdf` and let the service
  handle pagination internally. On long IFRS reports (e.g. Лукойл, ~140 p.)
  the service returned only a fraction of tables — `modelOCR.pages` was
  truncated / sparse. Upstream renders each page to a 2× zoom PNG via
  PyMuPDF and POSTs them in parallel as `image/png`. That's the supported
  high-quality path.

Output:
  Each page's `markdown.content` may contain `<table>...</table>` blocks
  (when the service emits HTML — output_format.page.html.html_support=True).
  We convert every HTML table to markdown via BeautifulSoup and replace it
  in the page text. Tables go into `OcrPage.tables` and `OcrResult.full_tables`
  for the reranker.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

# PyMuPDF + bs4 нужны ТОЛЬКО OCR/HTML-функциям ниже (call_idp_ocr и т.п.). В
# запечатанной станции OCR не делается (это работа гейтвея), а движок
# извлечения/анализа транзитивно импортит этот модуль — поэтому импорт защищён:
# без библиотек модуль грузится, а сами OCR-функции там просто не вызываются.
try:
    import pymupdf  # PyMuPDF
except ModuleNotFoundError:  # noqa: BLE001 — станция без OCR-стека
    pymupdf = None  # type: ignore[assignment]
try:
    from bs4 import BeautifulSoup
except ModuleNotFoundError:  # noqa: BLE001
    BeautifulSoup = None  # type: ignore[assignment,misc]

from finengine.config import settings


_log = logging.getLogger(__name__)


@dataclass
class OcrPage:
    page: int
    markdown: str
    tables: list[str]


@dataclass
class OcrResult:
    pages: list[OcrPage]
    full_text: str
    full_tables: list[dict]  # [{idx, page, text}, ...] — for the reranker
    diag: dict  # pages_total, pages_success, pages_failed, ocr_total_s


# Per-page params: same as upstream (`create_predict_params`) — minimal payload.
# The IDP-OCR service derives msgId/workId from the request; we pass
# random ids per page to avoid collisions across parallel requests. Tag
# format mirrors the upstream convention (`id<hex>` / `wordid<hex>`)
# so requests blend in with regular traffic.
def _predict_params(page_num: int) -> dict[str, Any]:
    import uuid
    from datetime import datetime, timezone
    return {
        "msgId": f"id{uuid.uuid4().hex[:8]}",
        "workId": f"wordid{uuid.uuid4().hex[:8]}",
        "msgTm": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# HTML <table> → markdown  (port of upstream utils_html.html_table_to_markdown)
# ─────────────────────────────────────────────────────────────────────────────

def _html_table_to_markdown(html_table: str) -> str:
    soup = BeautifulSoup(html_table, "html.parser")
    table = soup.find("table")
    if not table:
        return html_table

    rows: list[list[str]] = []

    thead = table.find("thead")
    if thead:
        for tr in thead.find_all("tr"):
            cells: list[str] = []
            for cell in tr.find_all(["th", "td"]):
                colspan = int(cell.get("colspan", 1) or 1)
                text = cell.get_text(strip=True)
                cells.append(text)
                for _ in range(colspan - 1):
                    cells.append("")
            if cells:
                rows.append(cells)

    tbody = table.find("tbody") or table
    thead_rows = thead.find_all("tr") if thead else []
    for tr in tbody.find_all("tr"):
        if tr in thead_rows:
            continue
        cells = []
        for cell in tr.find_all(["td", "th"]):
            colspan = int(cell.get("colspan", 1) or 1)
            text = cell.get_text(strip=True)
            cells.append(text)
            for _ in range(colspan - 1):
                cells.append("")
        if cells:
            rows.append(cells)

    if not rows:
        return html_table

    md_rows: list[str] = []
    for i, row in enumerate(rows):
        md_rows.append("| " + " | ".join(c if c else "" for c in row) + " |")
        if i == 0:
            md_rows.append("| " + " | ".join("---" for _ in row) + " |")
    return "\n".join(md_rows)


_HTML_TAG_REPLACEMENTS: list[tuple[str, str]] = [
    ("<br>", " "), ("<br/>", " "), ("<br />", " "),
    ("<strong>", "**"), ("</strong>", "**"),
    ("<b>", "**"), ("</b>", "**"),
    ("<em>", "*"), ("</em>", "*"),
    ("<i>", "*"), ("</i>", "*"),
    ("<p>", ""), ("</p>", " "),
    ("<div>", ""), ("</div>", " "),
    ("<span>", ""), ("</span>", ""),
]


def _postprocess_table_string(table_str: str) -> str:
    """Port of upstream `postprocess_table_string` — clean tags, normalize pipes."""
    if not table_str:
        return table_str
    cleaned = table_str
    for tag, repl in _HTML_TAG_REPLACEMENTS:
        cleaned = cleaned.replace(tag, repl)
    cleaned = re.sub(r" +", " ", cleaned)
    out_lines: list[str] = []
    for line in cleaned.split("\n"):
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"\s*\|\s*", " | ", line).strip()  # strip again — regex re-introduces edge whitespace
        if "|" in line and not line.startswith("|---"):
            if not line.startswith("|"):
                line = "| " + line.lstrip("|")
            if not line.endswith("|"):
                line = line.rstrip("|") + " |"
        out_lines.append(line)
    return "\n".join(out_lines)


def _convert_html_tables_in_text(content: str) -> tuple[str, list[str]]:
    """Replace every <table>...</table> in `content` with its markdown form.
    Returns (content_with_md_tables, list_of_extracted_table_strings).
    """
    extracted: list[str] = []
    pattern = re.compile(r"<table[^>]*>.*?</table>", re.DOTALL | re.IGNORECASE)

    def _replace(m: re.Match) -> str:
        md = _html_table_to_markdown(m.group(0))
        cleaned = _postprocess_table_string(md)
        if cleaned and len(cleaned) > 40:  # keep only tables with content
            extracted.append(cleaned)
        return cleaned

    new_content = pattern.sub(_replace, content)
    return new_content, extracted


_MD_TABLE_RE = re.compile(
    r"(\|[^\n]+\|\n\|[-:\s|]+\|\n(?:\|[^\n]+\|\n?)*)", re.MULTILINE | re.DOTALL
)


def _extract_md_tables(content: str) -> list[str]:
    """Pick up native markdown-pipe tables that weren't HTML."""
    out: list[str] = []
    for m in _MD_TABLE_RE.finditer(content):
        block = m.group().strip()
        if block.count("\n") >= 2:
            out.append(block)
    return out


def _page_markdown_block(page_num: int, content: str) -> str:
    """Wrap one page's Markdown with the marker consumed by Finengine."""
    return f"===PAGE {page_num}===\n{content.strip()}"


def markdown_from_saved_idp_response(raw: dict) -> str:
    """Build the paginated Markdown sidecar from a saved merged IDP response."""
    parts: list[str] = []
    results = raw.get("results", []) if isinstance(raw, dict) else []
    for result in sorted(results, key=lambda item: item.get("page", 0)):
        if not isinstance(result, dict) or not result.get("success"):
            continue
        page_num = int(result.get("page") or 0)
        payload = result.get("response", {})
        content = ""
        if isinstance(payload, dict) and "modelOCR" in payload:
            for sub in payload.get("modelOCR", {}).get("pages", []) or []:
                md = sub.get("markdown", {})
                if isinstance(md, dict) and md.get("content"):
                    content += md["content"] + "\n\n"
        elif isinstance(payload, dict) and "content" in payload:
            content = payload["content"]
        if content:
            content_md, _tables = _convert_html_tables_in_text(content)
            parts.append(_page_markdown_block(page_num, content_md))
    return "\n\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Per-page rendering + parallel POST
# ─────────────────────────────────────────────────────────────────────────────

def _render_page_png(pdf_path: Path, page_num: int, zoom: float = 2.0) -> bytes:
    """PyMuPDF-render one page to PNG bytes. zoom=2.0 → ~144 DPI (upstream default)."""
    doc = pymupdf.open(pdf_path)
    try:
        page = doc.load_page(page_num)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        return pix.tobytes("png")
    finally:
        doc.close()


async def _send_page(
    client: httpx.AsyncClient,
    url: str,
    page_num: int,
    pdf_path: Path,
    sem: asyncio.Semaphore,
    max_retries: int = 3,
) -> dict[str, Any]:
    """POST one rendered page PNG to /predict, with retry on transport errors.

    Returns {page, success, response | error}. Pages are returned 1-indexed
    in `page` (matching upstream).
    """
    async with sem:
        # Render in a thread — PyMuPDF is sync.
        png = await asyncio.to_thread(_render_page_png, pdf_path, page_num)
        files = {"structuredDocument": (f"page_{page_num + 1}.png", png, "image/png")}
        data = {"predictParams": json.dumps(_predict_params(page_num + 1))}

        last_err: str | None = None
        for attempt in range(max_retries + 1):
            try:
                resp = await client.post(url, files=files, data=data)
                if resp.status_code == 200:
                    return {"page": page_num + 1, "success": True, "response": resp.json()}
                if 400 <= resp.status_code < 500:
                    return {
                        "page": page_num + 1,
                        "success": False,
                        "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
                    }
                last_err = f"HTTP {resp.status_code}"
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = f"network: {type(e).__name__}: {e}"
            if attempt < max_retries:
                await asyncio.sleep(2.0 * (2 ** attempt))
        return {"page": page_num + 1, "success": False, "error": last_err or "max retries"}


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

async def call_idp_ocr(
    pdf_path: Path,
    *,
    timeout: float = 90.0,
    parallel: int | None = None,
    save_raw_to: Path | None = None,
) -> OcrResult:
    """Send each page of `pdf_path` to IDP-OCR as PNG, in parallel.

    Mirrors the upstream pipeline:
      1. Count pages with PyMuPDF
      2. Render each at zoom=2 to PNG bytes
      3. POST as image/png in parallel (semaphore=parallel)
      4. Convert HTML <table> in each page's markdown to clean MD via BS4
      5. Aggregate → OcrResult(pages, full_text, full_tables, diag)

    `save_raw_to`, if given, persists the merged raw response JSON for
    postmortems. Per-page errors are recorded in `diag["pages_failed"]`.
    """
    import time as _t

    url = settings.idp_ocr_url.rstrip("/")
    if not url:
        raise RuntimeError("IDP-OCR URL is not configured (set FINENGINE_IDP_OCR_URL).")
    if not url.endswith("/predict"):
        url = f"{url}/predict"

    pages_count = await asyncio.to_thread(lambda: pymupdf.open(pdf_path).page_count)

    # Match upstream `joblib.Parallel(n_jobs=-1)` semantic: use as many
    # concurrent requests as the box has CPU cores. Override via the
    # `parallel` kwarg if you want to throttle further.
    if parallel is None:
        parallel = os.cpu_count() or 8
    sem = asyncio.Semaphore(max(1, parallel))
    t0 = _t.time()
    async with httpx.AsyncClient(timeout=timeout) as client:
        tasks = [_send_page(client, url, p, pdf_path, sem) for p in range(pages_count)]
        results = await asyncio.gather(*tasks)
    ocr_total_s = round(_t.time() - t0, 1)

    results.sort(key=lambda r: r.get("page", 0))

    # ── Optionally persist the raw merged response ─────────────────────────
    # Atomic write: if two extracts on the same doc race here (rare but
    # possible if a user clicks "Извлечь" twice), the consumer never sees a
    # half-written file. Write to a sibling tmp + os.replace().
    if save_raw_to is not None:
        try:
            import os as _os
            save_raw_to.parent.mkdir(parents=True, exist_ok=True)
            tmp = save_raw_to.with_suffix(save_raw_to.suffix + f".tmp.{_os.getpid()}")
            tmp.write_text(
                json.dumps({"pages_count": pages_count, "results": results},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _os.replace(tmp, save_raw_to)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Failed to persist IDP-OCR raw response to {save_raw_to}") from e

    # ── Walk per-page responses → markdown + tables ────────────────────────
    pages: list[OcrPage] = []
    flat_tables: list[dict] = []
    full_md_parts: list[str] = []
    failed_pages: list[dict] = []

    for r in results:
        page_num = int(r.get("page") or 0)
        if not r.get("success"):
            failed_pages.append({"page": page_num, "error": str(r.get("error", ""))[:200]})
            continue
        payload = r.get("response", {})
        # Pull markdown.content from the per-page response (same shape as before).
        content = ""
        if isinstance(payload, dict) and "modelOCR" in payload:
            for sub in payload.get("modelOCR", {}).get("pages", []) or []:
                md = sub.get("markdown", {})
                if isinstance(md, dict) and md.get("content"):
                    content += md["content"] + "\n\n"
        elif isinstance(payload, dict) and "content" in payload:
            content = payload["content"]
        if not content:
            failed_pages.append({"page": page_num, "error": "empty markdown"})
            continue

        # Convert HTML tables → markdown, then look for any remaining MD tables.
        content_md, html_tables = _convert_html_tables_in_text(content)
        md_tables = _extract_md_tables(content_md)
        all_tables = html_tables + [
            t for t in md_tables if not any(t.strip() == ht.strip() for ht in html_tables)
        ]

        pages.append(OcrPage(page=page_num, markdown=content_md, tables=all_tables))
        full_md_parts.append(_page_markdown_block(page_num, content_md))
        for t in all_tables:
            flat_tables.append({"idx": len(flat_tables), "page": page_num, "text": t})

    diag = {
        "pages_total": pages_count,
        "pages_success": len(pages),
        "pages_failed": len(failed_pages),
        "failed_samples": failed_pages[:5],
        "ocr_total_s": ocr_total_s,
        "tables_total": len(flat_tables),
        "parallel": parallel,
    }

    return OcrResult(
        pages=pages,
        full_text="\n\n".join(full_md_parts),
        full_tables=flat_tables,
        diag=diag,
    )
