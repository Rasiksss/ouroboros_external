"""Indian GAAP · Bank · English — native EN prompts.

Cell owns: english prompts + Indian GAAP bank registry. Pins company_type to
bank, REPORTING_STANDARD to "Ind GAAP".
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


REPORTING_STANDARD = "Ind GAAP"
RUNTIME_STANDARD = "ind_gaap_en"
SEGMENT_METRICS: set[str] = SEGMENT_METRICS_US_GAAP_EN
DEFAULT_UNIT = "INR"
FALLBACK_PERIOD_LABEL = "Current"
PERIOD_CALENDAR = "indian_fiscal"


_IND_GAAP_BANK_EN_SPECIFICS = """
## Additional guardrails

1. **Indian GAAP / RBI bank statements use local banking vocabulary.** Prefer statutory banking tables with Advances, Deposits, Investments, Borrowings, Other liabilities and provisions, and Profit and Loss Account. Do not force IFRS 9 Stage 1/2/3 labels or US GAAP CECL labels when the report uses Indian GAAP / RBI terminology.

2. **Asset-quality metrics may use NPA/NPI language.** Look for gross NPA, net NPA, provisions for NPAs, standard assets, restructured assets, provision coverage and related RBI disclosures. Map those to the registry metrics carefully instead of substituting generic impairment terms.

3. **Capital and liquidity disclosures can sit outside the main forms.** CET1, Tier 1, CRAR/CAR, RWA, balances with RBI, and money at call and short notice often appear in dedicated regulatory notes. Use those regulatory tables when they are the primary disclosure.

4. **Consolidated-only.** Extract consolidated-only indicators. If a report contains both standalone and consolidated statements, use only the consolidated statement / consolidated notes values unless a metric explicitly asks otherwise.

5. **Indian numbering units matter.** The unit_text MUST capture the exact unit from the source: crore, lakh, arab, million, Rs., INR, or the currency symbol. Do not silently rewrite crore/lakh/arab to million in the model response.

6. **Use statutory figures over investor-summary restatements.** When the annual report contains both statutory schedules and narrative highlights, prefer the statutory schedule / note disclosure.
"""


def _build_system_prompt(reporting_standard: str = REPORTING_STANDARD) -> str:
    return build_legacy_en_system_prompt(reporting_standard) + "\n\n" + _IND_GAAP_BANK_EN_SPECIFICS.strip()


def _build_user_prompt(**kwargs) -> str:
    return build_legacy_en_user_prompt(**kwargs)


def _get_metric_enums() -> set[str]:
    _, metric_enums_set = resolve_cell_registry("ind_gaap_bank_en", None)
    return metric_enums_set


async def stream(ctx: ExtractContext) -> AsyncGenerator[dict, None]:
    from finengine.skills.bulk_extractor import _extract_stream_full_context

    reg, metric_enums_set = resolve_cell_registry("ind_gaap_bank_en", ctx.metric_enums)

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
        period_calendar=PERIOD_CALENDAR,
        reporting_standard_override=RUNTIME_STANDARD,
        include_top_borrowers=(
            ctx.include_top_borrowers
            and registry_has_extractable(reg, "TOP_BORROWERS")
        ),
    ):
        yield event


register("ind_gaap_bank_en", stream)
