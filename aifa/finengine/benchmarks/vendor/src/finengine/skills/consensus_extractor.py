"""Triumvirat consensus extractor — three-LLM debate over each metric.

Pipeline per metric:
  1. Round 1: call Qwen 3.6 Plus, Kimi K2.5, DeepSeek V4 Pro in parallel.
     Each returns a strict-JSON `{value_current, value_prev, unit, page,
     reasoning}` for one metric.
  2. Bucket the three answers by 5%-relative tolerance (or ±0.1 pp absolute
     for percentage-unit metrics). `null` is its own bucket; null + two
     numbers is treated as a 3-way split, NOT a 2-of-3 majority — the user
     wants debate to happen there because the disagreement is qualitative
     ("is it in the report at all?").
  3. If all 3 agree → record the value. If 2 of 3 agree (both numeric, both
     within tolerance) → record the majority value, drop the dissenter.
  4. Otherwise Round 2 debate: re-prompt all three with the round-1 peer
     answers visible (anonymized A/B/C, including each model's own answer
     so the prompt is symmetric and identical across the three calls →
     prompt-cache friendly). Re-bucket.
  5. If still no majority after Round 2 → record null with `consensus_flag`.
     Never invent a number — better empty than wrong.

The arch routes through a single `consensus` grant: a user with the grant
can use this mode regardless of whether they have standalone Kimi/DeepSeek
access. The premium models are NOT exposed individually — they only fire
inside this dispatcher.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncGenerator, Optional

from finengine.config import settings
from finengine.llm import openrouter
from finengine.skills.bulk_extractor import (
    CALCULATED_BANK,
    CALCULATED_NON_FINANCIAL,
    EXTRACTABLE_BANK,
    EXTRACTABLE_NON_FINANCIAL,
    _calculate_metrics_pair,
    _detect_company_from_text,
    _detect_company_type,
    _detect_year_from_text,
    _normalise_money_value,
    _reconcile_company_name,
    _wait_for_pdf_text,
)


TRIUMVIRAT_MODELS: tuple[str, str, str] = (
    "qwen/qwen3.6-plus-04-02",
    "moonshotai/kimi-k2.5",
    # 2026-05-03: Switched from V4 Pro → V3.2. V4 Pro routed to expensive
    # providers (~$1.40/M input vs catalog $0.435/M, ~3-4× markup) and
    # generated thousands of reasoning_tokens per call. V3.2 is much cheaper
    # ($0.27-style pricing tier) and is a hybrid model — reasoning OFF by
    # default, switches ON when `reasoning: {effort: medium}` is passed.
    # Triumvirat passes effort="medium" explicitly so V3.2 reasons too.
    "deepseek/deepseek-v3.2",
)
TRIUMVIRAT_LABELS = ("Qwen", "Kimi", "DeepSeek")

# Triumvirat input size cap: limited by the narrowest model's context window.
# DeepSeek V3.2 = 164K. We reserve headroom for system prompt + per-metric
# info block + JSON schema + output budget (4000 tokens). 164K - 14K = 150K
# safe upper bound on paginated_text. Above this the Kuibyshev-style 100%
# DeepSeek failure happens. Above 256K Kimi also fails. For PDFs above 150K
# tokens we route the user to Finengine DEV (Qwen 1M ctx, single-shot).
TRIUMVIRAT_MAX_INPUT_TOKENS = 150_000

# 5% relative for absolutes (matches validator). 0.1 pp absolute for
# percentage units (CET1, NIM, Tier1) — user said 5% relative is too loose
# for ratios.
AGREEMENT_TOLERANCE_PCT = 0.05
PCT_TOLERANCE_ABSOLUTE = 0.1


@dataclass
class ModelAnswer:
    model_id: str
    label: str
    value_current: Optional[float]
    value_prev: Optional[float]
    unit: str
    page: Optional[int]
    reasoning: str
    raw_text: str
    usage: dict
    error: Optional[str] = None


@dataclass
class ConsensusOutcome:
    value_current: Optional[float]
    value_prev: Optional[float]
    unit: str
    page: Optional[int]
    status: str  # unanimous | majority_2of3 | unanimous_after_debate | majority_after_debate | consensus_failed
    rounds: int
    consensus_flag: bool  # True when status == consensus_failed
    dissenter_labels: list[str] = field(default_factory=list)


def _is_pct_metric(unit: str | None) -> bool:
    if not unit:
        return False
    u = unit.strip().lower()
    return u in {"%", "пп", "п.п.", "процент", "ratio", "%."}


def _within_tolerance(a: Optional[float], b: Optional[float], is_pct: bool) -> bool:
    """Two values are 'agreeing'. None == None counts as agreement; None
    vs number does NOT — that disagreement is qualitative and triggers
    debate."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if is_pct:
        return abs(a - b) <= PCT_TOLERANCE_ABSOLUTE
    if a == 0 and b == 0:
        return True
    denom = max(abs(a), abs(b))
    if denom == 0:
        return True
    return abs(a - b) / denom <= AGREEMENT_TOLERANCE_PCT


def _coerce_num(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
        try:
            return float(s)
        except Exception:
            return None
    return None


def _coerce_page(v) -> Optional[int]:
    try:
        n = int(v) if v is not None else None
    except Exception:
        return None
    if n is None or n <= 0:
        return None
    return n


def _parse_one(text: str) -> dict:
    """Lift the strict-JSON object out of the model output. Extra-tolerant —
    accepts optional ```json fences and partial trailing commas."""
    if not text:
        return {}
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:].strip()
    # Find the first { and last } in case the model added prose
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end < start:
        return {}
    payload = s[start : end + 1]
    try:
        return json.loads(payload)
    except Exception:
        return {}


def _bucket_answers(answers: list[ModelAnswer], is_pct: bool) -> dict:
    """Group the three answers by `value_current` agreement. Returns a dict
    {verdict, value_current, value_prev, unit, page, dissenters}.

    verdict ∈ {"unanimous","majority","split"}.

    "majority" requires:
      - exactly 2 of 3 within tolerance on `value_current`, AND
      - both winners are numeric (None is qualitative — not majority-eligible),
      - their `value_prev` either both numeric within tolerance, or both None.

    Returns the AVERAGED value (mean of the two winning numbers) for tighter
    final value than picking just one.
    """
    if not answers or all(a.error for a in answers):
        return {"verdict": "split", "dissenters": [a.label for a in answers]}

    # Pairwise compatibility on value_current
    pairs = []
    for i in range(len(answers)):
        for j in range(i + 1, len(answers)):
            if answers[i].error or answers[j].error:
                continue
            if _within_tolerance(answers[i].value_current, answers[j].value_current, is_pct):
                pairs.append((i, j))

    # Unanimous: all 3 mutually agree
    if len(pairs) == 3:
        # Use the median — robust to one outlier in case of float jitter
        nums = [a.value_current for a in answers if a.value_current is not None]
        prevs = [a.value_prev for a in answers if a.value_prev is not None]
        units = [a.unit for a in answers if a.unit]
        pages = [a.page for a in answers if a.page is not None]
        return {
            "verdict": "unanimous",
            "value_current": (sum(nums) / len(nums)) if nums else None,
            "value_prev": (sum(prevs) / len(prevs)) if prevs else None,
            "unit": units[0] if units else "",
            "page": pages[0] if pages else None,
            "dissenters": [],
        }

    # Majority: exactly one pair forms a 2-of-3 cluster, both numeric
    for i, j in pairs:
        a, b = answers[i], answers[j]
        # Numeric requirement — pair must be both numeric.
        if a.value_current is None or b.value_current is None:
            continue
        third_idx = next(k for k in range(len(answers)) if k != i and k != j)
        third = answers[third_idx]
        # User spec: a third model returning null = qualitative disagreement
        # ("is this metric in the report at all?") → goes to debate, NOT
        # majority. An errored third "abstains" and the pair wins.
        if third.value_current is None and not third.error:
            continue
        nums = [a.value_current, b.value_current]
        prevs = [x for x in (a.value_prev, b.value_prev) if x is not None]
        units = [x for x in (a.unit, b.unit) if x]
        pages = [x for x in (a.page, b.page) if x is not None]
        return {
            "verdict": "majority",
            "value_current": sum(nums) / len(nums),
            "value_prev": (sum(prevs) / len(prevs)) if prevs else None,
            "unit": units[0] if units else "",
            "page": pages[0] if pages else None,
            "dissenters": [third.label],
        }

    # No agreement
    return {
        "verdict": "split",
        "dissenters": [a.label for a in answers],
    }


def _build_per_metric_user_prompt(
    metric: dict,
    paginated_text: str,
    period_current: str,
    period_prev: str,
    has_pages: bool,
    peer_answers: Optional[list[ModelAnswer]] = None,
) -> str:
    """Build the user-side prompt for one metric. When `peer_answers` is set
    we're in round 2 — append the three round-1 answers anonymized A/B/C."""
    block = [f"### {metric['enum']}: {metric['name_ru']}"]
    if metric.get("name_en"):
        block.append(f"  (англ.: {metric['name_en']})")
    aliases = []
    for a in (metric.get("aliases_ru") or []) + (metric.get("aliases_en") or []):
        if a and a != "Нет":
            aliases.append(a)
    if aliases:
        block.append(f"  Также может называться: {'; '.join(aliases[:6])}")
    if metric.get("description"):
        block.append(f"  Описание: {metric['description']}")
    if metric.get("hint"):
        block.append(f"  Где искать: {metric['hint']}")
    info_block = "\n".join(block)

    # 2026-05-03: removed `reasoning` field from JSON schema. The model
    # already thinks via the native reasoning channel (effort=medium); a
    # text-comment field in the JSON output was redundant — it ate ~50
    # tokens per call, was never read by downstream code, and may have
    # diverted attention from value accuracy.
    schema = (
        "{\n"
        '  "value_current": <число или null>,\n'
        '  "value_prev": <число или null>,\n'
        '  "unit": "<единица: млн руб. / тыс руб. / млрд руб. / % / руб.>",\n'
        '  "page": <номер страницы или null>\n'
        "}"
        if has_pages
        else
        "{\n"
        '  "value_current": <число или null>,\n'
        '  "value_prev": <число или null>,\n'
        '  "unit": "<единица>"\n'
        "}"
    )

    parts = [
        f"# Полный текст отчёта\n\n{paginated_text}",
        f"\n\n# Метрика для извлечения\n\n"
        f"Период текущий = **{period_current}**, предыдущий = **{period_prev}**.\n\n{info_block}",
    ]

    if peer_answers:
        peer_lines = ["\n\n# Round-2 контекст: ответы трёх независимых экстракторов в раунде 1"]
        peer_lines.append(
            "Три экстрактора (анонимизированы как A/B/C) дали разные ответы. "
            "Перепроверь источник внимательно. Можешь оставить свой ответ или "
            "изменить — но НЕ усредняй: выбери одно из трёх значений или null. "
            "Если уверенности всё ещё нет — поставь null. Лучше пусто, чем неверно."
        )
        for label, ans in zip(("A", "B", "C"), peer_answers):
            if ans.error:
                peer_lines.append(f"  {label}: ОШИБКА — {ans.error}")
                continue
            peer_lines.append(
                f"  {label}: value_current={ans.value_current}, value_prev={ans.value_prev}, "
                f"unit={ans.unit!r}, page={ans.page}, "
                f'reasoning="{(ans.reasoning or "").strip()[:300]}"'
            )
        parts.append("\n".join(peer_lines))

    parts.append(
        "\n\n# Алгоритм работы (выполняй мысленно)\n"
        "1. Прочитай 'Где искать' и найди соответствующий раздел в отчёте.\n"
        "2. Найди строку, заголовок которой совпадает с названием метрики или одним из синонимов.\n"
        "3. Сверь содержание с описанием — ту ли метрику ты нашёл.\n"
        "4. Возьми числовые значения для текущего и предыдущего периодов.\n"
        "5. Если строки нет — value_current=null. Если строка есть, но значения нет — для этого периода null.\n"
        "6. **Если есть сомнения, что это именно та метрика — null. Лучше пусто, чем неверно.**"
    )
    parts.append(f"\n\n# Формат ответа\n\nРовно один JSON-объект по схеме:\n\n{schema}")
    return "\n".join(parts)


SYSTEM_PROMPT_PER_METRIC = (
    "Ты опытный финансовый аналитик. Извлекаешь ОДНУ метрику из IFRS/RSBU "
    "отчёта точно как в первоисточнике — числа без пробелов и валютных символов.\n\n"
    "## Жёсткие правила\n"
    "1. **Лучше null, чем неверное число.** Это критично — твой ответ голосует "
    "наряду с двумя другими экстракторами; ложное значение хуже пропуска.\n"
    "2. Не путай похожие строки: «Себестоимость продаж» ≠ «Операционные расходы»; "
    "«Финансовые доходы» ≠ «Процентные доходы банка».\n"
    "3. Числа в **млн руб.** по умолчанию. Если в отчёте тыс. руб. — раздели на 1000. "
    "Если в млрд — умножь на 1000. В поле unit укажи фактическую единицу из отчёта.\n"
    "4. Прибыль/доходы — положительное. Убыток — отрицательное. Скобки `(1 234)` = "
    "отрицательное. Расходы и резервы — положительное число абсолютной величины.\n"
    "5. EPS — единственная метрика в рублях на акцию (unit='руб.').\n\n"
    "Возвращай ровно один валидный JSON-объект. Никаких пояснений, markdown-обёрток, "
    "комментариев — только JSON."
)


async def _call_one(
    model: str,
    label: str,
    user_prompt: str,
    seed: int,
) -> ModelAnswer:
    """One LLM call for one metric. Wrapped to never raise — errors return
    a ModelAnswer with `error` set so the caller can still bucket the
    other two."""
    try:
        text, usage = await openrouter.complete_with_usage(
            prompt=user_prompt,
            system=SYSTEM_PROMPT_PER_METRIC,
            model=model,
            response_format={"type": "json_object"},
            # 2026-05-03 update: bumped from 800 → 4000 because we now keep
            # reasoning ON (medium). Reasoning tokens are counted as part of
            # completion budget, so 800 was getting fully consumed by the
            # model's internal thinking before any JSON came out. 4000 leaves
            # room for ~2-3K reasoning + actual JSON answer.
            max_output=4000,
            seed=seed,
            temperature=0.0,
            # Reasoning ON at medium effort. Per user: "если выключаю,
            # модель перестаёт размышлять, качество извлечения метрик
            # становится хуже". Quality > token economy.
            # Per-chat override: if user picks low/high in the chat header
            # dropdown, that flows through the request contextvar and wins
            # over this default.
            reasoning_effort="medium",
        )
    except Exception as e:
        return ModelAnswer(
            model_id=model, label=label,
            value_current=None, value_prev=None, unit="", page=None,
            reasoning="", raw_text="", usage={"in": 0, "out": 0, "cached": 0, "model": model},
            error=str(e)[:200],
        )

    parsed = _parse_one(text)
    v_cur = _coerce_num(parsed.get("value_current"))
    v_prv = _coerce_num(parsed.get("value_prev"))
    raw_unit = (parsed.get("unit") or "").strip() or "млн. руб."
    # Normalise to млн руб. so all three answers are on the same scale
    # before bucketing — otherwise тыс vs млн would always disagree.
    v_cur_n, norm_unit = _normalise_money_value(v_cur, raw_unit)
    v_prv_n, _ = _normalise_money_value(v_prv, raw_unit)

    return ModelAnswer(
        model_id=model, label=label,
        value_current=v_cur_n,
        value_prev=v_prv_n,
        unit=norm_unit or raw_unit,
        page=_coerce_page(parsed.get("page")),
        reasoning=str(parsed.get("reasoning") or "")[:500],
        raw_text=text[:2000],
        usage=usage or {"in": 0, "out": 0, "cached": 0, "model": model},
    )


async def _consensus_one_metric(
    metric: dict,
    paginated_text: str,
    period_current: str,
    period_prev: str,
    has_pages: bool,
    doc_id: str,
) -> tuple[ConsensusOutcome, list[ModelAnswer], list[ModelAnswer]]:
    """Run the full per-metric consensus flow. Returns (outcome, round1, round2)."""
    from finengine.llm.tracer import update_trace_extra

    is_pct = _is_pct_metric(metric.get("unit") or metric.get("default_unit"))

    user_prompt_r1 = _build_per_metric_user_prompt(
        metric, paginated_text, period_current, period_prev, has_pages
    )

    # Per-metric deterministic seed — same metric same doc same model gets
    # the same seed → reproducible across re-runs.
    seeds = [hash((doc_id, metric["enum"], m)) & 0x7fffffff for m in TRIUMVIRAT_MODELS]
    update_trace_extra(metric_enum=metric["enum"], round=1)
    r1 = await asyncio.gather(
        *(
            _call_one(model, label, user_prompt_r1, seed)
            for model, label, seed in zip(TRIUMVIRAT_MODELS, TRIUMVIRAT_LABELS, seeds)
        )
    )

    bucket = _bucket_answers(r1, is_pct)
    if bucket["verdict"] in ("unanimous", "majority"):
        status = "unanimous" if bucket["verdict"] == "unanimous" else "majority_2of3"
        return (
            ConsensusOutcome(
                value_current=bucket.get("value_current"),
                value_prev=bucket.get("value_prev"),
                unit=bucket.get("unit") or "млн. руб.",
                page=bucket.get("page"),
                status=status,
                rounds=1,
                consensus_flag=False,
                dissenter_labels=bucket.get("dissenters", []),
            ),
            r1,
            [],
        )

    # Round 2 — debate. Each model sees all three round-1 answers anonymized.
    user_prompt_r2 = _build_per_metric_user_prompt(
        metric, paginated_text, period_current, period_prev, has_pages, peer_answers=r1
    )
    update_trace_extra(metric_enum=metric["enum"], round=2)
    r2 = await asyncio.gather(
        *(
            _call_one(model, label, user_prompt_r2, seed + 1)
            for model, label, seed in zip(TRIUMVIRAT_MODELS, TRIUMVIRAT_LABELS, seeds)
        )
    )
    bucket2 = _bucket_answers(r2, is_pct)
    if bucket2["verdict"] in ("unanimous", "majority"):
        status = "unanimous_after_debate" if bucket2["verdict"] == "unanimous" else "majority_after_debate"
        return (
            ConsensusOutcome(
                value_current=bucket2.get("value_current"),
                value_prev=bucket2.get("value_prev"),
                unit=bucket2.get("unit") or "млн. руб.",
                page=bucket2.get("page"),
                status=status,
                rounds=2,
                consensus_flag=False,
                dissenter_labels=bucket2.get("dissenters", []),
            ),
            r1,
            r2,
        )

    return (
        ConsensusOutcome(
            value_current=None,
            value_prev=None,
            unit="млн. руб.",
            page=None,
            status="consensus_failed",
            rounds=2,
            consensus_flag=True,
            dissenter_labels=[a.label for a in r2],
        ),
        r1,
        r2,
    )


async def extract_stream_consensus(
    doc_id: str,
    company_type: str,
    *,
    index=None,
    limit: Optional[int] = None,
    model_id: Optional[str] = None,  # ignored — Triumvirat hardcodes its 3 models
    parallel: int = 4,
) -> AsyncGenerator[dict, None]:
    """Triumvirat (3-model consensus) extract dispatcher. Public entry point
    matching the standard arch contract used by api/main.py."""
    from finengine.skills.aifa_baseline.extractor import filter_to_trained
    from finengine.skills.pricing import aggregate_usage_to_diag

    if index is None:
        from finengine.ingestion.indexer import VectorIndex
        index = VectorIndex()

    doc = index.get_document(doc_id)
    if not doc:
        yield {"type": "error", "message": f"Document {doc_id} not found"}
        return

    txt_path = Path(settings.docs_dir) / f"{doc_id}.txt"
    if not txt_path.exists():
        if not await _wait_for_pdf_text(txt_path):
            yield {
                "type": "error",
                "message": (
                    "Документ ещё парсится (превышено окно ожидания 15 минут). "
                    "Большие отчёты (100+ страниц) могут парситься дольше — "
                    "подождите 1-2 минуты и нажмите «Извлечь» снова. "
                    "Повторно загружать документ не нужно."
                ),
            }
            return
    pdf_text = txt_path.read_text(encoding="utf-8")

    # Paginated text from FAISS chunks (same as full_context).
    by_page: dict[int, list[str]] = {}
    for kind in ("text", "financial_table", "table"):
        try:
            chunks_p = index.get_chunks_by_type(kind, doc_id=doc_id)
        except Exception:
            chunks_p = []
        for c in chunks_p:
            p = c.metadata.get("page", 0) if hasattr(c, "metadata") else 0
            try:
                p = int(p or 0)
            except Exception:
                p = 0
            if p > 0:
                by_page.setdefault(p, []).append(c.content or "")
    paginated_text = ""
    if by_page:
        parts = []
        for p in sorted(by_page.keys()):
            parts.append(f"===PAGE {p}===\n" + "\n\n".join(by_page[p]))
        paginated_text = "\n\n".join(parts)
    has_pages = bool(paginated_text)
    pdf_text_for_prompt = paginated_text or pdf_text

    # Input size guard via tiktoken (cl100k_base — OpenAI baseline encoder).
    # Different models have slightly different tokenisers (DeepSeek/Qwen/Kimi
    # use their own BPE), but cl100k typically estimates within ±10% — close
    # enough for guard purposes. Earlier we used `len // 3` which severely
    # underestimated Russian text and the Kuibyshev run silently overflowed
    # DeepSeek V3.2's 164K context (real ≈226K, my heuristic said 77K).
    try:
        import tiktoken
        _enc = tiktoken.get_encoding("cl100k_base")
        approx_tokens = len(_enc.encode(pdf_text_for_prompt))
    except Exception:
        # Fallback if tiktoken unavailable — conservative estimate (1 token ≈
        # 1.5 chars for Russian-heavy IFRS reports, much safer than //3).
        approx_tokens = int(len(pdf_text_for_prompt) / 1.5)

    if approx_tokens > TRIUMVIRAT_MAX_INPUT_TOKENS:
        yield {
            "type": "error",
            "message": (
                f"Triumvirat пока не поддерживает документы крупнее ~{TRIUMVIRAT_MAX_INPUT_TOKENS // 1000}K токенов "
                f"(этот документ ≈{approx_tokens // 1000}K). Kimi K2.5 (256K контекст) и DeepSeek V3.2 (164K) "
                f"не вмещают полный текст. Для крупных банков используйте Finengine DEV — "
                f"Qwen 3.6 Plus 04-02 имеет окно 1M токенов и обрабатывает Сбер/ВТБ/ВЭБ целиком."
            ),
        }
        return

    effective_type = company_type
    if company_type in ("unknown", "auto", ""):
        effective_type = _detect_company_type(pdf_text)

    if effective_type == "bank":
        extractable = EXTRACTABLE_BANK
        calculated_defs = CALCULATED_BANK
    else:
        extractable = filter_to_trained(EXTRACTABLE_NON_FINANCIAL)
        calculated_defs = CALCULATED_NON_FINANCIAL

    if limit is not None and limit > 0:
        extractable = extractable[:limit]
        calculated_defs = []

    total_metrics = len(extractable) + len(calculated_defs)
    period_current = doc.period or _detect_year_from_text(pdf_text) or "Текущий"
    period_prev = (str(int(period_current) - 1) if period_current.isdigit() else "Предыдущий")

    company_display = doc.company_name or _detect_company_from_text(pdf_text) or ""
    company_display = _reconcile_company_name(company_display, doc.filename)

    yield {
        "type": "start",
        "total_metrics": total_metrics,
        "total_extractable": len(extractable),
        "company_type": effective_type,
        "company_name": company_display,
        "unit": "млн. руб.",
        "period_current": period_current,
        "period_prev": period_prev,
    }

    extracted_current: dict[str, Optional[float]] = {}
    extracted_prev: dict[str, Optional[float]] = {}
    diag: dict = {
        "pdf_text_chars": len(pdf_text),
        "metrics_total": len(extractable),
        "rounds_distribution": {"unanimous": 0, "majority_2of3": 0,
                                 "unanimous_after_debate": 0, "majority_after_debate": 0,
                                 "consensus_failed": 0},
        "calls_total": 0,
        "calls_failed": 0,
    }
    usage_sink: dict = {"in": 0, "out": 0, "cached": 0, "calls": 0}

    sem = asyncio.Semaphore(max(1, int(parallel or 4)))
    t0 = time.time()

    # Run all metrics concurrently with a semaphore cap, stream results
    # as they finish (asyncio.as_completed preserves the same yield-as-done
    # behaviour the UI expects).
    async def _wrapped(m):
        async with sem:
            return m, await _consensus_one_metric(
                m, pdf_text_for_prompt, period_current, period_prev,
                has_pages, doc_id,
            )

    tasks = [asyncio.create_task(_wrapped(m)) for m in extractable]
    for fut in asyncio.as_completed(tasks):
        try:
            metric, (outcome, r1, r2) = await fut
        except Exception as e:
            # Whole-metric exception — log + skip. Should be rare since
            # _call_one already swallows per-call errors.
            yield {"type": "error", "message": f"Metric loop crashed: {e}"}
            continue

        diag["rounds_distribution"][outcome.status] = (
            diag["rounds_distribution"].get(outcome.status, 0) + 1
        )
        for ans in (*r1, *r2):
            usage_sink["in"] += int(ans.usage.get("in", 0) or 0)
            usage_sink["out"] += int(ans.usage.get("out", 0) or 0)
            usage_sink["cached"] += int(ans.usage.get("cached", 0) or 0)
            usage_sink["calls"] += 1
            diag["calls_total"] += 1
            if ans.error:
                diag["calls_failed"] += 1

        extracted_current[metric["enum"]] = outcome.value_current
        extracted_prev[metric["enum"]] = outcome.value_prev

        yield {
            "type": "metric",
            "enum": metric["enum"],
            "name_ru": metric["name_ru"],
            "value_current": outcome.value_current,
            "value_prev": outcome.value_prev,
            "unit": outcome.unit or "млн. руб.",
            "found": outcome.value_current is not None,
            "is_calculated": False,
            "formula": "",
            "page": outcome.page,
            "consensus_status": outcome.status,
            "consensus_flag": outcome.consensus_flag,
            "consensus_rounds": outcome.rounds,
            "consensus_dissenters": outcome.dissenter_labels,
        }

    # Calculated metrics — derive from the consensus values.
    calc_current, calc_prev = _calculate_metrics_pair(
        calculated_defs,
        extracted_current,
        extracted_prev,
    )
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

    diag["wall_s"] = round(time.time() - t0, 1)
    aggregate_usage_to_diag(usage_sink, diag, "triumvirat")

    yield {
        "type": "done",
        "period_current": period_current,
        "period_prev": period_prev,
        "unit": "млн. руб.",
        "company_type": effective_type,
        "company_name": company_display or doc.company_name,
        "_diag": diag,
    }
