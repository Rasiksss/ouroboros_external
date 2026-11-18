"""Строки контекста вокруг таблицы OCR — срез ранжировщика Finengine (#257).

ЗАЧЕМ ФАЙЛ ПОЯВИЛСЯ. Двухстадийная схема на первой стадии выбирает НОМЕРА
ТАБЛИЦ, и чтобы модель понимала, что за таблица под номером, ей подаются
соседние строки текста — заголовок над таблицей и подпись под ней. Делает это
`table_neighbor_context_lines`, и без неё стадия сжатия обесценивается.

⚠️ ЭТО БЫЛ СКРЫТЫЙ БЛОКЕР, а не недостающее удобство. `_extract_tables_from_ocr_payload`
импортирует её БЕЗ защиты, и вызывается она с боевого пути
(`_extract_stream_two_stage_full_context` → `_build_report_tables_for_two_stage`).
Прогон падал бы `ModuleNotFoundError` на любом документе, у которого есть
OCR-payload. Не поймали раньше потому, что на пути станции текст приезжает
файлом, payload пуст, и функция выходит раньше импорта.

ПОЧЕМУ СРЕЗ, А НЕ МОДУЛЬ ЦЕЛИКОМ. У них файл 706 строк, и остальные 550 —
командная строка ранжировщика отраслей кредитного портфеля: argparse, csv,
ProcessPoolExecutor, своя система баллов. Это другая работа, у нас не
запускаемая. Замыкание нужной функции замерено обходом дерева: 10 объявлений,
взяты дословно и в порядке источника.

⚠️ НЕ ПУТАТЬ с ранжировщиком состава группы из #251 — там доказано, что он
меряет не тот признак. Здесь баллов нет вовсе: только границы строк и таблиц.
"""
from __future__ import annotations

import re
from typing import Any


def _tables(payload: dict[str, Any]) -> list[dict[str, Any]]:
    tables = payload.get("tables") or []
    return [t for t in tables if isinstance(t, dict)]

def _line_spans_by_newlines(text: str) -> list[tuple[int, int]]:
    """
    OCR line/paragraph spans. A boundary is a run of 1..4 newline chars as requested.
    Longer runs still create boundaries through repeated matches in practice, but the
    1..4 rule is what matters for table-neighbor context.
    """
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

def _valid_position_range(table: dict[str, Any], full_text_len: int) -> tuple[int, int] | None:
    pr = table.get("position_range")
    if not (isinstance(pr, list) and len(pr) >= 2 and all(isinstance(x, int) for x in pr[:2])):
        return None
    start, end = max(0, pr[0]), min(full_text_len, pr[1])
    if end <= start:
        return None
    return start, end

def _table_ranges(tables: list[dict[str, Any]], full_text_len: int) -> list[tuple[int, int, int]]:
    ranges: list[tuple[int, int, int]] = []
    for idx, table in enumerate(tables):
        pr = _valid_position_range(table, full_text_len)
        if pr is not None:
            ranges.append((pr[0], pr[1], idx))
    return sorted(ranges)

def _span_overlaps_range(span: tuple[int, int], range_: tuple[int, int]) -> bool:
    a, b = span
    start, end = range_
    return a < end and b > start

def _span_contains_table(span: tuple[int, int], ranges: list[tuple[int, int, int]], current_idx: int | None = None) -> bool:
    for start, end, idx in ranges:
        if current_idx is not None and idx == current_idx:
            continue
        if _span_overlaps_range(span, (start, end)):
            return True
    return False

def _line_index_for_position(spans: list[tuple[int, int]], pos: int) -> int:
    import bisect
    starts = [a for a, _ in spans]
    return max(0, bisect.bisect_right(starts, pos) - 1)

def _line_table_flags(spans: list[tuple[int, int]], ranges: list[tuple[int, int, int]]) -> list[bool]:
    """Return True for OCR lines that intersect any table range. Single-pass, file-level."""
    flags = [False] * len(spans)
    if not spans or not ranges:
        return flags
    j = 0
    for i, (a, b) in enumerate(spans):
        while j < len(ranges) and ranges[j][1] <= a:
            j += 1
        k = j
        while k < len(ranges) and ranges[k][0] < b:
            if a < ranges[k][1] and b > ranges[k][0]:
                flags[i] = True
                break
            k += 1
    return flags

def _clean_lines_around_table(
    full_text: str,
    table: dict[str, Any],
    table_index: int,
    spans: list[tuple[int, int]],
    ranges: list[tuple[int, int, int]],
    pre_lines: int,
    post_lines: int,
    line_is_table: list[bool] | None = None,
    stop_at_neighbor_table: bool = False,
) -> tuple[list[str], list[str]]:
    """
    Return the first N non-table OCR lines above and below the current table.
    If a nearby line intersects any neighboring OCR table by position_range, skip it
    and do not count it toward N.
    """
    pr = _valid_position_range(table, len(full_text))
    if pr is None or not spans:
        return [], []
    start, end = pr
    first_i = _line_index_for_position(spans, start)
    last_i = _line_index_for_position(spans, end)

    pre: list[str] = []
    i = first_i - 1
    while i >= 0 and len(pre) < pre_lines:
        span = spans[i]
        is_neighbor_table = line_is_table[i] if line_is_table is not None else _span_contains_table(span, ranges, current_idx=table_index)
        if is_neighbor_table and stop_at_neighbor_table:
            break
        if not is_neighbor_table:
            line = full_text[span[0]:span[1]].strip()
            if line:
                pre.append(line)
        i -= 1
    pre.reverse()

    post: list[str] = []
    i = last_i + 1
    while i < len(spans) and len(post) < post_lines:
        span = spans[i]
        is_neighbor_table = line_is_table[i] if line_is_table is not None else _span_contains_table(span, ranges, current_idx=table_index)
        if is_neighbor_table and stop_at_neighbor_table:
            break
        if not is_neighbor_table:
            line = full_text[span[0]:span[1]].strip()
            if line:
                post.append(line)
        i += 1
    return pre, post

def table_neighbor_context_lines(
    payload: dict[str, Any],
    table_index: int,
    *,
    pre_lines: int = 1,
    post_lines: int = 1,
) -> tuple[list[str], list[str]]:
    """Return clean OCR text lines around a table using ``position_range``.

    Neighboring tables are detected from all OCR table ranges; context
    collection stops there and does not jump through another table just to find
    text.
    """
    full_text = payload.get("text") if isinstance(payload.get("text"), str) else ""
    tables = _tables(payload)
    if not full_text or not (0 <= table_index < len(tables)):
        return [], []
    spans = _line_spans_by_newlines(full_text)
    ranges = _table_ranges(tables, len(full_text))
    line_is_table = _line_table_flags(spans, ranges)
    return _clean_lines_around_table(
        full_text,
        tables[table_index],
        table_index,
        spans,
        ranges,
        pre_lines,
        post_lines,
        line_is_table=line_is_table,
        stop_at_neighbor_table=True,
    )

