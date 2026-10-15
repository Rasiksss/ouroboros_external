"""Ind AS · Non-financial · English — native EN prompts.

Cell owns: english prompts + Ind AS NF registry. Pins company_type to
non_financial, REPORTING_STANDARD to "Ind AS".
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import AsyncGenerator

from finengine.skills.extractors._base import ExtractContext, register
from finengine.skills.extractors._shared.legacy_assemblies import (
    build_legacy_en_system_prompt,
    build_legacy_en_user_prompt,
)
from finengine.skills.extractors._shared.registry import (
    resolve_cell_registry,
)


REPORTING_STANDARD = "Ind AS"
RUNTIME_STANDARD = "ind_as_en"
SEGMENT_METRICS: set[str] = {
    "SALES_REVENUE",
    "GROSS_PROFIT",
    "COSTS_EXPENSES",
    "OPERATING_PROFIT",
    "EBITDA",
    "NET_INCOME",
}
DEFAULT_UNIT = "INR"
FALLBACK_PERIOD_LABEL = "Current"
PERIOD_CALENDAR = "indian_fiscal"


_IND_AS_NF_EN_SPECIFICS = """
## Additional guardrails

1. **Ind AS statutory column wins.** Many Indian annual reports mention other accounting principles generally accepted in India alongside Ind AS. When standalone/local-GAAP and Ind AS presentations appear side by side, use the Ind AS / consolidated statutory numbers.

2. **Consolidated-only is a hard rule.** Extract consolidated-only indicators. If a report contains both standalone and consolidated statements, use only the consolidated statement / consolidated notes values unless a metric explicitly asks otherwise. Standalone statements often appear earlier in Indian annual reports; keep searching for the later consolidated statements and ignore every table, note or auditor section headed "Standalone". If the same metric appears in an early Board's Report/standalone section and later in consolidated financial statements or consolidated notes, choose the later consolidated value. Use standalone values only when the entire report has no consolidated statements.

3. **Indian numbering units matter.** The unit_text MUST capture the exact unit from the source: crore, lakh, arab, million, Rs., INR, or the currency symbol. Do not silently rewrite crore/lakh/arab to million in the model response.

4. **March year-end is common.** Keep literal period headers such as "Year ended 31 March 2025" or "Half year ended 30 September 2025" and let Python normalize them later.

5. **Statutory over adjusted.** Use reported / statutory values unless the registry explicitly asks for an adjusted metric. Do not swap in management-adjusted EBITDA or similar non-GAAP presentations.

6. **Cash-flow outflow signs.** For cash-flow outflows such as CAPEX, dividends paid and principal lease repayments, prefer the signed row from the primary Cash Flow Statement. Do not strip parentheses or minus signs, and do not replace a signed cash-flow row with a positive MD&A/note total.

7. **Gross profit in Ind AS.** If "Gross profit" is not printed, use the direct-cost block in the Statement of Profit and Loss: revenue from operations less cost of materials consumed, purchases of stock-in-trade, changes in inventories and similar direct service/product costs. Do not derive gross profit from operating profit or profit before tax.

8. **Financial income is not total other income.** Use finance income / interest income only. Do not use total other income, other operating income, fair value gains, foreign exchange gains or sale-of-investment gains unless the finance-income note explicitly includes them.

9. **Operating profit is statutory.** Prefer an explicit statutory Statement of Profit and Loss subtotal before finance income, finance costs and tax. If the statutory P&L does not print operating profit/profit from operations, use the consolidated segment result/profit TOTAL before finance costs and tax. Do not use EBITDA, profit before tax or management adjusted metrics.

10. **Mixed quarterly table headers.** In quarterly-result tables, read each column header independently. If one column says "Quarter ended 30 June 2024", keep that literal quarter header even when a neighboring column says "Year ended 31 March 2025"; never relabel a comparative quarter as "Year ended 30 June 2024" by copying the annual header.

11. **Annual comparative columns are independent.** In annual statements with current and prior columns such as "31 March 2025" and "31 March 2024", read each column separately. Never copy the current-year cell into the prior-year period just because the row label is the same.
"""


_IND_AS_NF_SEGMENT_SPECIFICS = """
Ind AS non-financial segment specifics:
- Use consolidated segment, product and geography disclosures; ignore standalone-only segment tables when consolidated disclosures are available.
- Segment rows may appear in the notes to consolidated financial statements, revenue disaggregation notes or management discussion tables.
- Product/geography rows are valid segment disclosures when they are explicit rows or columns in the source.
- For geographic revenue, prefer a segmental information / geographical market table that reconciles to total revenue from operations, including other operating revenue. Do not replace it with a revenue-from-contracts-only disaggregation note when both are present.
- Keep geography labels as shown in the chosen table. If the chosen segmental table says "Outside India", do not rename it to "Others".
- For OPERATING_PROFIT, use the segment result/profit row only inside the segment stage; do not use EBITDA, PBT or adjusted management profit as a substitute.
"""


_STANDALONE_SECTION_MARKERS = (
    "report on the audit of the standalone financial statements",
    "standalone balance sheet",
    "standalone statement of profit and loss",
    "statement of standalone financial results",
    "standalone audited financial results",
    "standalone unaudited financial results",
)

_CONSOLIDATED_SECTION_MARKERS = (
    "report on the audit of the consolidated financial statements",
    "consolidated balance sheet",
    "consolidated statement of financial position",
    "statement of consolidated financial results",
    "consolidated audited financial results",
    "consolidated unaudited financial results",
)


def _first_marker_pos(text_lower: str, markers: tuple[str, ...], *, after: int = -1) -> int:
    positions = [
        pos
        for marker in markers
        if (pos := text_lower.find(marker, max(after, 0))) >= 0
    ]
    return min(positions) if positions else -1


def _prioritize_consolidated_report_text(pdf_text_for_prompt: str) -> str:
    """Move a later consolidated annual-report section before standalone pages.

    Indian annual reports commonly print standalone financial statements first and
    consolidated statements much later. The prompt already says consolidated-only,
    but long reports still bias the model toward the first full statement block.
    Reordering keeps the full text and page markers while giving the correct
    section first attention.
    """
    text = pdf_text_for_prompt or ""
    text_lower = text.lower()
    standalone_pos = _first_marker_pos(text_lower, _STANDALONE_SECTION_MARKERS)
    if standalone_pos < 0:
        return text

    consolidated_pos = _first_marker_pos(
        text_lower,
        _CONSOLIDATED_SECTION_MARKERS,
        after=standalone_pos + 1,
    )
    if consolidated_pos < 0:
        return text

    # Keep the first consolidated page marker attached to its contents. Cutting
    # at the heading itself makes citations from that page point at the preceding
    # standalone block after reordering.
    page_markers = list(re.finditer(r"(?m)^===PAGE\s+\d+===\s*$", text[:consolidated_pos]))
    if page_markers:
        consolidated_pos = page_markers[-1].start()

    return (
        "# Consolidated financial statements moved first for extraction priority\n\n"
        + text[consolidated_pos:].lstrip()
        + "\n\n# Earlier standalone and overview sections moved after consolidated section\n\n"
        + text[:consolidated_pos].rstrip()
    )


def _build_system_prompt(reporting_standard: str = REPORTING_STANDARD) -> str:
    return build_legacy_en_system_prompt(reporting_standard) + "\n\n" + _IND_AS_NF_EN_SPECIFICS.strip()


def _build_user_prompt(**kwargs) -> str:
    kwargs.setdefault("include_non_financial_industry_note", True)
    kwargs["pdf_text_for_prompt"] = _prioritize_consolidated_report_text(
        kwargs.get("pdf_text_for_prompt", "")
    )
    return build_legacy_en_user_prompt(**kwargs)


def _get_metric_enums() -> set[str]:
    _, metric_enums_set = resolve_cell_registry("ind_as_nf_en", None)
    return metric_enums_set


async def stream(ctx: ExtractContext) -> AsyncGenerator[dict, None]:
    from finengine.skills.bulk_extractor import _extract_stream_full_context

    reg, metric_enums_set = resolve_cell_registry("ind_as_nf_en", ctx.metric_enums)

    debug_dir = Path(ctx.debug_dir) if ctx.debug_dir else None

    async for event in _extract_stream_full_context(
        ctx.doc_id,
        "non_financial",
        index=ctx.index,
        limit=ctx.limit,
        model_id=ctx.model_id,
        include_segments=ctx.include_segments,
        include_consolidated_group=ctx.include_consolidated_group,
        metric_enums=metric_enums_set,
        full_registry=True,
        role=ctx.role,
        segment_mode=ctx.segment_mode,
        segment_context_mode=ctx.segment_context_mode,
        debug_dir=debug_dir,
        system_prompt_override=_build_system_prompt(),
        user_prompt_builder=_build_user_prompt,
        segment_metrics_override=SEGMENT_METRICS,
        segment_rules_extra=_IND_AS_NF_SEGMENT_SPECIFICS,
        default_unit_override=DEFAULT_UNIT,
        fallback_period_label_override=FALLBACK_PERIOD_LABEL,
        registry_override=reg,
        period_calendar=PERIOD_CALENDAR,
        reporting_standard_override=RUNTIME_STANDARD,
    ):
        yield event


register("ind_as_nf_en", stream)
