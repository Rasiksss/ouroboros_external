"""Поиск кандидатов отчётности — Срез 7 A5.

Тонкая обёртка над уже существующим `skills.web_search` (Tavily) — не
изобретаем свой поисковик; тот же принцип, что `explain.external` (Срез 6b):
один заранее известный внешний узел (`api.tavily.com`), список узлов —
обязанность вызывающего атома (A4/решение №9).

Поиск отдаёт ТОЛЬКО кандидатов (url/title/сниппет) — про безопасность и
содержимое ничего не знает: этим занимаются `url_safety`/`downloader` дальше
по конвейеру."""
from __future__ import annotations

from typing import Awaitable, Callable

from finengine.skills.web_search import WebSearchResult, web_search

from .policy import SearchCandidate

Searcher = Callable[..., Awaitable[list[WebSearchResult]]]


def _report_query(company: str, *, period: str = "") -> str:
    parts = [company, period, "financial report annual report filetype:pdf"]
    return " ".join(p for p in parts if p)


async def search_report_candidates(
    company: str, *, period: str = "", max_results: int = 8, searcher: Searcher = web_search,
) -> list[SearchCandidate]:
    """Найти кандидатов на отчётность компании. `searcher` — точка подмены
    для тестов (реальный Tavily-запрос не нужен в тестовом контуре)."""
    query = _report_query(company, period=period)
    results = await searcher(query, max_results=max_results)
    return [
        SearchCandidate(url=r.url, title=r.title, snippet=r.content, search_score=r.score)
        for r in results if r.url
    ]


__all__ = ["search_report_candidates"]
