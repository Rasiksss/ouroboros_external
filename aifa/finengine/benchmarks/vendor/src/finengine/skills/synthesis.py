"""Synthesis pass — produce an analytical commentary on top of extracted metrics.

Pipeline:
  1. Take the metrics already extracted (passed in by the UI).
  2. Pull a few commentary-flavoured chunks from the document via FAISS.
  3. Ask the LLM to produce a short, fact-grounded narrative.

This is intentionally a *separate* step from extraction so the user can see
numbers first and only then opt into the LLM commentary (which costs more
tokens than a single metric extraction).
"""
from __future__ import annotations
from typing import AsyncIterator

from finengine.i18n import language_directive
from finengine.ingestion.indexer import VectorIndex
from finengine.llm import openrouter as llm


_SYSTEM_PROMPT = (
    "Ты — финансовый аналитик. Тебе дают извлечённые метрики и фрагменты "
    "комментариев менеджмента из отчёта. Сформируй краткий аналитический "
    "разбор: что изменилось, почему, на что обратить внимание. "
    "Только факты из контекста, никаких догадок. Если данных не хватает — "
    "честно скажи. Структура: 3–5 буллетов, в конце 1–2 риска."
)


def _system_prompt() -> str:
    """Prompt body plus the answer-language directive for this request."""
    return f"{_SYSTEM_PROMPT}\n\n{language_directive()}"


def _format_metrics(metrics: list[dict]) -> str:
    """Render the metrics list as a compact table-like text block."""
    if not metrics:
        return "(метрики не переданы)"
    periods: list[str] = []
    seen_periods: set[str] = set()
    for m in metrics:
        for pv in m.get("period_values") or []:
            period = str(pv.get("period") or "").strip()
            if period and period not in seen_periods:
                seen_periods.add(period)
                periods.append(period)

    if periods:
        lines = ["| Метрика | Сегмент | Разрез | " + " | ".join(periods) + " | Единица |"]
        lines.append("|---|---|---|" + "|".join("---" for _ in periods) + "|---|")
        for m in metrics:
            values = {
                str(pv.get("period") or ""): pv.get("value")
                for pv in (m.get("period_values") or [])
            }
            if not any(v is not None for v in values.values()):
                continue
            name = m.get("name_ru") or m.get("enum") or "?"
            row = [
                str(name),
                str(m.get("segment") or ""),
                str(m.get("dimension") or ""),
                *["—" if values.get(p) is None else str(values.get(p)) for p in periods],
                str(m.get("unit") or ""),
            ]
            lines.append("| " + " | ".join(cell.replace("|", "/") for cell in row) + " |")
        return "\n".join(lines) if len(lines) > 2 else "(нет ненулевых значений)"

    lines = []
    for m in metrics:
        name = m.get("name_ru") or m.get("enum") or "?"
        cur = m.get("value_current")
        prev = m.get("value_prev")
        unit = m.get("unit") or ""
        if cur is None and prev is None:
            continue
        delta = ""
        if cur is not None and prev not in (None, 0):
            try:
                d = (float(cur) - float(prev)) / abs(float(prev)) * 100
                delta = f" (Δ {d:+.1f}%)"
            except Exception:
                pass
        segment = f" [{m.get('dimension') or 'segment'}: {m.get('segment')}]" if m.get("segment") else ""
        lines.append(f"- {name}{segment}: {cur} (тек.) / {prev} (пред.) {unit}{delta}".rstrip())
    return "\n".join(lines) if lines else "(нет ненулевых значений)"


def _collect_commentary(
    index: VectorIndex,
    doc_id: str,
    owner_id: int | None,
    period_current: str,
    period_prev: str,
) -> list[str]:
    """Pick chunks that look like management commentary, not raw tables."""
    queries = [
        f"факторы изменения результатов {period_current}",
        f"комментарий менеджмента {period_current} {period_prev}",
        f"причины динамика выручка прибыль",
        f"риски стратегия перспективы",
    ]
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        items = index.search(
            q,
            top_k=3,
            doc_ids=[doc_id],
            owner_id=owner_id,
            min_score=0.2,
        )
        for it in items:
            if it.chunk_type == "table":
                continue
            text = str(it.content).strip()
            key = text[:120]
            if key in seen:
                continue
            seen.add(key)
            out.append(f"[{it.source_ref}]\n{text}")
            if len(out) >= 8:
                return out
    return out


async def synthesize_stream(
    *,
    index: VectorIndex,
    doc_id: str,
    owner_id: int | None,
    metrics: list[dict],
    period_current: str = "",
    period_prev: str = "",
    company_name: str = "",
) -> AsyncIterator[str]:
    """Yield analytical text deltas as the LLM produces them."""
    doc = index.get_document(doc_id)
    label = company_name or (doc.company_name if doc else "") or (doc.filename if doc else doc_id)

    metrics_block = _format_metrics(metrics)
    commentary = _collect_commentary(index, doc_id, owner_id, period_current, period_prev)
    commentary_block = (
        "\n\n---\n\n".join(commentary) if commentary else "(комментариев менеджмента не найдено)"
    )

    prompt = (
        f"## Компания: {label}\n"
        f"Период: {period_current} vs {period_prev}\n\n"
        f"## Извлечённые метрики:\n{metrics_block}\n\n"
        f"## Фрагменты из отчёта:\n{commentary_block}\n\n"
        "Сформируй разбор по схеме из системного промпта."
    )

    async for delta in llm.stream(prompt=prompt, system_prompt=_system_prompt()):
        yield delta
