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
    # Дата публикации приходит ТОЛЬКО в новостном режиме (`topic="news"`) — в
    # обычном веб-поиске её нет вовсе. Заведена под раздел «Контекст извне»
    # (#159): там находка без даты выбывает (`analysis/external.py:_accept`),
    # поэтому пустая строка здесь — не мелочь, а причина не показать факт.
    published_date: str = ""


_DISABLED_MESSAGE = (
    "Поиск в сети не настроен. Чтобы включить, заведите API-ключ Tavily "
    "(https://tavily.com — 1000 запросов/мес бесплатно) и пропишите "
    "FINENGINE_TAVILY_API_KEY в deploy/.env."
)


_API_URL = "https://api.tavily.com/search"
_TIMEOUT_S = 25.0


def foreign_proxy() -> str | None:
    """Прокси для зарубежных API — БЕЗ НЕГО ПОИСК НЕ РАБОТАЕТ (17.08.2026).

    ЗАМЕР НА БОЕВОМ ПРОДЕ. С этого VPS исходящий трафик фильтруется по хостам:
        api.tavily.com  → 403 (HTML nginx, не ответ Tavily)
        openrouter.ai   → 403
        news.google.com → 302   www.cbr.ru → 200
    То есть отбивал не Tavily и не ключ — резала сеть. Через тот же прокси,
    которым уже ходит макро-блок, api.tavily.com отвечает 200 с результатами.

    ⚠️ ИЗ-ЗА ЭТОГО Я СНАЧАЛА ПОСТАВИЛ НЕВЕРНЫЙ ДИАГНОЗ «ключа нет»: проверял
    переменную под чужим именем (`TAVILY_API_KEY` вместо
    `FINENGINE_TAVILY_API_KEY`). Ключ был на месте всё время.

    Переменная переиспользуется, а не заводится новая: она уже прописана на
    обоих стендах, а новую пришлось бы раскладывать руками — и до первой
    выкатки поиск снова молчал бы.
    """
    return os.environ.get("FINENGINE_MACRO_FOREIGN_PROXY", "").strip() or None


def is_enabled() -> bool:
    return bool(os.environ.get("FINENGINE_TAVILY_API_KEY", "").strip())


async def web_search(query: str, max_results: int = 5, *,
                     topic: str = "general", days: int | None = None) -> list[WebSearchResult]:
    """Run a Tavily search and return up to `max_results` hits.

    Empty list when the API key is unset — caller checks `is_enabled()`
    first if it wants to short-circuit on UI before paying the round trip.

    `topic="news"` (+ `days` — глубина окна) переводит поиск в новостной режим:
    только там выдача несёт дату публикации. Дефолты сохраняют прежнее
    поведение — старые вызывающие ничего не заметили.
    """
    api_key = os.environ.get("FINENGINE_TAVILY_API_KEY", "").strip()
    if not api_key:
        return []
    import httpx  # noqa: PLC0415 — тяжёлый модуль, нужен только здесь

    payload: dict = {
        "api_key": api_key,
        "query": query,
        "search_depth": "basic",
        "max_results": max_results,
        "include_answer": False,
        "topic": topic,
    }
    if days is not None:
        payload["days"] = days
    async with httpx.AsyncClient(timeout=_TIMEOUT_S, proxy=foreign_proxy()) as client:
        resp = await client.post(_API_URL, json=payload)
    resp.raise_for_status()
    raw = resp.json()
    out: list[WebSearchResult] = []
    for r in raw.get("results", []) or []:
        out.append(
            WebSearchResult(
                title=str(r.get("title") or "").strip(),
                url=str(r.get("url") or "").strip(),
                content=str(r.get("content") or "").strip()[:1500],
                score=float(r.get("score") or 0.0),
                published_date=str(r.get("published_date") or "").strip(),
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
