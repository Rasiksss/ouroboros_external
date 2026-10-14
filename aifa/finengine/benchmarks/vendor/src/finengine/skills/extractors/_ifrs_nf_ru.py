"""IFRS · Non-financial · Russian — fully specialized cell.

Phase D timeline for this cell:
- D-1: cell owns its system_prompt
- D-2: cell owns its metric registry
- D-3: cell owns its user_prompt assembly
- D-4 (this commit): prompt assemblies extracted into
  _shared/legacy_assemblies — cell now imports them rather than
  inlining, identical for parity. Sibling cells follow the same pattern.

Future cell-specific divergence lands here (e.g. IFRS-NF-RU-specific
phrasing tweaks, NF-only segment scope). Until then this cell is a
representative "legacy assemblies + NF registry + RU prompts" shape.
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
    resolve_cell_registry,
)
from finengine.skills.extractors._shared.segments import SEGMENT_METRICS_IFRS_RU


REPORTING_STANDARD = "IFRS"
RUNTIME_STANDARD = "ifrs_ru"
SEGMENT_METRICS: set[str] = SEGMENT_METRICS_IFRS_RU
DEFAULT_UNIT = "млн. руб."
FALLBACK_PERIOD_LABEL = "Текущий"


_IFRS_NF_RU_SPECIFICS = """
## МСФО / российские нефинансовые отчёты — специфика

1. **МСФО, а не РСБУ.** Российские эмитенты часто публикуют две отчётности —
   РСБУ (бухгалтерская) и МСФО (консолидированная). У них разные правила, и
   числа отличаются. Извлекай только из консолидированной финансовой
   отчётности по МСФО / IFRS. Если видишь шапку «Бухгалтерская отчётность по
   РСБУ», «Бухгалтерский баланс (форма №1)» — это другой документ, в нём
   метрик из реестра НЕ ищи (верни null).

2. **Консолидированные, не отдельные.** Если рядом раскрыты показатели Группы
   и отдельной компании / материнской компании, для групповых метрик бери
   показатели Группы. Отдельную компанию используй только когда это прямо
   требует описание конкретной метрики.

3. **Стандарты МСФО, на которые ссылается LLM.** Это эвристика для понимания
   где в отчёте искать, не keyword-фильтр:
   - выручка: IFRS 15 (Revenue from Contracts with Customers)
   - аренда: IFRS 16 (Leases) — обязательства по аренде + активы в форме права пользования
   - финансовые инструменты / резервы по кредитам: IFRS 9
   - обесценение нефинансовых активов: IAS 36
   - сегментная отчётность: IFRS 8
   - запасы: IAS 2
   - основные средства: IAS 16

4. **Типичные русскоязычные названия строк.** Гибко сопоставляй:
   - «Выручка» / «Выручка от реализации» / «Выручка по основной деятельности» → SALES_REVENUE
   - «Себестоимость» / «Себестоимость продаж» / «Себестоимость реализации» → cost row; не путай с «Операционные расходы»
   - «Валовая прибыль» → GROSS_PROFIT
   - «Прибыль от операционной деятельности» / «Операционная прибыль» / «Прибыль от продаж» → OPERATING_PROFIT
   - «Прибыль до налогообложения» → pre-tax income
   - «Прибыль за год, приходящаяся на акционеров материнской компании» — это net income attributable to parent, а не general NET_INCOME
   - «Базовая прибыль на акцию» → EPS (basic); не путай с разводнённой

5. **Единицы измерения.** Шапка таблицы / колонки указывает буквально:
   - «(в миллионах российских рублей)» → unit_text: «в миллионах российских рублей»
   - «млн руб.» / «млн. руб.» → так и пиши
   - «млрд руб.» — миллиарды; не конвертируй сам, верни буквально
   - редко «в долларах США» / «USD» для некоторых export-ориентированных групп

6. **Периоды на русском.** В ключах `values` возвращай видимый текст из шапки:
   - пиши «За год, закончившийся 31 декабря 2024 года», не `FY 2024`;
   - пиши «По состоянию на 31 декабря 2024 года», не `31.12.2024`;
   - пиши «За 6 месяцев, закончившихся 30 июня 2024», не `6M 2024`;
   - пиши «3 месяца 2024», не `Q1/Q2/Q3/Q4`, если именно так написано в шапке.
   Python сам нормализует эти ключи после ответа.

7. **Знаки в русскоязычных таблицах.** Скобки = негатив: `(465)` → возвращай
   `(465)`. Прочерк «—» в строке найденной метрики → «0», не null. Расходы и
   резервы пишут со скобками — это норма.

8. **Подсанкционные / спецэффекты.** Если значение раскрыто только в
   примечании к спецэффектам, обесценению, продаже бизнеса или санкционным
   событиям, используй его только когда основная финансовая форма не раскрывает
   нужную метрику напрямую.
"""


_IFRS_NF_RU_SEGMENT_SPECIFICS = """
Дополнительные правила по сегментам:
- Не возвращай сверочные строки или колонки как сегменты. Метки вроде
  `Исключения`, `Элиминации`, `Корректировки`, и `Межсегментная деятельность` — это
  механика консолидации или сверки, а не бизнес-сегменты.
- Используй такие строки только чтобы понять, как таблица сходится к `TOTAL`.
  Для сегментной `SALES_REVENUE` предпочитай выручку / продажи внешним
  покупателям или третьим лицам. Не включай внутригрупповую выручку, если она
  раскрыта отдельно; используй общую выручку сегмента только когда строки или
  колонки с выручкой внешним покупателям нет.
"""


def _build_system_prompt(reporting_standard: str = REPORTING_STANDARD) -> str:
    return build_legacy_ru_system_prompt(reporting_standard) + "\n\n" + _IFRS_NF_RU_SPECIFICS.strip()


def _build_user_prompt(**kwargs) -> str:
    kwargs.setdefault("include_non_financial_industry_note", True)
    return build_legacy_ru_user_prompt(**kwargs)


def _get_metric_enums() -> set[str]:
    _, metric_enums_set = resolve_cell_registry("ifrs_nf_ru", None)
    return metric_enums_set


async def stream(ctx: ExtractContext) -> AsyncGenerator[dict, None]:
    """Stream extraction events for an IFRS / non-financial / Russian doc."""
    from finengine.skills.bulk_extractor import _extract_stream_full_context

    reg, metric_enums_set = resolve_cell_registry("ifrs_nf_ru", ctx.metric_enums)

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
        segment_rules_extra=_IFRS_NF_RU_SEGMENT_SPECIFICS,
        default_unit_override=DEFAULT_UNIT,
        fallback_period_label_override=FALLBACK_PERIOD_LABEL,
        registry_override=reg,
        reporting_standard_override=RUNTIME_STANDARD,
    ):
        yield event


register("ifrs_nf_ru", stream)
