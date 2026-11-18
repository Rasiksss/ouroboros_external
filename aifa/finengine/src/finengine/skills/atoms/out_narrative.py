"""`out.narrative` — карточка 16b: краткий/развёрнутый текст ТОЛЬКО из уже
готовых `Judgment`/`Explanation`/`ExternalFactRef`, без повторного извлечения
или расчёта чисел.

**Заземление — не здесь, а в `verify.grounding` (mandatory_gates).** Этот
исполнитель ПАРСИТ ответ модели в `claims` СТРУКТУРНО (валидный `kind`,
типы полей), но НЕ отбрасывает утверждения без `evidence_id` — в отличие от
`assess_judgment`/`explain_internal`, где непроверяемый тезис тише не
публиковать вовсе. Здесь недостающая опора обязана дойти до `verify.
grounding` КАК ОТДЕЛЬНЫЙ узел графа и заблокировать текст целиком —
наблюдаемо (C12: «неподтверждённое числовое утверждение» — предмет
отдельного оповещения), а не молча вычищена на этом шаге.

**Нечего рассказывать.** Пустой `judgments`+`explanations` — явный пробел
(«недостаточно проверенных доказательств»), не текст, сочинённый по общим
соображениям."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from pydantic import ValidationError

from finengine.contracts import (
    EvidenceBundleV2,
    Explanation,
    ExternalFactRef,
    Judgment,
    NarrativeOutput,
    NumericClaimRecord,
    OutputRunMetadata,
)
from finengine.skills.atoms.verify_grounding import check_grounding

ATOM_VERSION = "1.0.0"

LlmJsonCall = Callable[[str, str], dict]

_ALLOWED_CLAIM_KINDS = {"financial", "date", "page_number", "other_id", "narrative"}


def _fact_view(fact, *, kind: str) -> dict:
    return {
        "evidence_id": fact.evidence_id, "concept": fact.concept, "period_ref": fact.period_ref,
        "value": fact.value, "unit": getattr(fact, "unit", None), "kind": kind,
    }


_SYSTEM_PROMPT = (
    "Ты пишешь финансовый нарратив СТРОГО на основе уже готовых суждений и "
    "объяснений — не извлекаешь и не считаешь числа заново. Правила: "
    "(1) каждое числовое финансовое утверждение текста обязано появиться "
    "также отдельной записью в claims с kind=\"financial\" и evidence_id "
    "источника; копируй evidence_id ТОЛЬКО из массива facts, никогда не "
    "используй judgment_id/explanation_id и не придумывай UUID; "
    "(2) даты/номера страниц/иные служебные числа помечай "
    "kind=\"date\"/\"page_number\"/\"other_id\" и evidence_id можно не "
    "указывать; (3) не давай инвестиционных, кредитных или иных рекомендаций; "
    "(4) при неполной доказательной базе явно укажи в тексте, чего не хватает, "
    "вместо уверенного вывода. "
    "Верни JSON вида {\"text\": str, \"claims\": [{\"text\": str, "
    "\"kind\": str, \"value\": number|null, \"period_ref\": str|null, "
    "\"unit\": str|null, \"evidence_id\": str|null}]}."
)


def _build_prompt(
    *, depth: str, judgments: list[Judgment], explanations: list[Explanation],
    bundle: EvidenceBundleV2 | None, external_facts: list[ExternalFactRef],
) -> str:
    bundle_view = []
    if bundle is not None:
        bundle_view = (
            [_fact_view(f, kind="extracted") for f in bundle.scalar_facts]
            + [_fact_view(f, kind="calculated") for f in bundle.derived_facts]
        )
    payload = {
        "depth": depth,
        "judgments": [j.model_dump() for j in judgments],
        "explanations": [e.model_dump() for e in explanations],
        "facts": bundle_view,
        "external_facts": [f.model_dump() for f in external_facts],
    }
    return "Составь нарратив по готовым суждениям/объяснениям ниже (JSON):\n" + json.dumps(
        payload, ensure_ascii=False,
    )


def _parse_claim(item: dict) -> NumericClaimRecord | None:
    if not isinstance(item, dict):
        return None
    kind = str(item.get("kind") or "").strip()
    if kind not in _ALLOWED_CLAIM_KINDS:
        return None
    text = str(item.get("text") or "").strip()
    if not text:
        return None
    value = item.get("value")
    try:
        value = float(value) if value is not None else None
    except (TypeError, ValueError):
        value = None
    evidence_id = item.get("evidence_id")
    evidence_id = str(evidence_id).strip() if isinstance(evidence_id, str) and evidence_id.strip() else None
    period_ref = item.get("period_ref")
    period_ref = str(period_ref) if isinstance(period_ref, str) and period_ref else None
    unit = item.get("unit")
    unit = str(unit) if isinstance(unit, str) and unit else None
    try:
        return NumericClaimRecord(text=text, kind=kind, value=value, period_ref=period_ref, unit=unit,
                                   evidence_id=evidence_id)
    except ValidationError:
        return None


def _repair_claim_reference(
    claim: NumericClaimRecord, bundle: EvidenceBundleV2 | None, *, allowed_evidence_ids: set[str],
) -> NumericClaimRecord:
    """Repair only an unambiguous model-side reference serialization error.

    The grounding gate remains authoritative.  A claim is rebound only when
    exactly one fact in the *current* bundle passes that gate's value/sign,
    period and unit checks.  Zero or multiple matches keep the model's ID so
    the mandatory gate blocks the narrative instead of guessing a source.
    """
    if claim.kind != "financial" or bundle is None:
        return claim
    facts = [
        fact for fact in (*bundle.scalar_facts, *bundle.derived_facts)
        if fact.evidence_id in allowed_evidence_ids
    ]
    by_id = {fact.evidence_id: fact for fact in facts}
    if claim.evidence_id in by_id and check_grounding(claim, by_id).status == "pass":
        return claim
    matches = []
    for fact in facts:
        candidate = claim.model_copy(update={"evidence_id": fact.evidence_id})
        if check_grounding(candidate, {fact.evidence_id: fact}).status == "pass":
            matches.append(fact.evidence_id)
    if len(matches) != 1:
        return claim
    return claim.model_copy(update={"evidence_id": matches[0]})


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


def out_narrative_executor(
    args, ctx, *, depth: str = "detailed", llm_json_call: LlmJsonCall | None = None,
) -> list[NarrativeOutput]:
    deps = ctx.dependencies()
    judgments = [e for e in deps if isinstance(e, Judgment)]
    explanations = [e for e in deps if isinstance(e, Explanation)]
    external_facts = [e for e in deps if isinstance(e, ExternalFactRef)]
    bundle = next((e for e in deps if isinstance(e, EvidenceBundleV2)), None)
    if not judgments and not explanations:
        # Явный пробел (карточка: «При неполной доказательной базе ограничения
        # явно отражаются в тексте») — здесь пробел настолько полон, что
        # текста не будет вовсе, а не текст ни о чём.
        return []

    prompt = _build_prompt(depth=depth, judgments=judgments, explanations=explanations,
                            bundle=bundle, external_facts=external_facts)
    call = llm_json_call or (lambda p, s: _default_llm_json_call(p, s, model_id=None))
    try:
        raw = call(prompt, _SYSTEM_PROMPT)
    except Exception:  # noqa: BLE001
        return []
    text = str((raw or {}).get("text") or "").strip()
    if not text:
        return []
    cited = sorted({eid for j in judgments for eid in j.supporting_evidence_ids} | {
        eid for e in explanations for eid in e.cited_evidence_ids
    })
    claims = [c for c in (_parse_claim(item) for item in (raw or {}).get("claims") or []) if c is not None]
    claims = [
        _repair_claim_reference(claim, bundle, allowed_evidence_ids=set(cited))
        for claim in claims
    ]
    return [NarrativeOutput(
        run_id=ctx.run_id,
        text=text,
        cited_evidence_ids=cited,
        claims=claims,
        run_metadata=OutputRunMetadata.from_passport(ctx.run_id, ctx.run_passport),
    )]


__all__ = ["ATOM_VERSION", "out_narrative_executor", "LlmJsonCall"]
