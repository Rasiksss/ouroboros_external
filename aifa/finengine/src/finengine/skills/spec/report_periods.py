"""Периоды заголовков для контекста планирования, без извлечения значений."""

import re

from finengine.skills.bulk_extractor.period_selection import _select_run_periods
from finengine.skills.periods import FiscalPeriodContext, normalize_period_label, sort_periods


_PERIOD = re.compile(
    r"(?:FY |Q[1-4] |[369]M )20\d{2}|(?:[1-9]|1[0-2])M \d{1,2}\.\d{1,2}\.20\d{2}|\d{1,2}\.\d{1,2}\.20\d{2}"
)


def report_periods(text: str) -> tuple[str, ...]:
    """Прочитать шапку основной таблицы; без неё — только контекст обложки."""
    fiscal = FiscalPeriodContext.from_text(text)
    lines = text.splitlines()
    in_header = False
    for index, line in enumerate(lines):
        if "|" not in line:
            in_header = False
            continue
        cells = line.strip().strip("|").split("|")
        if index == 0 or "|" not in lines[index - 1]:
            in_header = True
        elif in_header and all(re.fullmatch(r"\s*:?-+:?\s*", cell) for cell in cells):
            continue
        elif not in_header or cells[0].strip():
            in_header = False
            continue
        labels = [
            normalize_period_label(cell.strip().strip("*"), fiscal_context=fiscal) for cell in cells
        ]
        found = [label for label in labels if _PERIOD.fullmatch(label)]
        if found:
            return tuple(sort_periods(found))
    return tuple(_select_run_periods(text[:4000])[0])
