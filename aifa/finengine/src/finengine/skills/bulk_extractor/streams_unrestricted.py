"""Извлечение без ограничения реестром: что найдётся в отчёте, то и берём.

Отдельный поток от полноконтекстного: там состав задан реестром ячейки, здесь
модель сама решает, какие строки в документе есть.
"""
from __future__ import annotations

from typing import AsyncGenerator, Optional
from finengine.llm import openrouter as llm
from .company_detection import _detect_company_from_text, _prefer_detected_company_name
from .period_selection import _select_run_periods
from .value_parsing import _normalise_money_value


async def _extract_stream_unrestricted(
    doc_id: str,
    company_type: str,
    index=None,
    limit: Optional[int] = None,
    model_id: Optional[str] = None,
) -> AsyncGenerator[dict, None]:
    """Finengine DEV — free-form extraction without the metric registry.

    The LLM is asked to surface ALL financial figures it can find in the
    report (revenue, EBITDA, segment splits, leverage ratios, even unusual
    items). Output shape mirrors registry-based extracts so the metrics
    table renders the same way; the only difference is `enum` is the
    Russian name (slug) instead of an English MetricEnum.
    """
    import json as _json
    import re as _re

    if index is None:
        from finengine.ingestion.document_store import get_store

        index = get_store()

    doc = index.get_document(doc_id)
    if not doc:
        yield {"type": "error", "message": f"Document {doc_id} not found"}
        return

    # Распознавание могло ещё идти фоновой задачей загрузки — ждём текст, а не
    # отбиваем сразу: обычный отчёт разбирается 5-30 с.
    from finengine.store.blobs import document_text_when_ready  # noqa: PLC0415

    pdf_text = await document_text_when_ready(
        doc_id, timeout_s=900, poll_s=1.5, limit=200_000)
    if pdf_text is None:
        yield {
            "type": "error",
            "message": (
                "Документ ещё распознаётся (превышено окно ожидания 15 минут). "
                "Большие отчёты (100+ страниц) распознаются дольше — "
                "подождите 1-2 минуты и нажмите «Извлечь» снова. "
                "Повторно загружать документ не нужно."
            ),
        }
        return

    periods_detected, period_current, period_prev = _select_run_periods(pdf_text, doc.period)
    # Pick the best company name we can, then sanity-check against the
    # filename. The reconcile catches the subsidiary-vs-parent confusion
    # (e.g. ЧТПЗ 2024.pdf returning "ТМК" because the report repeats
    # "входит в Группу ТМК" on every page).
    detected_company = _detect_company_from_text(pdf_text)
    company_display = _prefer_detected_company_name(doc.company_name, detected_company, doc.filename)

    # Yield the start event before the LLM call so the UI can show progress.
    yield {
        "type": "start",
        "total_metrics": 0,         # unknown ahead of time
        "total_extractable": 0,
        "company_type": company_type,
        "company_name": company_display,
        "unit": "млн. руб.",
        "period_current": period_current,
        "period_prev": period_prev,
        "periods": periods_detected,
        "ocr_engine": getattr(doc, "ocr_engine", "") or "",
    }

    prompt = (
        f"# Финансовый отчёт компании {company_display or doc.filename}\n"
        f"Текущий период: {period_current}, предыдущий: {period_prev}.\n\n"
        "## Текст отчёта\n"
        f"{pdf_text}\n\n"
        "## Задача\n"
        "Извлеки ВСЕ финансовые показатели, которые ты видишь в отчёте. "
        "Не ограничивайся стандартным реестром — тяни любые цифры с финансовым "
        "смыслом: статьи баланса, P&L, cash flow, операционные KPI (объёмы "
        "производства, ARPU, MAU, etc.), сегментные разрезы, разовые "
        "корректировки, всё что встретится.\n\n"
        "## Правила\n"
        "1. Каждый показатель — со значением за текущий и предыдущий период.\n"
        "2. Числа без пробелов, как в отчёте. Скобки = отрицательное.\n"
        "3. Единица как в первоисточнике (млн руб., тыс. руб., %, шт., тонн, ...).\n"
        "4. Не выдумывай. Если значения нет — null.\n\n"
        "## Формат ответа (строго JSON, без markdown)\n"
        '{"metrics": [{"name": "Выручка", "current": 100200, "previous": 95300, "unit": "млн. руб."}, ...]}'
    )

    try:
        raw, _usage = await llm.complete_with_usage(
            prompt=prompt, system=None, model=model_id,
            response_format={"type": "json_object"}, max_output=6000,
        )
    except Exception as e:
        yield {"type": "error", "message": f"LLM error: {e}"}
        return

    # Parse JSON, tolerate ```json wrappers.
    s = (raw or "").strip()
    s = _re.sub(r"^```(?:json)?\s*", "", s)
    s = _re.sub(r"\s*```$", "", s)
    try:
        parsed = _json.loads(s)
    except Exception:
        m = _re.search(r"\{[\s\S]*\}", s)
        try:
            parsed = _json.loads(m.group(0)) if m else {}
        except Exception:
            parsed = {}

    metrics = parsed.get("metrics") if isinstance(parsed, dict) else None
    if not isinstance(metrics, list):
        yield {"type": "error", "message": "Модель не вернула список показателей."}
        return

    if limit is not None and limit > 0:
        metrics = metrics[:limit]

    def _coerce(v):
        if v is None or v == "" or (isinstance(v, str) and v.strip().lower() in ("null", "none", "—", "-")):
            return None
        if isinstance(v, (int, float)):
            return float(v)
        try:
            return float(str(v).replace(" ", "").replace("\u00a0", "").replace(",", "."))
        except (TypeError, ValueError):
            return None

    seen_keys: set[str] = set()
    sent = 0
    for m in metrics:
        if not isinstance(m, dict):
            continue
        name = (m.get("name") or m.get("name_ru") or "").strip()
        if not name:
            continue
        # Slug as enum so the frontend has a stable React key.
        slug = _re.sub(r"\s+", "_", name.lower())[:80]
        slug = _re.sub(r"[^\wа-яё]+", "", slug, flags=_re.IGNORECASE)
        if not slug or slug in seen_keys:
            continue
        seen_keys.add(slug)
        v_cur = _coerce(m.get("current"))
        v_prv = _coerce(m.get("previous"))
        unit = (m.get("unit") or "").strip() or "млн. руб."
        # Same scale normalisation as the registry path.
        v_cur, norm_unit = _normalise_money_value(v_cur, unit)
        v_prv, _ = _normalise_money_value(v_prv, unit)
        period_values = [
            {"period": period_current, "value": v_cur, "unit": norm_unit or unit, "page": None, "source_text": ""},
            {"period": period_prev, "value": v_prv, "unit": norm_unit or unit, "page": None, "source_text": ""},
        ]
        yield {
            "type": "metric",
            "enum": slug.upper(),
            "name_ru": name,
            "value_current": v_cur,
            "value_prev": v_prv,
            "period_values": period_values,
            "unit": norm_unit or unit,
            "found": v_cur is not None,
            "is_calculated": False,
            "formula": "",
        }
        sent += 1

    yield {
        "type": "done",
        "period_current": period_current,
        "period_prev": period_prev,
        "periods": periods_detected,
        "unit": "млн. руб.",
        "company_type": company_type,
        "company_name": company_display,
        "_total_returned": sent,
    }
