"""Состав холдинга: отбор таблиц-кандидатов и стадия модели по ним.

⚠️ ОТБОР КАНДИДАТОВ РАБОТАЕТ НЕВЕРНО (#251, доказано 16.08). На Совкомбанке
лучшим кандидатом с баллом 12 вышел отчёт о движении денежных средств, на
Ак Барсе — таблица иерархии справедливой стоимости. Модель по инструкции
возвращает пустой список — правильное поведение на неправильном входе.
Это не вопрос порогов: признак «состав группы» ранжировщик не измеряет вовсе,
а высокий балл даёт таблицам, где просто много чисел и процентов.
"""
from __future__ import annotations

from pathlib import Path
import re
from typing import AsyncGenerator, Optional
from finengine.llm import openrouter as llm
from finengine.skills.periods import normalize_period_label, sort_periods
from .debug_io import _write_llm_debug_file
from .json_parse import _parse_extract_json
from .period_values import _first_value
from ._log import _LOG


CONSOLIDATED_GROUP_ENUM = "CONSOLIDATED_GROUP_OWNERSHIP"

CONSOLIDATED_GROUP_DIMENSION = "consolidated_group"

_GROUP_ENTITY_KEYWORDS = (
    "компания",
    "компании",
    "наименование",
    "название",
    "организация",
    "общество",
    "дочерняя",
    "subsidiary",
    "subsidiaries",
    "associate",
    "associates",
    "joint venture",
    "entity",
    "company",
    "name",
)

_GROUP_SHARE_KEYWORDS = (
    "доля",
    "доли",
    "участ",
    "владен",
    "собствен",
    "голос",
    "уставном капитале",
    "effective ownership",
    "ownership interest",
    "ownership",
    "voting",
    "shareholding",
    "interest",
)

_GROUP_CONTEXT_KEYWORDS = (
    "дочерн",
    "состав группы",
    "группы",
    "subsidiar",
    "group companies",
    "consolidated group",
)

_GROUP_COMPOSITION_HEADING_KEYWORDS = (
    "основные компании группы",
    "основные компании",
    "состав группы",
    "перечень компаний группы",
    "group companies",
    "principal group companies",
    "main group companies",
    "major group companies",
)

_GROUP_SUBSIDIARY_KEYWORDS = (
    "дочерн",
    "дочерние",
    "дочерних",
    "дочерними",
    "subsidiary",
    "subsidiaries",
    "principal subsidiaries",
    "major subsidiaries",
    "significant subsidiaries",
)

_GROUP_NON_SUBSIDIARY_KEYWORDS = (
    "ассоциир",
    "ассоциирован",
    "ассоциированные",
    "ассоциированных",
    "совместн",
    "совместные",
    "совместных",
    "прочие инвестиции",
    "прочих инвестиций",
    "associate",
    "associates",
    "associated",
    "joint venture",
    "joint ventures",
    "equity method",
    "equity-accounted",
    "other investments",
    "investments in associates",
    "investments in joint",
)

_GROUP_NOT_COMPOSITION_CONTEXT_KEYWORDS = (
    "выбытие дочер",
    "выбытия дочер",
    "приобретение дочер",
    "продажа акций",
    "disposal of subsidiar",
    "disposals of subsidiar",
    "acquisition of subsidiar",
    "sale of subsidiar",
)

_GROUP_NOT_COMPOSITION_TABLE_KEYWORDS = (
    "неконтролирующая доля",
    "неконтролирующей дол",
    "non-controlling interest",
    "капитал, причитающийся акционерам",
    "statement of changes in equity",
    "отчет об изменениях в капитале",
)

_GROUP_EXCLUDE_KEYWORDS = (
    "акционеры",
    "акционер",
    "shareholders",
    "shareholder",
    "дивиденды",
    "dividends",
    "ключевой вопрос аудита",
    "key audit matter",
    "выручка",
    "revenue",
    "сегмент",
    "segment",
)

def _clean_group_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\u00a0", " ")).strip()

def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(keyword in low for keyword in keywords)

def _group_percentish_count(text: str) -> int:
    return len(re.findall(r"\b\d{1,3}(?:[,.]\d+)?\s*%", text)) + len(
        re.findall(r"\b100(?:[,.]0+)?\b", text)
    )

def _last_keyword_pos(text: str, keywords: tuple[str, ...]) -> int:
    positions = [text.rfind(keyword) for keyword in keywords]
    return max(positions) if positions else -1

def _group_context_relation_kind(context: str) -> str:
    low = context.lower()
    if _contains_any(low, _GROUP_NOT_COMPOSITION_CONTEXT_KEYWORDS):
        return "not_composition"
    subsidiary_pos = _last_keyword_pos(low, _GROUP_SUBSIDIARY_KEYWORDS)
    non_subsidiary_pos = _last_keyword_pos(low, _GROUP_NON_SUBSIDIARY_KEYWORDS)
    composition_heading_pos = _last_keyword_pos(low, _GROUP_COMPOSITION_HEADING_KEYWORDS)
    if composition_heading_pos > max(subsidiary_pos, non_subsidiary_pos):
        return ""
    if subsidiary_pos < 0 and non_subsidiary_pos < 0:
        return ""
    return "subsidiary" if subsidiary_pos >= non_subsidiary_pos else "non_subsidiary"

def _focused_group_context(context: str, relation_kind: str) -> str:
    low = context.lower()
    keywords = (
        _GROUP_SUBSIDIARY_KEYWORDS
        if relation_kind == "subsidiary"
        else _GROUP_NON_SUBSIDIARY_KEYWORDS
        if relation_kind == "non_subsidiary"
        else _GROUP_NOT_COMPOSITION_CONTEXT_KEYWORDS
        if relation_kind == "not_composition"
        else ()
    )
    pos = _last_keyword_pos(low, keywords) if keywords else -1
    if pos >= 0:
        return context[max(0, pos - 120):].strip()
    return context[-600:].strip()

def _flush_group_table_block(
    blocks: list[dict],
    lines: list[str],
    page: int | None,
    context_lines: list[str] | None = None,
) -> None:
    if len(lines) < 2:
        return
    text = "\n".join(lines).strip()
    compact = _clean_group_text(text)
    if not compact:
        return
    context = _clean_group_text(" ".join(context_lines or []))
    low = compact.lower()
    if _contains_any(low, _GROUP_NOT_COMPOSITION_TABLE_KEYWORDS):
        return
    relation_kind = _group_context_relation_kind(context)
    score = 0
    has_entity = _contains_any(low, _GROUP_ENTITY_KEYWORDS) or bool(
        re.search(r"\b(?:ооо|ао|пао|zao|llc|jsc|pjsc)\b", low, flags=re.IGNORECASE)
    )
    has_share = _contains_any(low, _GROUP_SHARE_KEYWORDS)
    has_period = bool(
        re.search(r"\b(?:20\d{2}|31\s+декабр|30\s+июн|december\s+31|june\s+30)\b", low)
    )
    percent_count = _group_percentish_count(compact)
    has_group_context = _contains_any(low, _GROUP_CONTEXT_KEYWORDS)
    has_subsidiary_context = relation_kind == "subsidiary"
    has_non_subsidiary_context = relation_kind in {"non_subsidiary", "not_composition"}
    has_share_signal = has_share or percent_count >= 2
    if has_entity:
        score += 3
    if has_share:
        score += 5
    if has_period:
        score += 2
    if percent_count:
        score += min(4, percent_count)
    if has_group_context:
        score += 2
    if has_subsidiary_context:
        score += 7
    if has_non_subsidiary_context and not has_subsidiary_context:
        score -= 10
    if _contains_any(low, _GROUP_EXCLUDE_KEYWORDS) and not has_group_context:
        score -= 6
    if has_non_subsidiary_context and not has_subsidiary_context:
        return
    if not has_share_signal or not has_entity or score < 7:
        return
    blocks.append(
        {
            "page": page,
            "score": score,
            "text": text,
            "context": _focused_group_context(context, relation_kind),
            "chars": len(text),
            "subsidiary_context": has_subsidiary_context,
            "non_subsidiary_context": has_non_subsidiary_context,
        }
    )

def _consolidated_group_candidate_tables(
    pdf_text: str,
    *,
    max_tables: int = 24,
) -> list[dict]:
    blocks: list[dict] = []
    buf: list[str] = []
    block_page: int | None = None
    block_context: list[str] = []
    current_page: int | None = None
    recent_text_lines: list[str] = []

    for raw_line in (pdf_text or "").splitlines():
        page_match = re.match(r"\s*===PAGE\s+(\d+)===", raw_line)
        if page_match:
            _flush_group_table_block(blocks, buf, block_page, block_context)
            buf = []
            block_page = None
            block_context = []
            current_page = int(page_match.group(1))
            recent_text_lines = []
            continue
        line = raw_line.rstrip()
        if "|" in line:
            if not buf:
                block_page = current_page
                block_context = list(recent_text_lines[-4:])
            buf.append(line)
            continue
        _flush_group_table_block(blocks, buf, block_page, block_context)
        buf = []
        block_page = None
        block_context = []
        cleaned_line = _clean_group_text(line)
        if cleaned_line:
            recent_text_lines.append(cleaned_line)
            recent_text_lines = recent_text_lines[-8:]
    _flush_group_table_block(blocks, buf, block_page, block_context)

    subsidiary_blocks = [block for block in blocks if block.get("subsidiary_context")]
    if subsidiary_blocks:
        blocks = subsidiary_blocks
    blocks.sort(key=lambda item: (item["score"], item["chars"]), reverse=True)
    return blocks[:max_tables]

def _build_consolidated_group_context(
    pdf_text: str,
    *,
    max_chars: int = 60_000,
    max_tables: int = 24,
) -> tuple[str, list[dict]]:
    candidates = _consolidated_group_candidate_tables(pdf_text, max_tables=max_tables)
    parts: list[str] = []
    used = 0
    for idx, candidate in enumerate(candidates, start=1):
        text = str(candidate.get("text") or "")
        header = (
            f"[Candidate table {idx}; page={candidate.get('page') or 'unknown'}; "
            f"score={candidate.get('score')}]"
        )
        context = str(candidate.get("context") or "").strip()
        context_line = f"\nContext before table: {context}" if context else ""
        chunk = f"{header}{context_line}\n{text}"
        if used + len(chunk) > max_chars and parts:
            break
        parts.append(chunk)
        used += len(chunk)
    return "\n\n".join(parts), candidates[: len(parts)]

def _normalize_group_period_label(raw_period: object) -> str:
    raw = _clean_group_text(raw_period)
    if not raw:
        return ""
    normalized = normalize_period_label(raw)
    if match := re.fullmatch(r"FY\s+(20\d{2})", normalized, flags=re.IGNORECASE):
        return f"31.12.{match.group(1)}"
    if match := re.fullmatch(r"(20\d{2})", raw):
        return f"31.12.{match.group(1)}"
    return normalized

def _normalize_group_share_value(raw_value: object, unit_text: object = "") -> object | None:
    if raw_value is None:
        return None
    if isinstance(raw_value, (int, float)):
        return f"{raw_value:g}%"
    text = _clean_group_text(raw_value)
    if not text or text.lower() in {"null", "none", "n/a", "нет", "не применимо"}:
        return None
    if text in {"-", "—", "–", "−"}:
        return "-"
    low_unit = str(unit_text or "").lower()
    if "%" not in text and re.fullmatch(r"\d{1,3}(?:[,.]\d+)?", text):
        if "%" in low_unit or "процент" in low_unit or "percent" in low_unit:
            return f"{text}%"
    return text

def _is_plausible_group_share_value(value: object) -> bool:
    if value is None:
        return False
    text = _clean_group_text(value).lower()
    if text in {"-", "—", "–", "−"}:
        return True
    if re.search(r"\d", text) and (
        "%" in text
        or "акци" in text
        or "share" in text
        or re.fullmatch(r"\d{1,3}(?:[,.]\d+)?%?", text)
    ):
        return True
    return False

def _group_value_for_period(period_values: list[dict], target: str) -> object | None:
    target_norm = _normalize_group_period_label(target)
    for item in period_values:
        if _normalize_group_period_label(item.get("period")) == target_norm:
            return item.get("value")
    return None

def _group_row_is_non_subsidiary(row: dict) -> bool:
    relation = _clean_group_text(row.get("relation") or row.get("relationship") or row.get("type")).lower()
    table_type = _clean_group_text(row.get("table_type") or row.get("category")).lower()
    source = _clean_group_text(row.get("source_text") or "").lower()
    probe = " ".join(part for part in (relation, table_type, source) if part)
    if not probe:
        return False
    if _contains_any(probe, _GROUP_NON_SUBSIDIARY_KEYWORDS):
        return True
    return relation in {"associate", "associates", "associated", "joint_venture", "joint venture", "other"}

def _normalise_consolidated_group_rows(
    rows: object,
    *,
    period_current: str,
    period_prev: str,
    candidate_pages: dict[int, int | None] | None = None,
    candidate_allowed: dict[int, bool] | None = None,
) -> list[dict]:
    if not isinstance(rows, list):
        return []
    candidate_pages = candidate_pages or {}
    events_by_company: dict[str, dict] = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        candidate_id: int | None = None
        if row.get("candidate_id") is not None:
            try:
                candidate_id = int(row.get("candidate_id"))
            except Exception:
                candidate_id = None
        if candidate_id is not None and candidate_allowed is not None and not candidate_allowed.get(candidate_id, True):
            continue
        if _group_row_is_non_subsidiary(row):
            continue
        name = _clean_group_text(
            row.get("company_name")
            or row.get("company")
            or row.get("name")
            or row.get("entity")
            or row.get("organization")
        )
        if len(name) < 2 or name.lower() in {"итого", "total", "всего"}:
            continue
        values = row.get("values")
        if not isinstance(values, dict):
            continue
        unit_text = row.get("unit_text") or row.get("unit") or "%"
        raw_period_values: list[dict] = []
        for raw_period, raw_value in values.items():
            period = _normalize_group_period_label(raw_period)
            if not period:
                continue
            value = _normalize_group_share_value(raw_value, unit_text)
            if not _is_plausible_group_share_value(value):
                continue
            raw_period_values.append(
                {
                    "period": period,
                    "raw_period": _clean_group_text(raw_period),
                    "value": value,
                    "unit": "%",
                    "page": None,
                    "source_text": _clean_group_text(row.get("source_text") or ""),
                }
            )
        if not raw_period_values:
            continue

        page_raw = row.get("page")
        if not page_raw and candidate_id is not None:
            page_raw = candidate_pages.get(candidate_id)
        try:
            page = int(page_raw) if page_raw is not None else None
        except Exception:
            page = None
        if page is not None and page <= 0:
            page = None
        for pv in raw_period_values:
            pv["page"] = page

        period_values = [
            item for item in raw_period_values if item.get("value") is not None
        ]
        by_period = {
            str(item.get("period") or ""): item
            for item in period_values
            if item.get("period")
        }
        period_values = [by_period[p] for p in sort_periods(list(by_period))]
        value_current = _group_value_for_period(period_values, period_current)
        if value_current is None:
            value_current = _first_value(period_values)
        value_prev = _group_value_for_period(period_values, period_prev)

        source_bits = []
        for key in ("relation", "registration", "activity"):
            value = _clean_group_text(row.get(key))
            if value:
                source_bits.append(f"{key}: {value}")
        source_note = "; ".join(source_bits)
        event = {
            "type": "metric",
            "enum": CONSOLIDATED_GROUP_ENUM,
            "name_ru": name,
            "value_current": value_current,
            "value_prev": value_prev,
            "period_values": period_values,
            "unit": "%",
            "found": True,
            "is_calculated": False,
            "formula": "",
            "page": page,
            "segment": name,
            "dimension": CONSOLIDATED_GROUP_DIMENSION,
            "source_text": source_note,
            "group_relation": _clean_group_text(row.get("relation")),
            "group_registration": _clean_group_text(row.get("registration")),
            "group_activity": _clean_group_text(row.get("activity")),
        }

        key = name.lower()
        existing = events_by_company.get(key)
        if not existing:
            events_by_company[key] = event
            continue
        merged: dict[str, dict] = {
            str(item.get("period") or ""): item
            for item in existing.get("period_values") or []
            if item.get("period")
        }
        for item in period_values:
            period = str(item.get("period") or "")
            if period and period not in merged:
                merged[period] = item
        existing["period_values"] = [
            merged[p] for p in sort_periods(list(merged.keys()))
        ]
        existing["value_current"] = (
            _group_value_for_period(existing["period_values"], period_current)
            or _first_value(existing["period_values"])
        )
        existing["value_prev"] = _group_value_for_period(existing["period_values"], period_prev)
        if not existing.get("page") and page:
            existing["page"] = page

    return sorted(events_by_company.values(), key=lambda item: str(item.get("name_ru") or "").lower())

async def _extract_consolidated_group_full_context(
    *,
    pdf_text_for_prompt: str,
    period_current: str,
    period_prev: str,
    company_display: str,
    reporting_standard: str,
    company_type: str,
    model_id: Optional[str],
    debug_dir: Optional[Path] = None,
) -> AsyncGenerator[dict, None]:
    """Extract group composition / ownership rows for the generic role."""
    context, candidates = _build_consolidated_group_context(pdf_text_for_prompt)
    diag_base = {
        "consolidated_group_candidate_tables": len(candidates),
        "consolidated_group_rows": 0,
        "consolidated_group_company_type": company_type,
    }
    if not candidates or not context.strip():
        _LOG.info(
            "Consolidated group stage skipped for %s (%s): no candidate tables",
            company_display,
            company_type,
        )
        yield {
            "type": "consolidated_group_diag",
            "_diag_consolidated_group": {
                **diag_base,
                "consolidated_group_skip_reason": "no_candidate_tables",
            },
        }
        return

    candidate_pages = {
        idx: candidate.get("page")
        for idx, candidate in enumerate(candidates, start=1)
    }
    candidate_allowed = {
        idx: not bool(candidate.get("non_subsidiary_context"))
        for idx, candidate in enumerate(candidates, start=1)
    }
    system_prompt = (
        "You extract only consolidated group composition tables from financial "
        "reports. You are careful, literal, and return JSON only."
    )
    user_prompt = (
        f"# Company\n{company_display}\n\n"
        f"# Reporting standard\n{reporting_standard}\n\n"
        "# Task\n"
        "Find ONLY subsidiaries / дочерние общества (ДЗО) that form the "
        "consolidated group and are included in consolidation. Extract "
        "ownership/effective interest/voting share percentages by period.\n\n"
        "# Table selection\n"
        "- Use only the single table whose heading/context says subsidiaries, "
        "дочерние общества, дочерние предприятия, or principal/significant subsidiaries.\n"
        "- If several candidate tables are shown, choose that one subsidiaries table "
        "and ignore all other candidate tables.\n"
        "- If the candidates do not contain a subsidiaries table, return "
        '{"companies": []}.\n\n'
        "# Strict exclusions\n"
        "- Do NOT extract shareholders of the parent company.\n"
        "- Do NOT extract associates / ассоциированные общества.\n"
        "- Do NOT extract joint ventures / совместные предприятия.\n"
        "- Do NOT extract equity-accounted investees or other investments.\n"
        "- Do NOT extract operating segments, revenue structure, related-party "
        "transactions, investments, auditors' key matters, or financial metrics.\n"
        "# Value rules\n"
        "- Prefer ownership/effective interest/shareholding over voting rights.\n"
        "- Use voting rights only if ownership/effective interest is absent.\n"
        "- Preserve literal values: `100,00%`, `75% минус одна акция`, `-`.\n"
        "- If a value is written as `100` in a percentage column, return `100%`.\n"
        "- Keep period headers literally as written in the table.\n\n"
        "# Candidate tables\n"
        f"{context}\n\n"
        "# Response format\n"
        "Return one valid JSON object only:\n"
        "{\n"
        '  "companies": [\n'
        "    {\n"
        '      "candidate_id": 1,\n'
        '      "company_name": "ООО «Example»",\n'
        '      "relation": "subsidiary|unknown",\n'
        '      "registration": "Москва, РФ",\n'
        '      "activity": "Воздушные перевозки",\n'
        '      "values": {"31 декабря 2024 г.": "100,00%"},\n'
        '      "unit_text": "%",\n'
        '      "page": 17,\n'
        '      "source_text": "short source row excerpt"\n'
        "    }\n"
        "  ]\n"
        "}\n"
    )
    _write_llm_debug_file(debug_dir, "consolidated_group_system.md", system_prompt)
    _write_llm_debug_file(debug_dir, "consolidated_group_prompt.md", user_prompt)

    import time as _ttiming
    llm_t0 = _ttiming.time()
    _LOG.info(
        "Consolidated group stage started for %s (%s): %s candidate tables",
        company_display,
        company_type,
        len(candidates),
    )
    try:
        raw, usage = await llm.complete_with_usage(
            prompt=user_prompt,
            system=system_prompt,
            model=model_id,
            response_format={"type": "json_object"},
            max_output=18000,
        )
    except Exception as e:
        _LOG.exception(
            "Consolidated group stage failed for %s (%s)",
            company_display,
            company_type,
        )
        yield {
            "type": "consolidated_group_diag",
            "_diag_consolidated_group": {
                **diag_base,
                "consolidated_group_error": f"{type(e).__name__}: {e}"[:300],
            },
        }
        return

    group_llm_s = round(_ttiming.time() - llm_t0, 1)
    _write_llm_debug_file(debug_dir, "consolidated_group_response.json", raw)
    parsed = _parse_extract_json(raw) or {}
    rows = parsed.get("companies") if isinstance(parsed, dict) else []
    events = _normalise_consolidated_group_rows(
        rows,
        period_current=period_current,
        period_prev=period_prev,
        candidate_pages=candidate_pages,
        candidate_allowed=candidate_allowed,
    )
    for event in events:
        yield event
    _LOG.info(
        "Consolidated group stage finished for %s (%s): %s rows from %s candidates in %.1fs",
        company_display,
        company_type,
        len(events),
        len(candidates),
        group_llm_s,
    )

    diag = {
        **diag_base,
        "consolidated_group_rows": len(events),
        "consolidated_group_llm_s": group_llm_s,
    }
    if usage:
        from finengine.skills.pricing import cost_usd

        usage_model = str(usage.get("model") or model_id or "")
        tokens_in = int(
            usage.get("in")
            or usage.get("prompt_tokens")
            or usage.get("input_tokens")
            or 0
        )
        tokens_out = int(
            usage.get("out")
            or usage.get("completion_tokens")
            or usage.get("output_tokens")
            or 0
        )
        tokens_cached = int(usage.get("cached") or 0)
        diag.update(
            {
                "consolidated_group_tokens_in": tokens_in,
                "consolidated_group_tokens_out": tokens_out,
                "consolidated_group_tokens_cached": tokens_cached,
                "llm_requests": [
                    {
                        "stage": "consolidated_group",
                        "model": usage_model,
                        "elapsed_s": group_llm_s,
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                        "tokens_cached": tokens_cached,
                        "cost_usd": round(
                            cost_usd(usage_model, tokens_in, tokens_out, tokens_cached),
                            4,
                        ),
                    }
                ],
            }
        )
    yield {
        "type": "consolidated_group_diag",
        "_diag_consolidated_group": diag,
    }
