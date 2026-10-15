"""US GAAP · Non-financial · English — Phase F: 10-K specifics in prompt.

Cell owns: english prompts + metric registry. Pins company_type to
non_financial, REPORTING_STANDARD to "US GAAP".

Phase timeline for this cell:
- D-5: native english prompt (off the legacy russian assembly)
- F (this commit): system prompt extended with US-GAAP-NF-specific
  guidance — ASC codification references, 10-K structure pointer,
  GAAP-over-non-GAAP preference, non-calendar fiscal year heads-up
"""

from __future__ import annotations

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
from finengine.skills.extractors._shared.segments import SEGMENT_METRICS_US_GAAP_EN


REPORTING_STANDARD = "US GAAP"
RUNTIME_STANDARD = "us_gaap_en"
SEGMENT_METRICS: set[str] = SEGMENT_METRICS_US_GAAP_EN
DEFAULT_UNIT = "млн. $"
FALLBACK_PERIOD_LABEL = "Current"


_US_GAAP_NF_SPECIFICS = """
## Additional guardrails

1. **Audited statements over MD&A.** Audited numbers usually live in **Item 8 (Financial Statements and Supplementary Data)** and the **Notes to Consolidated Financial Statements**. Item 7 (MD&A) discusses results in prose; treat its numbers as illustrative — prefer Item 8 figures when both exist.

2. **GAAP over non-GAAP.** Many issuers disclose adjusted / non-GAAP figures with reconciliations. Use the **GAAP** value (the one labelled GAAP / per US GAAP / unadjusted) unless the metric description explicitly asks for a non-GAAP measure. The reconciliation table usually has both columns side-by-side.

3. **Non-calendar fiscal years.** When you see "fiscal 2024", the actual ended date matters — return the literal period header ("Year Ended September 30, 2024") and let the user's Python normalizer decide on `FY 2024` vs `12M 30.09.2024`. Never label a fiscal year ended outside 31.12 as plain `FY`.

4. **Restatements.** When the same metric appears in the current filing with a comparative period AND in last year's filing as the current period, take the current filing version — restatements supersede.
"""


_US_GAAP_NF_SEGMENT_SPECIFICS = """
US GAAP non-financial segment specifics:
- Prefer the ASC 280 / reportable segment note when the report has one. It is
  usually the company's own operating segment view.
- A reportable segment table can disclose several requested metrics at once
  (for example revenues, costs/deductions, operating income or net income).
  Match every relevant row in that table against the requested metric registry.
- Geography or product tables are valid segment disclosures too. If the report
  also has cross-disclosure rows/columns, extract them only when the current
  segment mode asks for additional cross-disclosures.
"""


def _build_system_prompt(reporting_standard: str = REPORTING_STANDARD) -> str:
    return build_legacy_en_system_prompt(reporting_standard) + "\n\n" + _US_GAAP_NF_SPECIFICS.strip()


def _build_user_prompt(**kwargs) -> str:
    kwargs.setdefault("include_non_financial_industry_note", True)
    return build_legacy_en_user_prompt(**kwargs)


def _get_metric_enums() -> set[str]:
    _, metric_enums_set = resolve_cell_registry("us_gaap_nf_en", None)
    return metric_enums_set


async def stream(ctx: ExtractContext) -> AsyncGenerator[dict, None]:
    from finengine.skills.bulk_extractor import _extract_stream_full_context

    reg, metric_enums_set = resolve_cell_registry("us_gaap_nf_en", ctx.metric_enums)

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
        segment_rules_extra=_US_GAAP_NF_SEGMENT_SPECIFICS,
        default_unit_override=DEFAULT_UNIT,
        fallback_period_label_override=FALLBACK_PERIOD_LABEL,
        registry_override=reg,
        reporting_standard_override=RUNTIME_STANDARD,
    ):
        yield event


register("us_gaap_nf_en", stream)
