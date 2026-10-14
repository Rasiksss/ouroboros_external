"""AIFA baseline pipeline: IDP-OCR → reranker → AIFA prompt → LLM.

Same per-metric flow as `aifa.extractor`, but:
- PDF is sent to the IDP-OCR cloud service (`idp_ocr_client`) instead of
  parsed locally with pdfplumber. Tables come pre-extracted as markdown.
- The LLM call uses the verbatim AIFA prompt template (`prompts.py`)
  including the JSON schema, the step-by-step reasoning, and the
  `EXTRA_GUIDANCE` block.

Result-shape stays the same as the rest of our extractors so the UI is
agnostic to which arch ran.
"""
from __future__ import annotations

import asyncio
import json as _json
import re as _re
from pathlib import Path
from typing import AsyncGenerator, Optional

from finengine.config import settings
from finengine.llm import openrouter as llm
from finengine.skills.aifa.classifier import AIFAUnsupportedError, classify_supported, detect_company_kind
from finengine.skills.aifa.ngrams import NGrams, PREPROCESSOR
from finengine.skills.aifa.scope import filter_to_trained
from finengine.skills.aifa.scorer import aggregate_score, score_table
from finengine.skills.aifa_baseline.reranker import rerank_tables, get_load_error
from finengine.skills.aifa_baseline.idp_ocr_client import call_idp_ocr
from finengine.skills.aifa_baseline.prompts import build_tables_extraction_prompt
from finengine.skills.metric_registry import (
    CALCULATED_BANK,
    CALCULATED_NON_FINANCIAL,
    EXTRACTABLE_BANK,
    EXTRACTABLE_NON_FINANCIAL,
)


def _metric_variations(metric: dict) -> list[str]:
    out: list[str] = [metric["name_ru"]]
    if metric.get("name_en"):
        out.append(metric["name_en"])
    for v in (metric.get("aliases_ru") or []) + (metric.get("aliases_en") or []):
        if v and v != "Нет":
            out.append(v)
    seen: set[str] = set()
    unique: list[str] = []
    for v in out:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    return unique


def _ngrams_for_metric(metric: dict) -> NGrams:
    variations = _metric_variations(metric)
    parts = PREPROCESSOR.extract_ngrams_batch(variations)
    unis: set[str] = set()
    bis: set[str] = set()
    tris: set[str] = set()
    for p in parts:
        unis |= p.unigrams
        bis |= p.bigrams
        tris |= p.trigrams
    return NGrams(frozenset(unis), frozenset(bis), frozenset(tris))


def _user_facing_llm_error(raw: str) -> str:
    """Translate provider-side LLM errors into a short, user-actionable
    message. Shown as the error event when fail-fast trips."""
    low = (raw or "").lower()
    if "402" in low or "insufficient credits" in low:
        return (
            "Извлечение прервано: на аккаунте OpenRouter закончились кредиты. "
            "Пополните баланс и попробуйте снова."
        )
    if "401" in low or "unauthorized" in low or "invalid api key" in low:
        return "Извлечение прервано: ключ OpenRouter недействителен."
    if "429" in low or "rate limit" in low:
        return (
            "Извлечение прервано: провайдер ограничил скорость запросов. "
            "Подождите минуту и попробуйте снова."
        )
    if "timeout" in low or "timed out" in low:
        return "Извлечение прервано: модель не отвечает (таймаут)."
    return f"Извлечение прервано: модель вернула ошибку. {raw[:200]}"


def _coerce_num(v) -> Optional[float]:
    if v is None or (isinstance(v, str) and v.strip().lower() in ("null", "none", "")):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace("\u00a0", "").replace(" ", "").replace(",", ".")
        try:
            return float(s)
        except Exception:
            return None
    return None


def _parse_aifa_response(raw: str, metric_name: str, period_current: str, period_prev: str) -> tuple[Optional[float], Optional[float], str]:
    """Parse the upstream AIFA JSON envelope:

        { "periods": [...], "parameters": [
            { "period": "2024", "extracted": [
                { "type": "<metric_name>", "value": "...", "unit": "..." }, ...
            ]}, ...
        ]}

    We pull values for the current and previous periods of `metric_name`.
    """
    if not raw:
        return None, None, "млн. руб."
    s = raw.strip()
    s2 = _re.sub(r"^```(?:json)?\s*", "", s)
    s2 = _re.sub(r"\s*```$", "", s2)
    obj: dict = {}
    for cand in (s, s2):
        try:
            o = _json.loads(cand)
            if isinstance(o, dict):
                obj = o
                break
        except Exception:
            continue
    if not obj:
        m = _re.search(r"\{.*\}", s, _re.DOTALL)
        if m:
            try:
                obj = _json.loads(m.group(0))
            except Exception:
                obj = {}
    if not isinstance(obj, dict):
        return None, None, "млн. руб."

    cur: Optional[float] = None
    prv: Optional[float] = None
    unit = "млн. руб."
    for block in obj.get("parameters", []) or []:
        if not isinstance(block, dict):
            continue
        period = str(block.get("period") or "").strip()
        target_cur = period == period_current or (period_current.isdigit() and period.endswith(period_current))
        target_prv = period == period_prev or (period_prev.isdigit() and period.endswith(period_prev))
        if not (target_cur or target_prv):
            continue
        for row in block.get("extracted", []) or []:
            if not isinstance(row, dict):
                continue
            row_type = str(row.get("type") or "").strip().lower()
            if metric_name.strip().lower() not in row_type and row_type not in metric_name.strip().lower():
                continue
            val = _coerce_num(row.get("value"))
            u = str(row.get("unit") or "").strip()
            if u:
                unit = u
            if target_cur and cur is None:
                cur = val
            elif target_prv and prv is None:
                prv = val
    return cur, prv, unit


async def _extract_one_metric(
    metric: dict,
    tables: list[dict],
    period_current: str,
    period_prev: str,
    model_id: str | None,
    top_n: int = 3,
    diag: dict | None = None,
    company_type: str = "non_financial",
) -> tuple[Optional[float], Optional[float], str, Optional[int]]:
    import time as _t
    score_t0 = _t.time()
    # Primary path: AutoML CatBoost reranker (Расула's evolute_features_automl
    # for non_financial, Basic_Banks for banks). Fallback to heuristic if the
    # binary is missing or load fails.
    top: list[tuple[float, dict]] = []
    used_reranker = False
    rerank_result = rerank_tables(tables, metric, top_n=top_n, company_type=company_type)
    if rerank_result is not None:
        top = rerank_result
        used_reranker = True
    else:
        metric_ngrams = _ngrams_for_metric(metric)
        variations = _metric_variations(metric)
        scored: list[tuple[float, dict]] = []
        for t in tables:
            s = score_table(t["text"], variations, metric_ngrams)
            scored.append((aggregate_score(s), t))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:top_n]
    score_dt = _t.time() - score_t0
    if diag is not None:
        diag["scoring_total_s"] = round(diag.get("scoring_total_s", 0.0) + score_dt, 2)
        if used_reranker:
            diag["reranker"] = "catboost_automl"
        else:
            diag.setdefault("reranker", "heuristic")
            err = get_load_error()
            if err and not diag.get("reranker_error"):
                diag["reranker_error"] = err
    # CatBoost predict_proba can produce arbitrarily small but >0 probabilities
    # for irrelevant tables — keep all top_n unconditionally when the reranker
    # is in charge (it's been calibrated). For the heuristic fallback keep the
    # >0 filter that was there originally so we don't ship zero-signal tables.
    if used_reranker:
        candidates = [t["text"] for _, t in top]
    else:
        candidates = [t["text"] for sc, t in top if sc > 0]
    if not candidates:
        if diag is not None and len(diag.get("samples", [])) < 5:
            diag.setdefault("samples", []).append({
                "enum": metric["enum"],
                "top_scores": [round(sc, 2) for sc, _ in top],
                "scoring_s": round(score_dt, 3),
                "reason": "no_candidates",
            })
        return None, None, "млн. руб.", None
    prompt = build_tables_extraction_prompt(metric, candidates)
    llm_t0 = _t.time()
    try:
        raw, usage = await llm.complete_with_usage(
            prompt=prompt,
            system=None,
            model=model_id,
            response_format={"type": "json_object"},
            max_output=2500,
        )
        if diag is not None and usage:
            usage_sum = diag.setdefault("usage_sum", {"in": 0, "out": 0, "cached": 0, "calls": 0})
            usage_sum["in"] += usage.get("in", 0)
            usage_sum["out"] += usage.get("out", 0)
            usage_sum["cached"] += usage.get("cached", 0)
            usage_sum["calls"] += 1
    except Exception as e:
        llm_dt = _t.time() - llm_t0
        if diag is not None:
            diag["llm_total_s"] = round(diag.get("llm_total_s", 0.0) + llm_dt, 2)
        if diag is not None and len(diag.get("samples", [])) < 5:
            diag.setdefault("samples", []).append({
                "enum": metric["enum"],
                "reason": "llm_error",
                "error": str(e)[:200],
                "llm_s": round(llm_dt, 2),
            })
        return None, None, "млн. руб.", None
    llm_dt = _t.time() - llm_t0
    if diag is not None:
        diag["llm_total_s"] = round(diag.get("llm_total_s", 0.0) + llm_dt, 2)
    cur, prv, unit = _parse_aifa_response(raw, metric["name_ru"], period_current, period_prev)
    # Source page = page of the top-ranked table that actually fed the LLM.
    # When the model returns a value we can claim that page as the source —
    # for the Источник column on the UI. Tables without `page` (rare) or
    # cases where the model returned None get None.
    source_page: Optional[int] = None
    if cur is not None or prv is not None:
        for _, t in top:
            p = t.get("page")
            if isinstance(p, int) and p > 0:
                source_page = p
                break
    if diag is not None and len(diag.get("samples", [])) < 5:
        diag.setdefault("samples", []).append({
            "enum": metric["enum"],
            "top_scores": [round(sc, 2) for sc, _ in top],
            "scoring_s": round(score_dt, 3),
            "llm_s": round(llm_dt, 2),
            "raw_excerpt": (raw or "")[:600],
            "parsed": {"current": cur, "previous": prv, "unit": unit},
            "page": source_page,
        })
    return cur, prv, unit, source_page


async def extract_stream_aifa_baseline(
    doc_id: str,
    company_type: str,
    index=None,
    limit: Optional[int] = None,
    model_id: Optional[str] = None,
    parallel: int = 16,
) -> AsyncGenerator[dict, None]:
    """Per-metric AIFA-baseline extraction.

    Yields the same event shape every other extractor uses so the UI is
    agnostic to which arch is running.
    """
    if index is None:
        from finengine.ingestion.indexer import VectorIndex
        index = VectorIndex()

    doc = index.get_document(doc_id)
    if not doc:
        yield {"type": "error", "message": f"Document {doc_id} not found"}
        return
    pdf_path = Path(doc.file_path) if doc.file_path else None
    if not pdf_path or not pdf_path.exists():
        yield {
            "type": "error",
            "code": "pdf_not_found",
            "message": "PDF файл не найден на сервере.",
        }
        return

    # OCR the PDF through the IDP-OCR cloud service. Cache key is `v2` to
    # invalidate old whole-PDF responses captured before the switch to the
    # per-page PNG pipeline.
    import time as _t
    _ocr_t0 = _t.time()
    raw_save_path = settings.docs_dir / f"{doc_id}.idpocr.v2.json"
    try:
        ocr = await call_idp_ocr(pdf_path, save_raw_to=raw_save_path)
    except Exception as e:
        yield {
            "type": "error",
            "code": "ocr_unavailable",
            "params": {"error": str(e)},
            "message": f"IDP-OCR недоступен: {e}. Попробуйте режим Finengine.",
        }
        return
    ocr_elapsed = _t.time() - _ocr_t0

    # Same scope guard as AIFA DEV.
    try:
        classify_supported(ocr.full_text)
    except AIFAUnsupportedError as e:
        yield {"type": "error", "code": "aifa_unsupported", "message": e.reason}
        return

    if not ocr.full_tables:
        yield {
            "type": "error",
            "code": "ocr_no_tables",
            "message": "OCR не нашёл таблиц в документе. Попробуйте режим Finengine.",
        }
        return

    # Period + company name detection (reuse Finengine helpers — they work on
    # any text input).
    from finengine.skills.bulk_extractor import (
        _detect_company_from_text,
        _detect_year_from_text,
        _reconcile_company_name,
    )
    period_current = doc.period or _detect_year_from_text(ocr.full_text) or "Текущий"
    period_prev = (
        str(int(period_current) - 1) if period_current.isdigit() else "Предыдущий"
    )
    company_display = doc.company_name or _detect_company_from_text(ocr.full_text) or ""
    company_display = _reconcile_company_name(company_display, doc.filename)

    # Auto-detect bank vs non_financial from the OCR text when the upload
    # didn't pin company_type — the user shouldn't have to mark "это банк"
    # manually. Only respects an explicit "bank" / "non_financial" override.
    if company_type not in ("bank", "non_financial"):
        company_type = detect_company_kind(ocr.full_text)

    # Pick the metric registry by document kind. The bank reranker
    # (`Basic_Banks.cbm`) was trained on the full 77-metric bank set, so we
    # pass them all through — no scope filter on banks. NF reranker is still
    # restricted to `TRAINED_NF_55` until Расул retrains.
    if company_type == "bank":
        extractable = list(EXTRACTABLE_BANK)
        calculated_defs = CALCULATED_BANK
        emit_company_type = "bank"
    else:
        extractable = filter_to_trained(EXTRACTABLE_NON_FINANCIAL)
        calculated_defs = CALCULATED_NON_FINANCIAL
        emit_company_type = "non_financial"
    if limit is not None and limit > 0:
        extractable = extractable[:limit]
        calculated_defs = []

    yield {
        "type": "start",
        "total_metrics": len(extractable) + len(calculated_defs),
        "total_extractable": len(extractable),
        "company_type": emit_company_type,
        "company_name": company_display,
        "unit": "млн. руб.",
        "period_current": period_current,
        "period_prev": period_prev,
        # Run-level diagnostics surfaced through the run logger.
        "_diag_tables_total": len(ocr.full_tables),
    }

    sem = asyncio.Semaphore(max(1, parallel))
    extracted_current: dict[str, Optional[float]] = {}
    extracted_prev: dict[str, Optional[float]] = {}
    event_q: asyncio.Queue = asyncio.Queue()
    # Fail-fast on a wedged LLM (rate limits, billing, downstream outage):
    # if we accumulate ≥3 errors in a row with the same prefix, abort the
    # whole extract and surface that error to the UI instead of yielding
    # 100 nulls and a misleading "done".
    fail_fast = {"errors_in_row": 0, "last_error": None, "tripped": False}
    # Diagnostic accumulator — stage timings + samples of first few metric
    # attempts so we can see in extract_runs.jsonl whether failures are
    # no-candidate, LLM error, or parser miss.
    diag: dict = {
        "tables_total": len(ocr.full_tables),
        "ocr_s": round(ocr_elapsed, 1),
        "ocr": getattr(ocr, "diag", {}),
        "scoring_total_s": 0.0,
        "llm_total_s": 0.0,
        "calc_s": 0.0,
        "parallel": parallel,
        "samples": [],
    }
    metric_phase_t0 = _t.time()

    async def _run_metric(metric: dict) -> None:
        async with sem:
            if fail_fast["tripped"]:
                return
            await event_q.put({
                "type": "progress",
                "enum": metric["enum"],
                "name_ru": metric["name_ru"],
            })
            v_cur, v_prv, unit, page = await _extract_one_metric(
                metric, ocr.full_tables, period_current, period_prev, model_id,
                diag=diag, company_type=emit_company_type,
            )
            # Detect repeated identical LLM errors and bail. The latest sample
            # in `diag` carries the error message when the call failed.
            if v_cur is None and v_prv is None:
                samples = diag.get("samples") or []
                last = samples[-1] if samples else None
                if last and last.get("reason") == "llm_error":
                    err = last.get("error") or ""
                    if err == fail_fast["last_error"]:
                        fail_fast["errors_in_row"] += 1
                    else:
                        fail_fast["errors_in_row"] = 1
                        fail_fast["last_error"] = err
                    if fail_fast["errors_in_row"] >= 3:
                        fail_fast["tripped"] = True
                        await event_q.put({
                            "type": "error",
                            "message": _user_facing_llm_error(err),
                        })
                        return
            extracted_current[metric["enum"]] = v_cur
            extracted_prev[metric["enum"]] = v_prv
            await event_q.put({
                "type": "metric",
                "enum": metric["enum"],
                "name_ru": metric["name_ru"],
                "value_current": v_cur,
                "value_prev": v_prv,
                "unit": unit,
                "found": v_cur is not None,
                "is_calculated": False,
                "formula": "",
                "page": page,
            })

    tasks = [asyncio.create_task(_run_metric(m)) for m in extractable]

    async def _fan_in() -> None:
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await event_q.put(None)

    fan_in = asyncio.create_task(_fan_in())
    try:
        while True:
            ev = await event_q.get()
            if ev is None:
                break
            yield ev
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        fan_in.cancel()

    from finengine.skills.bulk_extractor import _calculate_metrics_pair
    from finengine.skills.pricing import aggregate_usage_to_diag
    diag["metric_phase_s"] = round(_t.time() - metric_phase_t0, 1)
    calc_t0 = _t.time()
    calc_current, calc_prev = _calculate_metrics_pair(
        calculated_defs,
        extracted_current,
        extracted_prev,
    )
    diag["calc_s"] = round(_t.time() - calc_t0, 2)
    aggregate_usage_to_diag(diag.get("usage_sum") or {}, diag, model_id)
    for enum, name_ru, formula, _deps in calculated_defs:
        v_cur = calc_current.get(enum)
        v_prv = calc_prev.get(enum)
        is_pct = "* 100" in formula or "× 100" in formula
        yield {
            "type": "metric",
            "enum": enum,
            "name_ru": name_ru,
            "value_current": v_cur,
            "value_prev": v_prv,
            "unit": "%" if is_pct else "млн. руб.",
            "found": v_cur is not None,
            "is_calculated": True,
            "formula": formula,
        }

    yield {
        "type": "done",
        "period_current": period_current,
        "period_prev": period_prev,
        "unit": "млн. руб.",
        "company_type": emit_company_type,
        "company_name": company_display,
        "_diag": diag,
    }
