"""`id.revision` — карточка 18 (Срез 8, B9, docs/build-plan/
18-srez-08-struktury-i-neskolko-dokumentov.md): определить пересмотр/
переклассификацию сравнительных данных только с доказательством, не смешивая
исходную и исправленную версии.

**Два прохода, как требует карточка.** Сначала детерминированный поиск
маркера пересмотра РЯДОМ с контекстом сравнительного периода (`find_revision_
markers`) — маркер без такого контекста не идёт дальше вовсе (краевой случай
карточки: «нельзя объявлять весь документ пересмотренным только из-за одного
слова в отдалённом примечании»). Только если маркер найден, модель под жёсткой
JSON-схемой решает, ДЕЙСТВИТЕЛЬНО ли это пересмотр, какого рода (`restated`/
`reissued`) и какой из уже известных документов-кандидатов он заменяет —
парсер (вторая линия защиты, не полагается на дисциплину модели) отбрасывает
`supersedes_document_id`, не входящий в предложенный список кандидатов, и
любой `revision_kind` вне закрытого перечисления.

**Документы без доказанного отношения не связываются эвристически** — при
пустом результате детерминированного или модельного прохода атом возвращает
`None`, не «по умолчанию original» и не «предположительно restated»."""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from finengine.contracts import RevisionInfo

ATOM_VERSION = "1.0.0"

LlmJsonCall = Callable[[str, str], dict]

# RU + EN, словарь пересмотра/переиздания/переклассификации отчётности.
_MARKER_RE = re.compile(
    r"\b("
    r"пересчитан\w*|пересмотрен\w*|переклассифицирован\w*|скорректирован\w*|исправлен\w*"
    r"|restat(?:ed|ement)\w*|reissu(?:ed|ance)\w*|reclassifi(?:ed|cation)\w*"
    r")\b",
    re.IGNORECASE,
)
# Контекст, без которого маркер — «одно слово в отдалённом примечании», не
# доказательство пересмотра сравнительных данных.
_COMPARATIVE_CONTEXT_RE = re.compile(
    r"\b(сравнительн\w*|предыдущ\w*\s+период\w*|прошл\w*\s+год\w*"
    r"|comparative\w*|prior[\s-]period\w*|prior[\s-]year\w*)\b",
    re.IGNORECASE,
)

_WINDOW = 200  # символов контекста по обе стороны маркера, отдаётся модели как есть


@dataclass(frozen=True)
class RevisionMarkerHit:
    """Один найденный маркер вместе с окном текста вокруг него — то, что
    реально видит модель на втором проходе, не весь документ целиком."""

    marker: str
    context: str
    start: int


def find_revision_markers(text: str) -> list[RevisionMarkerHit]:
    """Детерминированный первый проход (карточка: «сначала детерминированный
    поиск маркеров пересмотра рядом с заголовками и примечаниями»)."""
    hits: list[RevisionMarkerHit] = []
    for m in _MARKER_RE.finditer(text or ""):
        start = max(0, m.start() - _WINDOW)
        end = min(len(text), m.end() + _WINDOW)
        window = text[start:end]
        if not _COMPARATIVE_CONTEXT_RE.search(window):
            continue
        hits.append(RevisionMarkerHit(marker=m.group(0), context=window.strip(), start=m.start()))
    return hits


_SYSTEM_PROMPT = (
    "Ты определяешь, описывает ли предоставленный фрагмент отчёта ДЕЙСТВИТЕЛЬНЫЙ "
    "пересмотр (restated) или переиздание (reissued) сравнительных данных "
    "прошлого периода — а не случайное упоминание слова вне финансового "
    "контекста. Правила: (1) is_revision=false, если фрагмент не связывает "
    "маркер с конкретным периодом или строкой отчётности; (2) revision_kind "
    "— строго \"restated\" либо \"reissued\"; (3) supersedes_document_id — "
    "ТОЛЬКО один из id, перечисленных в candidate_prior_documents, либо null, "
    "если ни один не подходит — не выдумывай новый идентификатор; (4) не "
    "распространяй пересмотр на документ целиком без основания в тексте. "
    "Верни JSON вида {\"is_revision\": bool, \"revision_kind\": \"restated\"|"
    "\"reissued\", \"supersedes_document_id\": str|null, \"reason\": str}."
)


def _build_prompt(hits: Sequence[RevisionMarkerHit], candidates: Sequence[dict]) -> str:
    payload = {
        "marker_contexts": [{"marker": h.marker, "context": h.context} for h in hits],
        "candidate_prior_documents": list(candidates),
    }
    return "Определи наличие пересмотра по фрагментам ниже (JSON):\n" + json.dumps(payload, ensure_ascii=False)


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


def detect_revision(
    text: str,
    *,
    document_id: str,
    candidate_prior_documents: Sequence[dict] = (),
    model_id: str | None = None,
    llm_json_call: LlmJsonCall | None = None,
) -> RevisionInfo | None:
    """Детерминированный маркер → (только если найден) модель под жёсткой
    схемой. `candidate_prior_documents` — уже известные документы того же
    набора (`[{"document_id", "canonical_name", "period_ref"}, ...]`), не
    вновь распознаваемые здесь — атом не ищет вторую сторону сам, ему её
    называет вызывающий (`set.assemble`/тест сценария), как и `ref.prior`
    получает вторую сторону от уже собранных `ctx.dependencies()`."""
    hits = find_revision_markers(text)
    if not hits:
        return None

    call = llm_json_call or (lambda p, s: _default_llm_json_call(p, s, model_id=model_id))
    try:
        raw = call(_build_prompt(hits, candidate_prior_documents), _SYSTEM_PROMPT)
    except Exception:  # noqa: BLE001 — модельный сбой не создаёт пересмотр без основания
        return None

    if not isinstance(raw, dict) or not raw.get("is_revision"):
        return None
    kind = raw.get("revision_kind")
    if kind not in ("restated", "reissued"):
        return None

    supersedes = raw.get("supersedes_document_id")
    known_ids = {c.get("document_id") for c in candidate_prior_documents}
    if supersedes is not None and supersedes not in known_ids:
        # Модель не имеет права назвать документ вне предложенного списка —
        # вторая линия защиты, не полагающаяся на дисциплину модели.
        supersedes = None

    try:
        return RevisionInfo(
            revision_id=f"rev-{document_id}",
            document_id=document_id,
            revision_kind=kind,
            supersedes=supersedes,
        )
    except ValidationError:
        return None


__all__ = [
    "ATOM_VERSION",
    "RevisionMarkerHit",
    "find_revision_markers",
    "detect_revision",
    "LlmJsonCall",
]
