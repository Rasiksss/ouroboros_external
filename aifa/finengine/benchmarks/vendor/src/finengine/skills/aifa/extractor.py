"""AIFA Dev — per-metric extraction (experimental sandbox for prompts/chains).

Pipeline:
  1. IDP-OCR cloud service (same as AIFA baseline; cache shared via
     `{doc_id}.idpocr.v2.json`).
  2. classifier.classify_supported(ocr.full_text) — bail out if out of scope.
  3. For each metric:
     a) score every IDP-OCR table for relevance (heuristic n-gram + fuzzy)
     b) keep top-3 highest-scoring tables
     c) ask LLM to read those tables and pick the value (current/previous)
  4. Calculated metrics computed locally afterwards (same as Finengine).

Output event shape matches `_extract_stream_full_context` so the UI is
agnostic to which arch produced the run.
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
from finengine.skills.aifa_baseline.idp_ocr_client import call_idp_ocr
from finengine.skills.metric_registry import (
    CALCULATED_BANK,
    CALCULATED_NON_FINANCIAL,
    EXTRACTABLE_BANK,
    EXTRACTABLE_NON_FINANCIAL,
)


def _metric_variations(metric: dict) -> list[str]:
    """Concatenate every textual handle the metric can appear under in the
    report — name + aliases (RU + EN). Used for fuzzy match + n-gram seed."""
    out: list[str] = []
    for k in ("name_ru", "name_en"):
        v = metric.get(k)
        if v:
            out.append(v)
    for v in (metric.get("aliases_ru") or []) + (metric.get("aliases_en") or []):
        if v and v != "Нет":
            out.append(v)
    # Dedupe, preserve order.
    seen: set[str] = set()
    unique: list[str] = []
    for v in out:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    return unique


def _ngrams_for_metric(metric: dict) -> NGrams:
    """Union n-grams over every variation of the metric's name."""
    variations = _metric_variations(metric)
    if not variations:
        return PREPROCESSOR.extract_ngrams("")
    parts = PREPROCESSOR.extract_ngrams_batch(variations)
    unis: set[str] = set()
    bis: set[str] = set()
    tris: set[str] = set()
    for p in parts:
        unis |= p.unigrams
        bis |= p.bigrams
        tris |= p.trigrams
    return NGrams(frozenset(unis), frozenset(bis), frozenset(tris))


def _build_extract_prompt(
    metric: dict,
    candidate_tables: list[dict],
    period_current: str,
    period_prev: str,
) -> tuple[str, str]:
    """Render the LLM prompt that picks a value from the top-3 tables."""
    tables_str = "\n\n".join(
        f"### Таблица {i + 1} (стр. {t.get('page', '?')})\n{t['text']}"
        for i, t in enumerate(candidate_tables)
    )

    aliases = _metric_variations(metric)
    alias_str = "; ".join(aliases[:6]) if aliases else metric["name_ru"]

    system = (
        "Ты опытный финансовый аналитик. Извлекаешь значение метрики из "
        "одной из приведённых таблиц финансовой отчётности.\n\n"
        "## Правила\n"
        "1. **Лучше null, чем неверное число.** Если ни одна таблица не содержит "
        "однозначного значения метрики — верни null.\n"
        "2. Сначала проверь каждую таблицу: совпадают ли её заголовки строк/столбцов "
        "с описанием метрики. Если нет — игнорируй таблицу.\n"
        "3. Числа без пробелов, без валюты, как в первоисточнике. Скобки — отрицательное.\n"
        "4. По умолчанию единица — млн. руб. Если в таблице тыс. руб. — переведи (÷1000); "
        "если млрд — умножь (×1000). Поле unit заполни из таблицы.\n"
        "5. Возвращай **только** JSON, без markdown и комментариев."
    )
    user = (
        f"# Информация о показателе\n\n"
        f"**Название:** {metric['name_ru']}"
        + (f" (англ.: {metric.get('name_en')})" if metric.get("name_en") else "")
        + "\n"
        + f"**Также может называться:** {alias_str}\n"
        + (f"**Описание:** {metric.get('description', '')}\n" if metric.get("description") else "")
        + (f"**Где искать:** {metric.get('hint', '')}\n" if metric.get("hint") else "")
        + f"\nТекущий период = **{period_current}**, предыдущий период = **{period_prev}**.\n\n"
        + f"# Кандидаты таблиц (топ-3 по релевантности)\n\n{tables_str}\n\n"
        + "# Алгоритм\n"
        "1. Прочитай каждую таблицу. Найди ту, где заголовок строки соответствует "
        "названию метрики или одному из синонимов.\n"
        "2. Если такой строки нет ни в одной — `current` и `previous` = null.\n"
        "3. Если строка найдена — возьми числа за текущий и предыдущий периоды.\n\n"
        "# Формат ответа\n\n"
        '{"current": <число или null>, "previous": <число или null>, "unit": "<единица>"}'
    )
    return system, user


def _coerce_num(v) -> Optional[float]:
    if v is None:
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


def _parse_value_json(text: str) -> dict:
    """Robust parse of `{"current": …, "previous": …, "unit": …}`."""
    if not text:
        return {}
    s = text.strip()
    s2 = _re.sub(r"^```(?:json)?\s*", "", s)
    s2 = _re.sub(r"\s*```$", "", s2)
    for cand in (s, s2):
        try:
            obj = _json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    m = _re.search(r"\{[^{}]*\}", s, _re.DOTALL)
    if m:
        try:
            obj = _json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return {}


async def _extract_one_metric(
    metric: dict,
    tables: list[dict],
    period_current: str,
    period_prev: str,
    model_id: str | None,
    doc_id: str,
    top_n: int = 3,
    diag: dict | None = None,
) -> tuple[Optional[float], Optional[float], str, Optional[int]]:
    """Score tables for this metric, keep top-N, ask the LLM, parse result.

    Returns (current, previous, unit, source_page) — source_page is the
    page of the highest-ranked candidate table, surfaced into the metric
    event so the UI can show it in the «Источник» column.
    """
    import time as _t
    metric_ngrams = _ngrams_for_metric(metric)
    variations = _metric_variations(metric) or [metric["name_ru"]]
    score_t0 = _t.time()
    scored: list[tuple[float, dict]] = []
    for t in tables:
        s = score_table(t["text"], variations, metric_ngrams)
        scored.append((aggregate_score(s), t))
    scored.sort(key=lambda x: x[0], reverse=True)
    score_dt = _t.time() - score_t0
    if diag is not None:
        diag["scoring_total_s"] = round(diag.get("scoring_total_s", 0.0) + score_dt, 2)
    top = scored[:top_n]
    candidates = [t for sc, t in top if sc > 0]
    if not candidates:
        if diag is not None and len(diag.get("samples", [])) < 5:
            diag.setdefault("samples", []).append({
                "enum": metric["enum"],
                "top_scores": [round(sc, 2) for sc, _ in top],
                "scoring_s": round(score_dt, 3),
                "reason": "no_candidates",
            })
        return None, None, "млн. руб.", None

    system, user = _build_extract_prompt(metric, candidates, period_current, period_prev)
    llm_t0 = _t.time()
    # Determinism knobs for per-metric extract:
    #  - temperature=0: no creative sampling, model picks the highest-prob token.
    #  - seed: stable hash of (doc_id, metric_enum). Same input → same output
    #    on providers that honour the OpenAI seed parameter (Qwen via DashScope
    #    does). Combined with the env-pinned provider this drove the AIFA
    #    duplicate-run divergence from 18 / 103 metrics down to <1 in our pre-
    #    deploy probe.
    seed_int = abs(hash((doc_id, metric["enum"]))) % (2**31 - 1)
    try:
        raw, usage = await llm.complete_with_usage(
            prompt=user,
            system=system,
            model=model_id,
            response_format={"type": "json_object"},
            max_output=1500,
            seed=seed_int,
            temperature=0.0,
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
            if len(diag.get("samples", [])) < 5:
                diag.setdefault("samples", []).append({
                    "enum": metric["enum"], "reason": "llm_error",
                    "error": str(e)[:200], "llm_s": round(llm_dt, 2),
                })
        return None, None, "млн. руб.", None
    llm_dt = _t.time() - llm_t0
    if diag is not None:
        diag["llm_total_s"] = round(diag.get("llm_total_s", 0.0) + llm_dt, 2)

    parsed = _parse_value_json(raw)
    cur = _coerce_num(parsed.get("current"))
    prv = _coerce_num(parsed.get("previous"))
    unit = str(parsed.get("unit") or "млн. руб.")
    source_page: Optional[int] = None
    if cur is not None or prv is not None:
        for t in candidates:
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


async def extract_stream_aifa(
    doc_id: str,
    company_type: str,
    index=None,
    limit: Optional[int] = None,
    model_id: Optional[str] = None,
    parallel: int = 8,
) -> AsyncGenerator[dict, None]:
    """Per-metric AIFA extraction: classify → score → top-3 tables → LLM.

    Yields the same event shape `_extract_stream_full_context` does so the
    UI can plug in unchanged. Falls out early with an `error` event when
    AIFA decides the document is out of scope (banks / foreign / RSBU).
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

    # OCR via IDP-OCR cloud service (same path as AIFA baseline). Cache key
    # `.idpocr.v2.json` is shared with baseline so a doc OCR'd in one mode
    # is reused in the other without re-billing.
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
    full_text = ocr.full_text
    tables = ocr.full_tables

    # AIFA scope guard.
    try:
        classify_supported(full_text)
    except AIFAUnsupportedError as e:
        yield {"type": "error", "code": "aifa_unsupported", "message": e.reason}
        return

    if not tables:
        yield {
            "type": "error",
            "code": "ocr_no_tables",
            "message": "OCR не нашёл таблиц в документе. Попробуйте режим Finengine.",
        }
        return

    # Period detection (same heuristic as full-context path).
    from finengine.skills.bulk_extractor import (
        _detect_company_from_text,
        _detect_year_from_text,
        _reconcile_company_name,
    )
    period_current = doc.period or _detect_year_from_text(full_text) or "Текущий"
    period_prev = (
        str(int(period_current) - 1) if period_current.isdigit() else "Предыдущий"
    )
    company_display = doc.company_name or _detect_company_from_text(full_text) or ""
    company_display = _reconcile_company_name(company_display, doc.filename)

    # Auto-detect kind when the upload didn't pin company_type (mirrors AIFA
    # baseline — user shouldn't have to mark "это банк" manually).
    if company_type not in ("bank", "non_financial"):
        company_type = detect_company_kind(full_text)

    # Pick metric registry by document kind (matches aifa_baseline routing).
    # Banks use the full 77-metric set (Basic_Banks reranker was trained on it);
    # non_financial stays on 55 until v2 NF reranker arrives.
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
        "_diag_tables_total": len(tables),
    }

    diag: dict = {
        "tables_total": len(tables),
        "ocr_s": round(ocr_elapsed, 1),
        "ocr": getattr(ocr, "diag", {}),
        "scoring_total_s": 0.0,
        "llm_total_s": 0.0,
        "calc_s": 0.0,
        "parallel": parallel,
        "samples": [],
    }
    metric_phase_t0 = _t.time()

    # Process metrics with bounded concurrency. Each call: score-tables
    # locally + 1 LLM round-trip; total cost ≈ 77 × LLM-call.
    sem = asyncio.Semaphore(max(1, parallel))
    extracted_current: dict[str, Optional[float]] = {}
    extracted_prev: dict[str, Optional[float]] = {}
    event_q: asyncio.Queue = asyncio.Queue()
    fail_fast = {"errors_in_row": 0, "last_error": None, "tripped": False}

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
                metric, tables, period_current, period_prev, model_id,
                doc_id=doc_id, diag=diag,
            )
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
                        from finengine.skills.aifa_baseline.extractor import _user_facing_llm_error
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

    # Calculated metrics — same machinery as the Finengine path.
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
