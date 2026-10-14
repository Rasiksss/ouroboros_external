"""Sequential chain runner — minimal `mmar_carl.ReasoningChain` replacement.

Walks one chain (a list[StepDescription]) step-by-step, calling the LLM
once per step. Each step is given:
  - the metrics table relevant to its `step_context_queries`
  - the title / aim / questions / stage_action / example_reasoning
  - a running history of previous steps' outputs

The output is a markdown synthesis the user can read top-to-bottom: a
section per step + an executive-summary at the end.

Streams via async generator so the API can SSE the partial output as it
lands. The chain itself runs sequentially (each step depends on the
previous via history); within a step the LLM is a single call.
"""
from __future__ import annotations

import asyncio
from typing import AsyncGenerator, Iterable

from mmar_carl import StepDescription

from finengine.i18n import language_directive, reply_locale, scaffold, t
from finengine.llm import openrouter as llm
from finengine.skills.reasoner.titles import localize_title
from finengine.skills.reasoner.chains import (
    STEP_CONFIG,
    executive_summary_prompt,
    pseudo_reasoning_step_to_prompt,
    step_by_step_prompting,
)


def _format_metric_row(name: str, value_current, value_prev, unit: str) -> str:
    cur = "—" if value_current is None else f"{value_current}"
    prev = "—" if value_prev is None else f"{value_prev}"
    return f"- {name}: {cur} (тек.) / {prev} (пред.) {unit}".rstrip()


def build_metrics_table_str(metrics: list[dict], filter_enums: Iterable[str] | None = None) -> str:
    """Render the extracted metrics as a markdown bullet list (one row per
    enum). When `filter_enums` is non-empty, keep only those — used so each
    chain step sees only the metrics it cares about.
    """
    selected = list(filter_enums) if filter_enums else None
    rows = []
    for m in metrics:
        if selected is not None and m.get("enum") not in selected:
            continue
        name = m.get("name_ru") or m.get("enum") or ""
        if not name:
            continue
        rows.append(_format_metric_row(
            name=name,
            value_current=m.get("value_current"),
            value_prev=m.get("value_prev"),
            unit=m.get("unit", "") or "",
        ))
    return "\n".join(rows) if rows else "(нет данных по этой группе показателей)"


_ENUM_TOKEN_RE = __import__("re").compile(r"\b[A-Z][A-Z0-9_]{2,}\b")


def _translate_enums(text: str, metrics: list[dict]) -> str:
    """Replace METRIC_ENUM tokens in a string with their human-readable
    names from the metric registry, in the current reply locale.

    Upstream chain definitions (financier-reasoner/cot/prompts.py) write
    titles like "Маржинальность по OPERATING_PROFIT" — convenient for the
    LLM prompt but ugly when shown in chat as a section header. Build an
    enum→name map from the metrics passed in (they carry both `name_ru` and
    `name_en`) and patch the string. Tokens that don't match any known
    metric are left alone so we never silently drop something.
    """
    if not text:
        return text
    prefer_en = reply_locale() != "ru"
    name_map: dict[str, str] = {}
    for m in metrics or []:
        e = m.get("enum")
        # Fall back across languages rather than leaving a bare enum in a
        # heading: a Russian metric name reads better than SALES_REVENUE.
        n = (m.get("name_en") or m.get("name_ru")) if prefer_en else m.get("name_ru")
        if e and n and e not in name_map:
            name_map[e] = str(n)
    if not name_map:
        return text
    return _ENUM_TOKEN_RE.sub(lambda mo: name_map.get(mo.group(0), mo.group(0)), text)


async def _llm_step(
    *,
    table_str: str,
    step: StepDescription,
    history: str,
    model_id: str | None,
) -> str:
    """One step of the chain — single LLM call, returns full response text."""
    pseudo = pseudo_reasoning_step_to_prompt(step)
    user_prompt = step_by_step_prompting(table_str, pseudo, history)
    text, _usage = await llm.complete_with_usage(
        prompt=user_prompt,
        # Chain definitions stay in Russian (pivot language); the directive
        # decides what language the step text comes back in. Section titles
        # are authored in chains.py and are NOT covered by this — they stay
        # Russian until the chain library itself is localized.
        system=language_directive(),
        model=model_id,
        # Cap raised от 1000 → 2500: каждый шаг теперь требует ≥3 абзацев
        # (~250+ слов = ~400-600 токенов основного текста), плюс размышления
        # для reasoning-моделей. С прежним 1000 шаги обрезались посередине.
        max_output=2500,
    )
    return (text or "").strip()


async def run_chain(
    *,
    chain: list[StepDescription],
    chain_title: str,
    metrics: list[dict],
    model_id: str | None,
) -> AsyncGenerator[dict, None]:
    """Run one chain end-to-end. Yields events:

    - {"type": "chain_start", "title": str}
    - {"type": "step_start", "number": int, "title": str}
    - {"type": "step_text", "text": str}     — full step output (no token-level streaming yet)
    - {"type": "step_end", "number": int}
    - {"type": "chain_end"}

    Reasonable middle ground: each step is one LLM call, response shipped
    whole. Token-level streaming would require teaching the OpenRouter
    helper to yield deltas; not needed for the first cut.
    """
    # Translate METRIC_ENUM tokens in the title to human Russian names
    # (e.g. "Маржинальность по OPERATING_PROFIT" → "Маржинальность по
    # Операционная прибыль"). Same on each step title below — the LLM
    # still sees the raw enum in the prompt body, only the user-facing
    # heading is patched.
    yield {
        "type": "chain_start",
        "title": _translate_enums(localize_title(chain_title), metrics),
    }
    history_parts: list[str] = []
    marks = scaffold()
    for step in chain:
        # Build a per-step table — only the metrics this step actually queries.
        table_str = build_metrics_table_str(metrics, step.step_context_queries)
        yield {
            "type": "step_start",
            "number": step.number,
            # Localize first, substitute enums second — the lookup table is
            # keyed by the raw title, enum tokens and all.
            "title": _translate_enums(localize_title(step.title), metrics),
        }
        try:
            text = await _llm_step(
                table_str=table_str,
                step=step,
                history="\n\n".join(history_parts),
                model_id=model_id,
            )
        except Exception as e:
            text = t("chain.stepFailed", error=e)
        if text:
            # Translate enums in step text too — the LLM frequently echoes
            # back metric names in its prose ("маржа по EBITDA", "EBT…").
            text = _translate_enums(text, metrics)
            history_parts.append(
                f"{marks['step']} {step.number} ({step.title}): {text}"
            )
            yield {"type": "step_text", "text": text}
        yield {"type": "step_end", "number": step.number}
    yield {"type": "chain_end"}


async def run_full_synthesis(
    *,
    metrics: list[dict],
    model_id: str | None,
    chains: list[tuple[str, list[StepDescription], str]] | None = None,
) -> AsyncGenerator[dict, None]:
    """Run a curated set of chains and end with an executive-summary pass.

    Yields the same event shape as `run_chain`, plus:
    - {"type": "summary_start"} / {"type": "summary_text", "text": str} / {"type": "summary_end"}
    - {"type": "done"}
    """
    if chains is None:
        chains = STEP_CONFIG  # the full upstream lineup

    full_text_parts: list[str] = []
    for chain_key, chain, chain_title in chains:
        # Skip chains where none of the requested metrics are populated.
        all_queries: set[str] = set()
        for step in chain:
            all_queries.update(step.step_context_queries)
        any_present = any(
            (m.get("enum") in all_queries) and (m.get("value_current") is not None)
            for m in metrics
        )
        if not any_present:
            continue

        chain_text: list[str] = [f"## {chain_title}"]
        async for ev in run_chain(
            chain=chain, chain_title=chain_title, metrics=metrics, model_id=model_id
        ):
            yield ev
            if ev["type"] == "step_start":
                chain_text.append(f"\n### {ev['title']}\n")
            elif ev["type"] == "step_text":
                chain_text.append(ev["text"])
        full_text_parts.append("\n".join(chain_text))
        # Light backoff between chains to avoid OpenRouter rate-limit cascade.
        await asyncio.sleep(0.2)

    full_report = "\n\n".join(full_text_parts)
    metrics_table = build_metrics_table_str(metrics)

    if full_report.strip():
        yield {"type": "summary_start"}
        try:
            summary_prompt = executive_summary_prompt(full_report, metrics_table)
            text, _ = await llm.complete_with_usage(
                # ≥1500 слов на 9 разделов = ~2000-2500 токенов чистого текста.
                # Беру 5000 с запасом на reasoning-модели и нумерованные списки
                # в разделах «Позитивы»/«Риски». Был 1500 → отрубал ответ.
                prompt=summary_prompt,
                system=language_directive(),
                model=model_id,
                max_output=5000,
            )
            # Same enum→Russian translation as the chain steps so the
            # executive summary doesn't leak "TOTAL_LIABILITIES" /
            # "RIGHT_OF_USE_ASSETS" tokens into the user-facing text.
            yield {
                "type": "summary_text",
                "text": _translate_enums((text or "").strip(), metrics),
            }
        except Exception as e:
            yield {"type": "summary_text", "text": t("chain.summaryFailed", error=e)}
        yield {"type": "summary_end"}
    yield {"type": "done"}
