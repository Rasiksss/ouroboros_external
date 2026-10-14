"""Web search via Tavily.

Used by Finengine DEV when the user asks something the document alone
can't answer (macroeconomic context, peer comparisons, news after the
report). Tavily's free tier — 1000 searches/month, no card on file —
is enough for the pilot. Set FINENGINE_TAVILY_API_KEY in deploy/.env.

If the key is missing, `web_search` returns a friendly message instead
of crashing — Finengine DEV degrades to doc-only Q&A.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class WebSearchResult:
    title: str
    url: str
    content: str
    score: float = 0.0


_DISABLED_MESSAGE = (
    "Поиск в сети не настроен. Чтобы включить, заведите API-ключ Tavily "
    "(https://tavily.com — 1000 запросов/мес бесплатно) и пропишите "
    "FINENGINE_TAVILY_API_KEY в deploy/.env."
)


def is_enabled() -> bool:
    return bool(os.environ.get("FINENGINE_TAVILY_API_KEY", "").strip())


async def web_search(query: str, max_results: int = 5) -> list[WebSearchResult]:
    """Run a Tavily search and return up to `max_results` hits.

    Empty list when the API key is unset — caller checks `is_enabled()`
    first if it wants to short-circuit on UI before paying the round trip.
    """
    api_key = os.environ.get("FINENGINE_TAVILY_API_KEY", "").strip()
    if not api_key:
        return []
    try:
        from tavily import TavilyClient  # type: ignore[import-untyped]
    except ImportError:
        return []

    client = TavilyClient(api_key=api_key)
    # Tavily client is sync — wrap in to_thread so we don't block the loop.
    import asyncio
    raw = await asyncio.to_thread(
        client.search,
        query=query,
        search_depth="basic",
        max_results=max_results,
        include_answer=False,
    )
    out: list[WebSearchResult] = []
    for r in raw.get("results", []) or []:
        out.append(
            WebSearchResult(
                title=str(r.get("title") or "").strip(),
                url=str(r.get("url") or "").strip(),
                content=str(r.get("content") or "").strip()[:1500],
                score=float(r.get("score") or 0.0),
            )
        )
    return out


def format_results_for_prompt(results: list[WebSearchResult]) -> str:
    """Render search hits as a markdown bullet list ready to drop into a
    prompt. Each item: title (link) + content snippet."""
    if not results:
        return "(нет результатов)"
    lines: list[str] = []
    for i, r in enumerate(results, start=1):
        lines.append(f"{i}. **{r.title}** ([источник]({r.url}))")
        if r.content:
            snippet = r.content.replace("\n", " ").strip()
            if len(snippet) > 800:
                snippet = snippet[:800] + "…"
            lines.append(f"   {snippet}")
    return "\n".join(lines)


__all__ = ["WebSearchResult", "is_enabled", "web_search", "format_results_for_prompt"]
