"""IFRS · Bank · English — native EN prompts.

Cell owns: english prompts + bank registry. Pins company_type to bank,
REPORTING_STANDARD to "IFRS".

Phase D-6: switched off legacy russian assembly. Future Phase D-N:
Basel III / IFRS 9 ECL phrasing once GT lands.
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


REPORTING_STANDARD = "IFRS"
RUNTIME_STANDARD = "ifrs_en"
# Banks short-circuit the segment LLM stage; this set governs only
# main-extract total fallbacks and is independent of standard routing.
SEGMENT_METRICS: set[str] = SEGMENT_METRICS_US_GAAP_EN
DEFAULT_UNIT = "млн. $"
FALLBACK_PERIOD_LABEL = "Current"


_IFRS_BANK_EN_SPECIFICS = """
## Additional guardrails

1. **IFRS 9 ECL — three-stage model.** Different from US GAAP CECL:
   - **Stage 1** (12-month ECL): performing, no significant deterioration
   - **Stage 2** (lifetime ECL): significant deterioration but still performing
   - **Stage 3** (credit-impaired): NPL / non-performing
   - POCI: purchased / originated credit-impaired
   Look for "Stage 1 / 2 / 3" headers in the impairment notes. Don't
   confuse the gross carrying amount (GCA) with the net carrying amount
   (NCA, after ECL allowance).

2. **Capital disclosures.** CET1, Tier 1, Total capital, leverage and RWA
   usually live in capital-management / regulatory-capital notes, separate
   from the P&L and balance sheet. Do not mix capital amounts, ratios and RWA.

3. **Loan categorization (different from US):**
   - **Wholesale / Corporate & Institutional Banking** vs **Retail Banking**
   - **SME** / Small & Medium Enterprises (often a separate segment)
   - **Mortgages** (residential)
   - **Cards & Unsecured Lending** (consumer)
   - **Corporate** (commercial loans)

4. **Multi-currency presentation.** The unit_text MUST capture the exact symbol/code — never default to "$" when the table shows GBP/£, EUR/€, JPY/¥, CHF or another reporting currency.

5. **Non-calendar fiscal years.** Return non-modified period header and let the user normalize it themselves.

6. **Underlying vs Statutory.** Use statutory / reported numbers unless the metric explicitly asks for adjusted. When a table has statutory and underlying columns side-by-side, use the statutory column.
"""


def _build_system_prompt(reporting_standard: str = REPORTING_STANDARD) -> str:
    return build_legacy_en_system_prompt(reporting_standard) + "\n\n" + _IFRS_BANK_EN_SPECIFICS.strip()


def _build_user_prompt(**kwargs) -> str:
    return build_legacy_en_user_prompt(**kwargs)


def _get_metric_enums() -> set[str]:
    # Curated registry; must match what stream() hands to legacy via
    # resolve_cell_registry below.
    _, metric_enums_set = resolve_cell_registry("ifrs_bank_en", None)
    return metric_enums_set


async def stream(ctx: ExtractContext) -> AsyncGenerator[dict, None]:
    from finengine.skills.bulk_extractor import _extract_stream_full_context

    reg, metric_enums_set = resolve_cell_registry("ifrs_bank_en", ctx.metric_enums)

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


register("ifrs_bank_en", stream)
