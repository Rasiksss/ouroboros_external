"""Cross-Document Semantic Synthesis skill."""
from __future__ import annotations
from typing import Any

from finengine.skills.extractor import get_index
from finengine.models.document import ExtractedItem


def synthesize(query_input: dict[str, Any]) -> dict[str, Any]:
    """
    Find quantitative facts AND qualitative commentary across documents.
    Input: {metric: str, period: str, doc_ids: list[str]}
    Output: {facts: list[dict], commentary: list[dict]}
    """
    metric = query_input.get("metric", "")
    period = query_input.get("period", "")
    doc_ids = query_input.get("doc_ids")

    index = get_index()

    # Search for numerical facts (tables)
    fact_query = f"{metric} {period} показатель значение"
    facts = index.search(fact_query, top_k=5, doc_ids=doc_ids)
    table_facts = [f for f in facts if f.chunk_type == "table"]
    if not table_facts:
        table_facts = facts[:3]

    # Search for management commentary (text)
    commentary_queries = [
        f"{metric} комментарий менеджмент {period}",
        f"{metric} объяснение причина динамика",
        f"{metric} management commentary {period}",
    ]
    commentary_items: list[ExtractedItem] = []
    seen: set[str] = set()
    for cq in commentary_queries:
        items = index.search(cq, top_k=3, doc_ids=doc_ids)
        for item in items:
            key = str(item.content)[:80]
            if key not in seen:
                seen.add(key)
                commentary_items.append(item)

    return {
        "status": "success",
        "metric": metric,
        "period": period,
        "facts": [
            {
                "content": str(item.content),
                "source": item.source_ref,
                "type": item.chunk_type,
                "score": item.score,
            }
            for item in table_facts
        ],
        "commentary": [
            {
                "content": str(item.content),
                "source": item.source_ref,
                "speaker": item.metadata.get("speaker", ""),
                "score": item.score,
            }
            for item in commentary_items[:5]
        ],
    }
