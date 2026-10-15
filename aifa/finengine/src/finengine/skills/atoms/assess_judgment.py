"""`assess.judgment` — карточка 16b: типизированное финансовое суждение по
проверенным фактам и методике, без арифметики и без новых чисел.

**Жёсткий входной контракт.** Модель получает ТОЛЬКО: (1) уже проверенные
факты концепта (`MetricEvidence`) с их `evidence_id`, (2) более широкий
`EvidenceBundleV2` того же прогона как контекст сопоставления, (3) текст
методики. Промпт прямо запрещает арифметику и новые числа; парсер ниже —
вторая линия защиты (не полагается на дисциплину модели): `supporting_
evidence_ids`, не резолвящиеся ни в один `evidence_id` контекста, отбрасываются
из суждения, а не проходят как есть (см. `_parse_judgments`).

**Пакетная экономика.** Один физический вызов модели на ВЕСЬ пакет концептов
одного прогона (`assess_judgment_batch`) — тот же принцип, что `locate`/
`read.value`/`calc.formula`: логическая атомизация по концепту не должна
означать сто отдельных модельных вызовов (C9 методики).

**Недостаток доказательств.** Концепт без единого факта в пакете просто не
подаётся модели и не получает суждения — пустой результат, не выдуманный
вердикт (тот же класс исхода, что «показатель не нашёлся», см. `calc_formula.
py`)."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence

from pydantic import ValidationError

from finengine.contracts import EvidenceBundleV2, Judgment, RunPassport
from finengine.skills.spec.atom_registry import NodeArgs
from finengine.skills.spec.target import InputSpec

ATOM_VERSION = "1.0.0"

LlmJsonCall = Callable[[str, str], dict]


def assess_judgment_requires(node_args: NodeArgs, run_passport: RunPassport | None) -> tuple[InputSpec, ...]:
    if run_passport is None or not node_args.concept:
        return ()
    return (
        InputSpec(input_type="MetricEvidence", concept=node_args.concept),
        InputSpec(input_type="EvidenceBundleV2"),
    )


def _fact_view(fact, *, kind: str) -> dict:
    return {
        "evidence_id": fact.evidence_id, "concept": fact.concept, "period_ref": fact.period_ref,
        "value": fact.value, "unit": getattr(fact, "unit", None), "kind": kind,
    }


def _bundle_view(bundle: EvidenceBundleV2) -> list[dict]:
    return (
        [_fact_view(f, kind="extracted") for f in bundle.scalar_facts]
        + [_fact_view(f, kind="calculated") for f in bundle.derived_facts]
    )


_SYSTEM_PROMPT = (
    "Ты формируешь типизированное финансовое суждение СТРОГО по предоставленным "
    "фактам и методике. Правила: (1) не выполняй арифметику и не выводи новых "
    "числовых значений — используй только value/unit из фактов как есть; "
    "(2) каждое суждение обязано ссылаться на evidence_id хотя бы одного "
    "предоставленного факта того же концепта; (3) при недостатке фактов для "
    "концепта сформулируй ограничение анализа в verdict, а не уверенный вывод; "
    "(4) не давай инвестиционных или иных рекомендаций. "
    "Верни JSON вида {\"judgments\": [{\"concept\": str, \"verdict\": str, "
    "\"supporting_evidence_ids\": [str, ...]}]}."
)


def _build_prompt(concepts: Sequence[str], bundle_view: list[dict], methodology_text: str) -> str:
    payload = {
        "concepts_to_assess": list(concepts),
        "facts": bundle_view,
        "methodology": methodology_text[:8000],
    }
    return "Оцени перечисленные концепты по методике и фактам ниже (JSON):\n" + json.dumps(
        payload, ensure_ascii=False,
    )


def _as_id_list(value: object) -> list:
    """Реальная модель не держит форму `supporting_evidence_ids`: на dev она
    возвращала число, JSON-строку со списком и обрывок сломанного JSON. Форму
    поля чиним здесь, а существование самих идентификаторов по-прежнему
    проверяет `_parse_judgments` — выдуманная опора не проходит ни в одном
    варианте."""
    if isinstance(value, str):
        text = value.strip()
        if not text.startswith("["):
            return [text] if text else []
        try:
            value = json.loads(text)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def _parse_judgments(
    raw: dict, *, concepts: Sequence[str], evidence_ids: set[str], methodology_ref: str,
) -> dict[str, list[Judgment]]:
    result: dict[str, list[Judgment]] = {c: [] for c in concepts}
    wanted = set(concepts)
    judgments = raw.get("judgments") if isinstance(raw, dict) else None
    for item in (judgments if isinstance(judgments, list) else []):
        if not isinstance(item, dict):
            continue
        concept = str(item.get("concept") or "").strip().upper()
        if concept not in wanted:
            continue
        supporting_raw = _as_id_list(item.get("supporting_evidence_ids"))
        supporting = [e for e in supporting_raw if isinstance(e, str) and e in evidence_ids]
        if not supporting:
            # Инвариант карточки: «все числовые основания суждения ссылаются
            # на существующие проверенные доказательства» — суждение без
            # уцелевшей опоры не публикуется, а не публикуется без опоры.
            continue
        try:
            judgment = Judgment(
                concept=concept, verdict=str(item.get("verdict") or "").strip() or "нет вывода",
                supporting_evidence_ids=supporting, methodology_ref=methodology_ref,
            )
        except ValidationError:
            continue
        result.setdefault(concept, []).append(judgment)
    return result


def _default_llm_json_call(prompt: str, system: str, *, model_id: str | None) -> dict:
    from finengine.llm import openrouter as llm  # noqa: PLC0415

    async def _call() -> dict:
        raw, _usage = await llm.complete_with_usage(
            prompt=prompt, system=system, model=model_id, response_format={"type": "json_object"},
        )
        try:
            return json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}

    return asyncio.run(_call())


def assess_judgment_batch(
    concepts: Sequence[str],
    *,
    bundle: EvidenceBundleV2,
    methodology_text: str,
    methodology_ref: str = "unversioned",
    model_id: str | None = None,
    llm_json_call: LlmJsonCall | None = None,
) -> dict[str, list[Judgment]]:
    """Один физический вызов модели на весь `concepts`. Концепт без фактов в
    `bundle` не подаётся модели и молча получает пустой список — та же
    семантика, что `read.value`/`calc.formula` для ненайденного показателя."""
    bundle_view = _bundle_view(bundle)
    present = {row["concept"] for row in bundle_view}
    askable = [c for c in concepts if c in present]
    result: dict[str, list[Judgment]] = {c: [] for c in concepts}
    if not askable:
        return result

    evidence_ids = {row["evidence_id"] for row in bundle_view}
    prompt = _build_prompt(askable, bundle_view, methodology_text)
    call = llm_json_call or (lambda p, s: _default_llm_json_call(p, s, model_id=model_id))
    try:
        raw = call(prompt, _SYSTEM_PROMPT)
    except Exception:  # noqa: BLE001 — модельный сбой не должен ронять пакет соседних концептов
        return result
    parsed = _parse_judgments(raw or {}, concepts=askable, evidence_ids=evidence_ids, methodology_ref=methodology_ref)
    result.update(parsed)
    return result


__all__ = [
    "ATOM_VERSION", "assess_judgment_requires", "assess_judgment_batch", "LlmJsonCall",
]
