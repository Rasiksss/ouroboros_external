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
  in the page text. Tables go into `OcrPage.tables` and `OcrResult.full_tables`;
  full_tables keeps normalized text for prompts plus original_text and
  position/context metadata for diagnostics and table-neighbor rankers.
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
import pymupdf  # PyMuPDF
from bs4 import BeautifulSoup

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
    full_tables: list[dict]  # [{idx, page, text, original_text, position_range, ...}, ...]
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


def _split_markdown_row(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return []
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def _join_markdown_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _is_separator_cell(cell: str) -> bool:
    return bool(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")))


def _trim_empty_edge_columns_markdown(table_str: str) -> str:
    """Remove fully empty leading/trailing columns from a markdown table."""
    if not table_str or "|" not in table_str:
        return table_str

    lines = [line.rstrip() for line in table_str.splitlines() if line.strip()]
    parsed_rows = [_split_markdown_row(line) for line in lines]
    if not parsed_rows or any(not row for row in parsed_rows):
        return table_str

    widths = {len(row) for row in parsed_rows}
    if len(widths) != 1:
        return table_str

    width = widths.pop()
    if width <= 1:
        return table_str

    def col_is_empty(idx: int) -> bool:
        for row in parsed_rows:
            cell = row[idx]
            if _is_separator_cell(cell):
                continue
            if cell:
                return False
        return True

    left = 0
    right = width - 1
    while left < width and col_is_empty(left):
        left += 1
    while right >= 0 and col_is_empty(right):
        right -= 1

    if left == 0 and right == width - 1:
        return table_str
    if left > right:
        return table_str

    return "\n".join(_join_markdown_row(row[left : right + 1]) for row in parsed_rows)


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


def _normalize_table_string(table_str: str) -> str:
    cleaned = _postprocess_table_string(table_str)
    return _trim_empty_edge_columns_markdown(cleaned)


def _convert_html_tables_in_text(content: str) -> tuple[str, list[dict[str, str]]]:
    """Replace every HTML table with normalized markdown and keep originals."""
    extracted: list[dict[str, str]] = []
    pattern = re.compile(r"<table[^>]*>.*?</table>", re.DOTALL | re.IGNORECASE)

    def _replace(m: re.Match) -> str:
        original = m.group(0)
        md = _html_table_to_markdown(original)
        cleaned = _normalize_table_string(md)
        if cleaned and len(cleaned) > 40:  # keep only tables with content
            extracted.append({"text": cleaned, "original_text": original})
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


def _normalize_tables_in_text(content: str) -> tuple[str, list[dict[str, str]]]:
    """Normalize all OCR tables and replace them in the returned text."""
    content_with_md, html_tables = _convert_html_tables_in_text(content)
    html_queue = list(html_tables)
    tables: list[dict[str, str]] = []

    def _replace_markdown(match: re.Match) -> str:
        original = match.group().strip()
        normalized = _normalize_table_string(original)
        original_text = original
        if html_queue and html_queue[0]["text"].strip() == normalized.strip():
            original_text = html_queue.pop(0)["original_text"]
        if normalized and len(normalized) > 40:
            tables.append({"text": normalized, "original_text": original_text})
        return normalized

    normalized_content = _MD_TABLE_RE.sub(_replace_markdown, content_with_md)
    tables.extend(html_queue)
    return normalized_content, tables


def _line_spans_by_newlines(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for m in re.finditer(r"\n{1,4}", text):
        end = m.start()
        if end > start:
            spans.append((start, end))
        start = m.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _line_index_for_position(spans: list[tuple[int, int]], pos: int) -> int:
    import bisect

    starts = [start for start, _end in spans]
    return max(0, bisect.bisect_right(starts, pos) - 1)


def _span_overlaps_range(span: tuple[int, int], range_: tuple[int, int]) -> bool:
    a, b = span
    start, end = range_
    return a < end and b > start


def _clean_neighbor_context_line(line: str) -> str:
    line = line.strip()
    if re.fullmatch(r"===PAGE\s+\d+===", line):
        return ""
    return line


def _attach_table_positions_and_context(
    full_text: str,
    tables: list[dict[str, Any]],
    *,
    pre_lines: int = 1,
    post_lines: int = 1,
) -> None:
    """Attach old OCR-style table offsets plus nearest non-table context."""
    search_from = 0
    ranges: list[tuple[int, int, int]] = []
    for idx, table in enumerate(tables):
        table_text = table.get("text")
        if not isinstance(table_text, str) or not table_text:
            continue
        start = full_text.find(table_text, search_from)
        if start < 0:
            start = full_text.find(table_text)
        if start < 0:
            table.setdefault("before_context", "")
            table.setdefault("after_context", "")
            continue
        end = start + len(table_text)
        table["position_range"] = [start, end]
        table.setdefault("before_context", "")
        table.setdefault("after_context", "")
        ranges.append((start, end, idx))
        search_from = end

    if not ranges:
        return

    spans = _line_spans_by_newlines(full_text)
    if not spans:
        return
    ranges.sort()

    line_is_table = [False] * len(spans)
    range_idx = 0
    for line_idx, span in enumerate(spans):
        while range_idx < len(ranges) and ranges[range_idx][1] <= span[0]:
            range_idx += 1
        probe = range_idx
        while probe < len(ranges) and ranges[probe][0] < span[1]:
            if _span_overlaps_range(span, (ranges[probe][0], ranges[probe][1])):
                line_is_table[line_idx] = True
                break
            probe += 1

    for start, end, table_idx in ranges:
        first_line = _line_index_for_position(spans, start)
        last_line = _line_index_for_position(spans, max(start, end - 1))

        before: list[str] = []
        line_idx = first_line - 1
        while line_idx >= 0 and len(before) < pre_lines:
            if line_is_table[line_idx]:
                break
            line = _clean_neighbor_context_line(full_text[spans[line_idx][0]:spans[line_idx][1]])
            if line:
                before.append(line)
            line_idx -= 1
        before.reverse()

        after: list[str] = []
        line_idx = last_line + 1
        while line_idx < len(spans) and len(after) < post_lines:
            if line_is_table[line_idx]:
                break
            line = _clean_neighbor_context_line(full_text[spans[line_idx][0]:spans[line_idx][1]])
            if line:
                after.append(line)
            line_idx += 1

        tables[table_idx]["before_context"] = "\n".join(before)
        tables[table_idx]["after_context"] = "\n".join(after)


def _page_markdown_block(page_num: int, content: str) -> str:
    """Wrap one page's Markdown with the marker consumed by Finengine."""
    return f"===PAGE {page_num}===\n{content.strip()}"


def _page_content_from_response(payload: dict[str, Any]) -> str:
    """Return per-page OCR text, accepting both markdown and html IDP shapes."""
    content = ""
    if isinstance(payload, dict) and "modelOCR" in payload:
        for sub in payload.get("modelOCR", {}).get("pages", []) or []:
            if not isinstance(sub, dict):
                continue
            md = sub.get("markdown", {})
            html = sub.get("html", {})
            if isinstance(md, dict) and md.get("content"):
                content += md["content"] + "\n\n"
            elif isinstance(html, dict) and html.get("content"):
                content += html["content"] + "\n\n"
    elif isinstance(payload, dict) and "content" in payload:
        content = payload["content"]
    return content


def _content_from_saved_page(page: dict[str, Any]) -> str:
    """Return OCR text from a normalized saved page object."""
    for key in ("markdown", "text", "content"):
        value = page.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            nested = value.get("content") or value.get("text") or value.get("markdown")
            if isinstance(nested, str) and nested.strip():
                return nested
    return ""


def _page_num_from_saved_page(page: dict[str, Any], fallback: int) -> int:
    for key in ("page", "page_num", "page_number", "number"):
        try:
            value = int(page.get(key) or 0)
        except Exception:
            value = 0
        if value > 0:
            return value
    return fallback


def _markdown_from_normalized_ocr_result(ocr_result: dict[str, Any]) -> str:
    """Build paginated markdown from normalized `{ocr_result: ...}` sidecars."""
    text = ocr_result.get("text")
    if isinstance(text, str) and text.strip():
        # Newer sidecars already store the final prompt text with page markers.
        if "===PAGE " in text:
            return text

    pages = ocr_result.get("pages")
    if isinstance(pages, list) and pages:
        page_parts: list[tuple[int, str]] = []
        for idx, page_obj in enumerate(pages, start=1):
            if not isinstance(page_obj, dict):
                continue
            page_num = _page_num_from_saved_page(page_obj, idx)
            content = _content_from_saved_page(page_obj)
            if not content:
                continue
            content_md, _tables = _normalize_tables_in_text(content)
            page_parts.append((page_num, _page_markdown_block(page_num, content_md)))
        if page_parts:
            return "\n\n".join(part for _page_num, part in sorted(page_parts, key=lambda item: item[0]))

    return text if isinstance(text, str) else ""


def markdown_from_saved_idp_response(raw: dict) -> str:
    """Build the paginated Markdown sidecar from a saved merged IDP response."""
    if isinstance(raw, dict):
        ocr_result = raw.get("ocr_result")
        if isinstance(ocr_result, dict):
            normalized_markdown = _markdown_from_normalized_ocr_result(ocr_result)
            if normalized_markdown:
                return normalized_markdown
        text = raw.get("text")
        if isinstance(text, str) and text.strip():
            return text

    parts: list[str] = []
    results = raw.get("results", []) if isinstance(raw, dict) else []
    for result in sorted(results, key=lambda item: item.get("page", 0)):
        if not isinstance(result, dict) or not result.get("success"):
            continue
        page_num = int(result.get("page") or 0)
        payload = result.get("response", {})
        content = _page_content_from_response(payload)
        if content:
            content_md, _tables = _normalize_tables_in_text(content)
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
        # Pull markdown/html content from the per-page response.
        content = _page_content_from_response(payload)
        if not content:
            failed_pages.append({"page": page_num, "error": "empty markdown"})
            continue

        # Normalize HTML/native markdown tables and replace them in page text.
        content_md, all_tables = _normalize_tables_in_text(content)

        pages.append(OcrPage(page=page_num, markdown=content_md, tables=[t["text"] for t in all_tables]))
        full_md_parts.append(_page_markdown_block(page_num, content_md))
        for table in all_tables:
            flat_tables.append({
                "idx": len(flat_tables),
                "page": page_num,
                "text": table["text"],
                "original_text": table.get("original_text") or table["text"],
            })

    diag = {
        "pages_total": pages_count,
        "pages_success": len(pages),
        "pages_failed": len(failed_pages),
        "failed_samples": failed_pages[:5],
        "ocr_total_s": ocr_total_s,
        "tables_total": len(flat_tables),
        "parallel": parallel,
    }

    full_text = "\n\n".join(full_md_parts)
    _attach_table_positions_and_context(full_text, flat_tables)

    return OcrResult(
        pages=pages,
        full_text=full_text,
        full_tables=flat_tables,
        diag=diag,
    )
