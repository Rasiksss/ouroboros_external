"""Синхронные обёртки над потоковым извлечением.

Для вызывающих, которым поток не нужен: прогоняют генератор до конца и
отдают собранный результат.
"""
from __future__ import annotations

import asyncio
# ⚠️ Импорт из КОНКРЕТНОГО модуля, а не из шима `.streams`. Разрез
# `streams_sync`/`streams_dev` прошёл РАНЬШЕ, чем `streams_full_context`,
# и помощник направил их на шим — тот теперь сам импортирует отсюда, и
# получался круг. Порядок разрезов имеет значение: модуль-потребитель
# режется ПОСЛЕ того, кого он зовёт.
from .streams_full_context import _extract_stream_full_context


async def _extract_async(doc_id: str, company_type: str, index=None, parallel: int = 8) -> dict:
    """Collect all streaming events into a single result dict."""
    if index is None:
        from finengine.ingestion.document_store import get_store

        index = get_store()

    doc = index.get_document(doc_id)
    if not doc:
        return {"error": f"Document {doc_id} not found in index"}

    metrics: list[dict] = []
    meta: dict = {}

    async for event in _extract_stream_full_context(doc_id, company_type, index=index):
        etype = event.get("type")
        if etype == "error":
            return {"error": event.get("message", "unknown error")}
        elif etype == "metric":
            metrics.append({
                "enum": event["enum"],
                "name_ru": event["name_ru"],
                "value_current": event["value_current"],
                "value_prev": event["value_prev"],
                "period_values": event.get("period_values") or [],
                "unit": event["unit"],
                "found": event["found"],
                "is_calculated": event["is_calculated"],
                "formula": event.get("formula", ""),
                # Четвёртое место, где обрезался бы контракт строки (Б5):
                # эта сборка тоже перечисляет ключи поимённо, а не копирует
                # событие, и без строки происхождение расчёта теряется здесь.
                "inputs": event.get("inputs") or [],
                "segment": event.get("segment", ""),
                "dimension": event.get("dimension", ""),
            })
        elif etype == "done":
            meta = event

    return {
        "doc_id": doc_id,
        "company_name": doc.company_name,
        "period": doc.period,
        "company_type": meta.get("company_type", ""),
        "period_current": meta.get("period_current", "Текущий"),
        "period_prev": meta.get("period_prev", "Предыдущий"),
        "unit": meta.get("unit", ""),
        "metrics": metrics,
    }

def extract_all_sync(doc_id: str, company_type: str = "unknown") -> dict:
    """Synchronous wrapper (for scripts / tests)."""
    return asyncio.run(_extract_async(doc_id, company_type))
