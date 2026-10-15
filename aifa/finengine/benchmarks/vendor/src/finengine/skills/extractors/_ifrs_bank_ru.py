"""IFRS · Bank · Russian — Phase D-4 specialization.

Cell owns: prompts (system + user) + metric registry. Same level as
ifrs_nf_ru. Pins company_type to bank, REPORTING_STANDARD to "IFRS".

Future cell-only work (Phase D-5+):
- bank-specific russian prompts (Stage 1/2/3 loans, NPL, capital ratios)
- prune NF + US-GAAP branches from bulk_extractor once no caller hits
- specialize segment scope (corp vs retail, by stage, by geography)
"""

from __future__ import annotations

from pathlib import Path
from typing import AsyncGenerator

from finengine.skills.extractors._base import ExtractContext, register
from finengine.skills.extractors._shared.legacy_assemblies import (
    build_legacy_ru_system_prompt,
    build_legacy_ru_user_prompt,
)
from finengine.skills.extractors._shared.registry import (
    registry_has_extractable,
    resolve_cell_registry,
)
from finengine.skills.extractors._shared.segments import SEGMENT_METRICS_IFRS_RU


REPORTING_STANDARD = "IFRS"
RUNTIME_STANDARD = "ifrs_ru"
# Banks don't run the segment LLM stage (effective_type=="bank" short-circuits
# at the top of _extract_segment_breakdown_full_context), but the main extract
# still uses this set to scope total-fallback events.
SEGMENT_METRICS: set[str] = SEGMENT_METRICS_IFRS_RU
DEFAULT_UNIT = "млн. руб."
FALLBACK_PERIOD_LABEL = "Текущий"


_IFRS_BANK_RU_SPECIFICS = """
## Дополнительные guardrails

1. **Проверка P&L арифметикой.** В OCR-таблицах банков иногда съезжают строки. Перед тем как вернуть P&L-метрику, мысленно проверь соседние subtotal:
   - ECL/provision flow должен объяснять разницу между ЧПД до резервов и ЧПД после резервов.
   - `OPERATING_EXPENSES_BANKS`, `FEE_AND_COMMISSION_EXPENSE` и `CHANGE_IN_EXPECTED_CREDIT_LOSSES` как расходы обычно отрицательные, если P&L показывает их в скобках или строка называется «создание/расходы/формирование резерва». Возвращай знак источника даже если OCR потерял скобку в самой ячейке.

2. **Активы с общей строкой.** Если баланс показывает объединённую строку вроде «Основные средства, нематериальные активы и активы в форме права пользования», не используй её для `PPE`, `INTANGIBLE_ASSETS` или `RIGHT_OF_USE_ASSETS`. Иди в примечание и бери отдельную строку/колонку для каждой метрики. Для `PPE` нужна балансовая/остаточная стоимость на конец периода, а не первоначальная стоимость; исключай НМА, goodwill и right-of-use assets.

3. **Относительные заголовки периодов.** Если таблица вместо года пишет «за отчетный период» / «за предшествующий отчетный период» / `current period` / `previous period`, верни периоды из задания: текущий год отчёта и предыдущий год. Не используй такой текст как period label в JSON.

4. **Промежуточная отчётность: не теряй колонки.** В квартальных/полугодовых/9М отчётах P&L часто раскрывает четыре колонки: 3M/Q за текущий и сравнительный год плюс YTD (6M/9M) за текущий и сравнительный год. Для каждой duration-метрики из P&L верни ВСЕ явно раскрытые колонки этой строки: и квартал/3M, и YTD. Не останавливайся на первой паре колонок. Для point-метрик из баланса и примечаний возвращай все даты таблицы, обычно текущую отчётную дату и предыдущий 31 декабря; не превращай такие строки в Q/6M/9M.

5. **Stage/ECL таблицы.** Если в примечании к кредитам есть таблица со стадиями 1/2/3 и колонками gross / ECL allowance / net, извлеки из этой одной таблицы все подходящие метрики: `LOANS_STAGE1_GROSS`, `LOANS_STAGE2_GROSS`, `LOANS_STAGE3_GROSS` из gross-колонок и `ECL_ALLOWANCE_STAGE1`, `ECL_ALLOWANCE_STAGE2`, `ECL_ALLOWANCE_STAGE3` из allowance/ECL-колонок. Не возвращай net carrying amount для gross или allowance. Если стадии раскрыты для нескольких дат, верни все эти даты.
"""


def _build_system_prompt(reporting_standard: str = REPORTING_STANDARD) -> str:
    return build_legacy_ru_system_prompt(reporting_standard) + "\n\n" + _IFRS_BANK_RU_SPECIFICS.strip()


def _build_user_prompt(**kwargs) -> str:
    return build_legacy_ru_user_prompt(**kwargs)


def _get_metric_enums() -> set[str]:
    _, metric_enums_set = resolve_cell_registry("ifrs_bank_ru", None)
    return metric_enums_set


async def stream(ctx: ExtractContext) -> AsyncGenerator[dict, None]:
    from finengine.skills.bulk_extractor import _extract_stream_full_context

    reg, metric_enums_set = resolve_cell_registry("ifrs_bank_ru", ctx.metric_enums)

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


register("ifrs_bank_ru", stream)
