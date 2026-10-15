"""Smart Extractor skill — hybrid search over text and tables."""
from __future__ import annotations
import json
from typing import Any

from finengine.ingestion.indexer import VectorIndex
from finengine.models.query import ExtractionQuery, ExtractionResult
from finengine.models.document import ExtractedItem

_index: VectorIndex | None = None


def get_index() -> VectorIndex:
    global _index
    if _index is None:
        _index = VectorIndex()
    return _index


def extract(query_input: dict[str, Any]) -> dict[str, Any]:
    """
    Tool handler for the Smart Extractor skill.
    Input: {queries: list[str], document_filters: dict, top_k: int}
    Output: {status: str, items: list[dict]}
    """
    try:
        eq = ExtractionQuery(**query_input)
        index = get_index()

        doc_ids = eq.document_filters.get("doc_ids")
        doc_type_filter = eq.document_filters.get("doc_type")
        if isinstance(doc_type_filter, str):
            doc_type_filter = [doc_type_filter]

        all_items: list[ExtractedItem] = []
        seen_content: set[str] = set()

        for query_text in eq.queries:
            results = index.search(
                query=query_text,
                top_k=eq.top_k,
                doc_ids=doc_ids,
                doc_type_filter=doc_type_filter,
            )
            for item in results:
                content_key = str(item.content)[:100]
                if content_key not in seen_content:
                    seen_content.add(content_key)
                    all_items.append(item)

        if not all_items:
            return ExtractionResult(
                status="not_found",
                items=[],
                error_message="Релевантные данные в документах не найдены",
            ).model_dump()

        # Sort by score, take top_k
        all_items.sort(key=lambda x: x.score, reverse=True)
        all_items = all_items[:eq.top_k * 2]

        return ExtractionResult(
            status="success",
            items=all_items,
        ).model_dump()

    except Exception as e:
        return ExtractionResult(
            status="error",
            items=[],
            error_message=str(e),
        ).model_dump()
