"""Factor Analysis (Root Cause Analysis) skill."""
from __future__ import annotations
from typing import Any

from finengine.skills.extractor import get_index


def factor_analyze(query_input: dict[str, Any]) -> dict[str, Any]:
    """
    Decompose a metric to find root causes of changes.
    Input: {metric: str, period_current: str, period_prev: str, doc_ids: list[str]}
    Output: {decomposition: list[dict], key_drivers: list[str]}
    """
    metric = query_input.get("metric", "")
    period_current = query_input.get("period_current", "")
    period_prev = query_input.get("period_prev", "")
    doc_ids = query_input.get("doc_ids")

    index = get_index()

    # Search for sub-components of the metric
    search_queries = [
        f"{metric} структура составляющие компоненты {period_current}",
        f"{metric} breakdown components {period_current}",
        f"динамика {metric} изменение {period_prev} {period_current}",
        f"{metric} factors drivers change",
    ]

    all_items = []
    seen: set[str] = set()
    for q in search_queries:
        items = index.search(q, top_k=4, doc_ids=doc_ids)
        for item in items:
            key = str(item.content)[:80]
            if key not in seen:
                seen.add(key)
                all_items.append(item)

    return {
        "status": "success",
        "metric": metric,
        "periods": {"current": period_current, "previous": period_prev},
        "source_data": [
            {
                "content": str(item.content),
                "source": item.source_ref,
                "type": item.chunk_type,
                "score": item.score,
            }
            for item in all_items[:10]
        ],
        "instruction": (
            "На основе найденных данных проведи факторный анализ: "
            "декомпозируй изменение показателя на составляющие, "
            "определи ключевые драйверы роста/снижения и их вклад в процентах."
        ),
    }
