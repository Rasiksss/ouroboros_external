"""Hidden IFRS-bank top-borrower sector concentration extraction.

The public metric table should not display ``TOP_BORROWERS`` yet. The value is
prepared as a calculation input: each period stores the sum of top-N sector
amounts in ``value`` and the structured sector rows in ``borrowers``.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
from typing import Any, AsyncIterator, Callable

from finengine.skills.periods import POINT_KIND, normalize_period_label


TOP_BORROWERS_ENUM = "TOP_BORROWERS"
TOP_BORROWERS_SYSTEM_PROMPT = (
    "Ты извлекаешь структурированные данные из банковской отчетности МСФО. "
    "Отвечай только JSON."
)
DEFAULT_TOP_BORROWERS_MIN_TABLE_SCORE = 65.0


@dataclass(frozen=True)
class CandidateTable:
    page: int | None
    score: float
    text: str
    table_index: int | None = None
    caption: str = ""


_POSITIVE_PATTERNS: tuple[str, ...] = (
    "концентрация",
    "кредитн",
    "заемщик",
    "заёмщик",
    "ссуд",
    "клиент",
    "отрасл",
    "сектор",
    "экономик",
    "вид экономической деятельности",
    "loans",
    "borrower",
    "customer",
    "industry",
    "sector",
    "economic sector",
    "concentration",
)

_NEGATIVE_PATTERNS: tuple[str, ...] = (
    "депозит",
    "вклад",
    "средства клиентов",
    "ценные бумаги",
    "securities",
    "deposit",
)

_PRIVATE_INDIVIDUAL_SECTOR_RE = re.compile(
    r"\b("
    r"частн\w*\s+(лица|лицам|лиц|клиент\w*)|"
    r"физическ\w*\s+(лица|лицам|лиц)|"
    r"населени\w*|"
    r"ипотек\w*|"
    r"потребительск\w*|"
    r"individuals?|"
    r"private\s+(individuals?|persons?|customers?|clients?)|"
    r"retail\s+(customers?|clients?)|"
    r"personal\s+loans?|"
    r"mortgage|consumer\s+loans?"
    r")\b",
    re.IGNORECASE,
)


def _norm(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower().replace("ё", "е")).strip()


def _is_private_individual_sector(sector: object) -> bool:
    return bool(_PRIVATE_INDIVIDUAL_SECTOR_RE.search(_norm(sector)))


def _iter_pages(text: str) -> list[tuple[int | None, str]]:
    parts = re.split(r"(?m)^===PAGE\s+(\d+)===\s*$", text or "")
    if len(parts) == 1:
        return [(None, text or "")]
    out: list[tuple[int | None, str]] = []
    prefix = parts[0].strip()
    if prefix:
        out.append((None, prefix))
    for idx in range(1, len(parts), 2):
        try:
            page = int(parts[idx])
        except Exception:
            page = None
        out.append((page, parts[idx + 1] if idx + 1 < len(parts) else ""))
    return out


def _read_json_payload(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    payload = data.get("ocr_result") if isinstance(data.get("ocr_result"), dict) else data
    if isinstance(payload, dict) and (payload.get("tables") or payload.get("text")):
        return payload
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        try:
            from finengine.skills.aifa_baseline.idp_ocr_client import markdown_from_saved_idp_response

            text = markdown_from_saved_idp_response(data)
        except Exception:
            text = ""
        if text.strip():
            return {"text": text}
    return payload if isinstance(payload, dict) else None


def _ocr_table_body(table: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("caption", "annotation", "formatted_str", "text", "markdown"):
        value = table.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n".join(parts).strip()


def rank_sector_concentration_tables_from_ocr_json(
    ocr_json_path: Path | None,
    *,
    top_k: int = 3,
    min_score: float = DEFAULT_TOP_BORROWERS_MIN_TABLE_SCORE,
) -> list[CandidateTable]:
    """Use the project table ranker when an OCR JSON payload is available."""
    payload = _read_json_payload(ocr_json_path)
    if not payload:
        return []
    tables = payload.get("tables") or []
    if not isinstance(tables, list) or not tables:
        text = payload.get("text") if isinstance(payload.get("text"), str) else ""
        return rank_sector_concentration_tables(text, top_k=top_k, min_score=min_score) if text else []
    from finengine.skills.loan_sector_table_ranker import rank_payload

    try:
        ranked = rank_payload(
            payload,
            ocr_json_path.name if ocr_json_path else "document.json",
            min_score=min_score,
        )
    except Exception:
        return []

    out: list[CandidateTable] = []
    for item in ranked:
        score = float(getattr(item, "score", 0.0) or 0.0)
        if score < min_score:
            continue
        table_index = getattr(item, "table_index", None)
        if not isinstance(table_index, int) or not (0 <= table_index < len(tables)):
            continue
        table = tables[table_index]
        if not isinstance(table, dict):
            continue
        body = _ocr_table_body(table)
        if not body:
            body = str(getattr(item, "snippet", "") or "")
        out.append(
            CandidateTable(
                page=getattr(item, "page", None),
                score=score,
                text=body[:7000],
                table_index=table_index,
                caption=str(getattr(item, "caption", "") or table.get("caption") or ""),
            )
        )
        if len(out) >= top_k:
            break
    return out


def _looks_like_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.count("|") >= 2


def _iter_markdown_tables(text: str) -> list[tuple[int | None, str]]:
    tables: list[tuple[int | None, str]] = []
    for page, page_text in _iter_pages(text):
        lines = page_text.splitlines()
        idx = 0
        while idx < len(lines):
            if not _looks_like_table_line(lines[idx]):
                idx += 1
                continue
            start = idx
            while idx < len(lines) and _looks_like_table_line(lines[idx]):
                idx += 1
            heading = "\n".join(lines[max(0, start - 6):start]).strip()
            body = "\n".join(lines[start:idx]).strip()
            if body:
                tables.append((page, "\n".join(part for part in (heading, body) if part).strip()))
    return tables


def _table_score(text: str) -> float:
    low = _norm(text)
    score = 0.0
    for pattern in _POSITIVE_PATTERNS:
        if pattern in low:
            score += 1.0
    if ("отрасл" in low or "сектор" in low or "industry" in low or "sector" in low) and (
        "кредит" in low or "loans" in low or "ссуд" in low
    ):
        score += 4.0
    if "топ" in low or "top" in low or "крупнейш" in low or "largest" in low:
        score += 1.5
    for pattern in _NEGATIVE_PATTERNS:
        if pattern in low:
            score -= 0.8
    return max(0.0, min(100.0, score * 10.0))


def rank_sector_concentration_tables(
    text: str,
    *,
    top_k: int = 3,
    min_score: float = DEFAULT_TOP_BORROWERS_MIN_TABLE_SCORE,
) -> list[CandidateTable]:
    """Return top candidate markdown tables for loan sector concentration.

    This is the built-in deterministic fallback. A future external ranker can
    replace this function without changing the extraction contract.
    """
    ranked: list[CandidateTable] = []
    for page, table in _iter_markdown_tables(text):
        score = _table_score(table)
        if score < min_score:
            continue
        ranked.append(CandidateTable(page=page, score=score, text=table[:7000]))
    ranked.sort(key=lambda item: (item.score, len(item.text)), reverse=True)
    return ranked[:top_k]


def build_top_borrowers_prompt(
    tables: list[CandidateTable],
    *,
    top_n: int,
    periods_detected: list[str],
    period_current: str,
    period_prev: str,
) -> str:
    rendered_tables = []
    for idx, table in enumerate(tables, start=1):
        page = f"page {table.page}" if table.page else "unknown page"
        rendered_tables.append(
            f"## Candidate table {idx} ({page}, rank_score={table.score:.2f})\n{table.text}"
        )
    tables_text = "\n\n".join(rendered_tables) or "No candidate tables were found."
    periods = ", ".join(periods_detected or [period_current, period_prev])
    return f"""
Ты финансовый аналитик по банковской отчетности МСФО.

Задача: из таблиц ниже извлечь топ-{top_n} отраслей/секторов экономики по
концентрации кредитов/займов юридическим лицам / корпоративным клиентам.
Нужны именно отрасли экономики заемщиков-юрлиц, а не география, стадии ECL,
типы клиентов, валюты, обеспечения или депозиты.

Строго исключай физических лиц: строки "Частные лица", "Физические лица",
"Private individuals", "Retail customers", ипотека, потребительские кредиты
и любые другие частные/розничные клиенты никогда не включаются в топ и не
влияют на ранжирование. Топ-{top_n} нужно выбирать уже после исключения
физических лиц.

Если есть таблица или таблицы по группе компаний, выбирай их и игнорируй таблицу 
по компании. Если таблица раскрывает больше {top_n} строк, верни только {top_n} 
крупнейших строк по сумме. Если строк меньше, верни сколько есть. Не придумывай 
сектор и не суммируй разные строки самостоятельно, кроме строки с точным значением 
в таблице. Сохраняй значения буквально как в ячейке, включая скобки/минус.

Ориентиры периодов отчета: {periods}
Текущий период: {period_current}
Предыдущий период: {period_prev}

Верни только JSON строго такого вида:
{{
  "{TOP_BORROWERS_ENUM}": {{
    "values": {{
      "<период как в шапке таблицы>": [
        {{"sector": "<название сектора>", "value": "<значение как в ячейке>"}}
      ]
    }},
    "unit_text": "<единица измерения как в таблице>",
    "page": <номер страницы или null>
  }}
}}

Таблицы-кандидаты:
{tables_text}
""".strip()


def _parse_numeric(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text in {"-", "—", "–"}:
        return 0.0
    negative = bool(re.match(r"^\s*\(.*\)\s*$", text))
    text = text.strip("()")
    text = re.sub(r"[^\d,.\-]", "", text.replace("\u00a0", " ").replace(" ", ""))
    if not text:
        return None
    if text.count(",") == 1 and text.count(".") == 0:
        head, tail = text.split(",", 1)
        text = f"{head}.{tail}" if 1 <= len(tail) <= 2 else head + tail
    elif text.count(",") > 0:
        text = text.replace(",", "")
    try:
        number = float(text)
    except Exception:
        return None
    return -abs(number) if negative else number


def _extract_json_object(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def parse_top_borrowers_response(
    response: str,
    *,
    top_n: int,
    default_unit: str,
) -> list[dict[str, Any]]:
    data = _extract_json_object(response)
    payload = data.get(TOP_BORROWERS_ENUM) if isinstance(data.get(TOP_BORROWERS_ENUM), dict) else data
    values = payload.get("values") if isinstance(payload, dict) else {}
    if not isinstance(values, dict):
        return []
    unit = str(payload.get("unit_text") or default_unit or "").strip()
    page = payload.get("page")
    out: list[dict[str, Any]] = []
    for raw_period, raw_items in values.items():
        period = normalize_period_label(raw_period, value_kind=POINT_KIND)
        if not period:
            continue
        if isinstance(raw_items, dict):
            items_iter = [
                {"sector": key, "value": value}
                for key, value in raw_items.items()
            ]
        elif isinstance(raw_items, list):
            items_iter = raw_items
        else:
            continue
        borrowers: list[dict[str, Any]] = []
        total = 0.0
        for item in items_iter:
            if len(borrowers) >= top_n:
                break
            if not isinstance(item, dict):
                continue
            sector = str(item.get("sector") or item.get("name") or item.get("borrower") or "").strip()
            if _is_private_individual_sector(sector):
                continue
            amount_raw = item.get("value")
            amount = _parse_numeric(amount_raw)
            if not sector or amount is None:
                continue
            borrowers.append({"sector": sector, "value": amount, "raw_value": amount_raw})
            total += amount
        if borrowers:
            out.append(
                {
                    "period": period,
                    "raw_period": raw_period,
                    "value": total,
                    "unit": unit,
                    "page": page,
                    "source_text": "",
                    "borrowers": borrowers,
                }
            )
    return out


async def extract_top_borrowers_period_values(
    pdf_text: str,
    *,
    ocr_json_path: Path | None = None,
    top_n: int,
    periods_detected: list[str],
    period_current: str,
    period_prev: str,
    default_unit: str,
    model_id: str | None,
    debug_dir: Path | None,
    stream_fn: Callable[..., AsyncIterator[str]],
    min_table_score: float = DEFAULT_TOP_BORROWERS_MIN_TABLE_SCORE,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tables = rank_sector_concentration_tables_from_ocr_json(
        ocr_json_path,
        top_k=3,
        min_score=min_table_score,
    )
    ranker_source = "ocr_payload" if tables else "fallback"
    if not tables:
        tables = rank_sector_concentration_tables(pdf_text, top_k=3, min_score=min_table_score)
    diag: dict[str, Any] = {
        "top_borrowers_enabled": True,
        "top_borrowers_n": top_n,
        "top_borrowers_candidate_tables": len(tables),
        "top_borrowers_ranker": ranker_source,
        "top_borrowers_min_table_score": min_table_score,
    }
    if debug_dir is not None:
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / "top_borrowers_tables.md").write_text(
            "\n\n".join(f"# Candidate {idx}\n{table.text}" for idx, table in enumerate(tables, start=1)),
            encoding="utf-8",
        )
    if not tables:
        diag["top_borrowers_skip_reason"] = "no_candidate_tables_above_threshold"
        return [], diag
    prompt = build_top_borrowers_prompt(
        tables,
        top_n=top_n,
        periods_detected=periods_detected,
        period_current=period_current,
        period_prev=period_prev,
    )
    system_prompt = TOP_BORROWERS_SYSTEM_PROMPT
    if debug_dir is not None:
        (debug_dir / "top_borrowers_prompt.md").write_text(prompt, encoding="utf-8")
        (debug_dir / "top_borrowers_system_prompt.md").write_text(system_prompt, encoding="utf-8")
    usage_sink: dict[str, int] = {"in": 0, "out": 0, "cached": 0}
    chunks: list[str] = []
    llm_t0 = time.perf_counter()
    async for delta in stream_fn(
        prompt=prompt,
        system_prompt=system_prompt,
        model=model_id,
        response_format={"type": "json_object"},
        max_output=4096,
        usage_sink=usage_sink,
    ):
        chunks.append(delta)
    llm_elapsed = time.perf_counter() - llm_t0
    response = "".join(chunks)
    if debug_dir is not None:
        (debug_dir / "top_borrowers_response.json").write_text(response, encoding="utf-8")
    period_values = parse_top_borrowers_response(
        response,
        top_n=top_n,
        default_unit=default_unit,
    )
    diag.update(
        {
            "top_borrowers_period_values": len(period_values),
            "top_borrowers_tokens_in": int(usage_sink.get("in", 0) or 0),
            "top_borrowers_tokens_out": int(usage_sink.get("out", 0) or 0),
            "top_borrowers_tokens_cached": int(usage_sink.get("cached", 0) or 0),
            "top_borrowers_llm_s": round(llm_elapsed, 2),
        }
    )
    return period_values, diag
