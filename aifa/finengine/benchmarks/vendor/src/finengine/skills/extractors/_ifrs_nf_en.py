"""IFRS · Non-financial · English — native EN prompts.

Cell owns: english prompts + NF registry. Pins company_type to
non_financial, REPORTING_STANDARD to "IFRS".

Phase D-6: switched off legacy russian assembly. Foreign IFRS-EN
reports now get an english system + user prompt parallel to the
russian assembly. Future Phase D-N: IFRS-specific phrasing (IAS 1 /
IFRS 8 segment / IFRS 9 ECL) once team has GT.
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


REPORTING_STANDARD = "IFRS"
RUNTIME_STANDARD = "ifrs_en"
# Segment scope is an independent extraction policy. Keep the validated
# English set until an IFRS-EN segment benchmark justifies changing it.
SEGMENT_METRICS: set[str] = SEGMENT_METRICS_US_GAAP_EN
DEFAULT_UNIT = "млн. $"
FALLBACK_PERIOD_LABEL = "Current"


_IFRS_NF_EN_SPECIFICS = """
## Additional guardrails

1. **Currency presentation matters a lot.** The unit_text MUST capture the exact currency symbol/code — never default to "$" when the table shows GBP/£, EUR/€, JPY/¥, CHF, KRW or another reporting currency.

2. **Non-calendar fiscal years.** Return the non-modified period header ("Year ended 31 March 2024", "Year ended 30 June 2025", etc.) and let user's normalizer decide on FY vs shifted 12M.

3. **"Underlying" / "Adjusted" / "Core" measures are non-IFRS.** Use statutory / reported numbers unless the metric explicitly requires adjusted. When a table has statutory and underlying columns side-by-side, use the statutory column.

4. **Two-column local + IFRS presentation.** If local-GAAP and IFRS columns are shown side-by-side, use the IFRS column.
"""


def _build_system_prompt(reporting_standard: str = REPORTING_STANDARD) -> str:
    return build_legacy_en_system_prompt(reporting_standard) + "\n\n" + _IFRS_NF_EN_SPECIFICS.strip()


def _build_user_prompt(**kwargs) -> str:
    kwargs.setdefault("include_non_financial_industry_note", True)
    return build_legacy_en_user_prompt(**kwargs)


def _get_metric_enums() -> set[str]:
    _, metric_enums_set = resolve_cell_registry("ifrs_nf_en", None)
    return metric_enums_set


async def stream(ctx: ExtractContext) -> AsyncGenerator[dict, None]:
    from finengine.skills.bulk_extractor import _extract_stream_full_context

    reg, metric_enums_set = resolve_cell_registry("ifrs_nf_en", ctx.metric_enums)

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
        default_unit_override=DEFAULT_UNIT,
        fallback_period_label_override=FALLBACK_PERIOD_LABEL,
        registry_override=reg,
        reporting_standard_override=RUNTIME_STANDARD,
    ):
        yield event


register("ifrs_nf_en", stream)
