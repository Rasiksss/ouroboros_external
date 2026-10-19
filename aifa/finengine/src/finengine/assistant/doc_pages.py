"""Распознанный текст документа по страницам: разметка `===PAGE N===` ставит OCR (`skills/meta_card.py`).

Чистые функции: берут текст, ничего не читают из хранилища. Ответ инструмента из них строит `ouro_view`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PAGE_MARK = re.compile(r"(?m)^===PAGE\s+(\d+)===\n?")
# Текст без разметки — части по столько знаков: ориентир для просьбы «следующая часть» (CONTRACTS.md §3.0).
CHUNK_CHARS = 8_000
MATCH_LIMIT = 20
MATCH_CONTEXT = 160


@dataclass(frozen=True)
class Page:
    number: int
    text: str


@dataclass(frozen=True)
class Match:
    page: int
    fragment: str


def pages_of(text: str) -> list[Page]:
    """Страницы по разметке OCR; без неё — части по `CHUNK_CHARS` знаков с номерами с 1."""
    marks = list(_PAGE_MARK.finditer(text))
    if not marks:
        return [Page(index, text[start : start + CHUNK_CHARS]) for index, start in enumerate(range(0, len(text), CHUNK_CHARS), 1)]
    return [
        Page(int(mark.group(1)), text[mark.end() : marks[index + 1].start() if index + 1 < len(marks) else len(text)])
        for index, mark in enumerate(marks)
    ]


def page_range(spec: str, pages: list[Page]) -> list[Page] | None:
    """`"3"`, `"3-5"` → страницы документа в этих пределах; `None` — запись не по формату."""
    found = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", spec)
    if found is None:
        return None
    first = int(found.group(1))
    last = int(found.group(2) or first)
    return [page for page in pages if first <= page.number <= last]


def find(query: str, pages: list[Page]) -> list[Match]:
    """Места, где встречается `query` (без учёта регистра): страница и фрагмент вокруг; не больше `MATCH_LIMIT`."""
    needle = query.casefold()
    matches: list[Match] = []
    for page in pages:
        haystack = page.text.casefold()
        start = haystack.find(needle)
        while start != -1 and len(matches) < MATCH_LIMIT:
            left, right = max(0, start - MATCH_CONTEXT), start + len(needle) + MATCH_CONTEXT
            matches.append(Match(page.number, " ".join(page.text[left:right].split())))
            start = haystack.find(needle, start + len(needle) + MATCH_CONTEXT)
    return matches
