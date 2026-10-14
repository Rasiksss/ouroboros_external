"""US GAAP · Bank · English — Phase D-5: native EN prompts.

Cell owns: english prompts + bank registry. Pins company_type to bank,
REPORTING_STANDARD to "US GAAP".

Phase D-5: switched off legacy russian assembly, now ships native
english prompts. JPM / BofA / Citi no longer get a russian intro.
Future Phase D-N: CECL / Basel-III / FDIC-specific phrasing once
team has GT for bench validation.
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
    registry_has_extractable,
    resolve_cell_registry,
)
from finengine.skills.extractors._shared.segments import SEGMENT_METRICS_US_GAAP_EN


REPORTING_STANDARD = "US GAAP"
RUNTIME_STANDARD = "us_gaap_en"
# Banks short-circuit the segment LLM stage; constant still used by main
# extract for total-fallback scope.
SEGMENT_METRICS: set[str] = SEGMENT_METRICS_US_GAAP_EN
DEFAULT_UNIT = "млн. $"
FALLBACK_PERIOD_LABEL = "Current"


_US_GAAP_BANK_EN_SPECIFICS = """
## Additional guardrails

1. **CECL, not IFRS-9 ECL.** Under US GAAP (ASC 326, effective 2020) banks use **Current Expected Credit Loss** — a single lifetime-loss model for ALL loans, not the IFRS 9 Stage 1/2/3 three-bucket structure. Don't look for "Stage 1/2/3" in a 10-K; do look for:
   - **Allowance for credit losses (ACL)** — the CECL allowance on loans HFI
   - **Provision for credit losses (PCL)** — period charge to P&L
   - Legacy term **Allowance for loan and lease losses (ALLL)** in pre-2020 comparatives
   ASC 326 also covers AFS securities, off-balance-sheet credit exposures, and reinsurance recoverables.

2. **Basel III adapted by US regulators.** Capital ratios disclosed:
   - **CET1 ratio** (Common Equity Tier 1)
   - **Tier 1 capital ratio**
   - **Total capital ratio**
   - **SLR** (Supplementary Leverage Ratio) — applies to large banks
   - **LCR** (Liquidity Coverage Ratio), **NSFR** (Net Stable Funding Ratio) — for G-SIBs
   These typically live in the "Capital Management" or "Regulatory Capital" notes, separate from the P&L / balance sheet. Russian Central Bank capital normatives do NOT apply here.

3. **Loan portfolio breakdown.** When the registry asks for a corporate vs retail split and the report provides loan classes, map:
   - Commercial & Industrial (C&I)
   - Commercial Real Estate (CRE)
   - Residential mortgage / 1-4 family
   - Home Equity
   - Credit Cards
   - Auto
   - Construction
   C&I + CRE + Construction are corporate; residential + consumer (cards + auto + home equity) are retail.

4. **Restatements / mergers.** Trust the current filing's restated comparatives over an old standalone filing.

5. **Audited statements over MD&A.** Audited numbers usually live in **Item 8 (Financial Statements and Supplementary Data)** and the **Notes to Consolidated Financial Statements**. Item 7 (MD&A) discusses results in prose; treat its numbers as illustrative — prefer Item 8 figures when both exist.
"""


def _build_system_prompt(reporting_standard: str = REPORTING_STANDARD) -> str:
    return build_legacy_en_system_prompt(reporting_standard) + "\n\n" + _US_GAAP_BANK_EN_SPECIFICS.strip()


def _build_user_prompt(**kwargs) -> str:
    return build_legacy_en_user_prompt(**kwargs)


def _get_metric_enums() -> set[str]:
    _, metric_enums_set = resolve_cell_registry("us_gaap_bank_en", None)
    return metric_enums_set


async def stream(ctx: ExtractContext) -> AsyncGenerator[dict, None]:
    from finengine.skills.bulk_extractor import _extract_stream_full_context

    reg, metric_enums_set = resolve_cell_registry("us_gaap_bank_en", ctx.metric_enums)

    debug_dir = Path(ctx.debug_dir) if ctx.debug_dir else None

    async for event in _extract_stream_full_context(
        ctx.doc_id,
        "bank",
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
        default_unit_override=DEFAULT_UNIT,
        fallback_period_label_override=FALLBACK_PERIOD_LABEL,
        registry_override=reg,
        reporting_standard_override=RUNTIME_STANDARD,
        include_top_borrowers=(
            ctx.include_top_borrowers
            and registry_has_extractable(reg, "TOP_BORROWERS")
        ),
    ):
        yield event


register("us_gaap_bank_en", stream)
