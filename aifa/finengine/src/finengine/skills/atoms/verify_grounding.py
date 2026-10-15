"""`verify.grounding` — карточка 16a: обязательный барьер, не пускающий
финансовое числовое утверждение в итоговый текст без связи с доказательной
базой.

**Границы этой сессии.** Потребителя (нарратива) сегодня в графе нет —
`out.narrative`/`explain.*`/`assess.judgment` не реализованы (Срез 6b).
`NarrativeOutput` (`contracts/results.py`) несёт текст ЦЕЛИКОМ и плоский
список `cited_evidence_ids` — этого недостаточно для проверки «какое именно
число к какому именно доказательству привязано» (нужна связь на уровне
ОДНОГО утверждения, не всего текста). `NumericClaim` ниже — намеренно НЕ
`contracts.Contract` (не A0-контракт): 16a сама называет вход «тестовым
контрактом», потому что настоящую форму структурированного утверждения
определит `out.narrative`/`explain.*` в 6b — эта сессия строит и проверяет
ТОЛЬКО барьер, не канонизирует раньше времени форму, которой ещё нет
реального производителя.

**Различение "финансовое число" vs "служебное число" — по явному полю
`kind`, не по regex/эвристике над текстом** (карточка: «Даты, номера страниц
и иные нефинансовые числа должны классифицироваться отдельно, чтобы не
создавать ложные блокировки»; история диапазонов уже документирует урок
репозитория про «регулярки по прозе» — не повторяем его здесь)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from finengine.contracts import (
    DerivedFact,
    EvidenceBundleV2,
    HumanOverride,
    NarrativeOutput,
    QuoteEvidence,
    ReportOutput,
    RunPassport,
    ScalarFact,
    VerificationFinding,
)
from finengine.skills.spec.atom_registry import NodeArgs
from finengine.skills.spec.target import InputSpec

ATOM_VERSION = "1.0.0"
CHECK_KIND = "grounding"

ClaimKind = Literal["financial", "date", "page_number", "other_id", "narrative"]

# Округление форматирования (текст печатает «≈1,5 млрд» для факта 1 532,4) —
# допустимо; величина расхождения — не проверенный норматив, а разумный
# верхний предел человеческого округления в отчётах (см. `verify_identity_
# cross.DEFAULT_CROSS_TOLERANCE`, тот же принцип, тот же порядок величины).
ROUNDING_TOLERANCE = 0.01


@dataclass(frozen=True)
class NumericClaim:
    """Одно утверждение аналитического текста — ЧТО заявлено (число/дата/
    номер) и НА ЧТО оно ссылается (`evidence_id` или `None`, если модель не
    указала происхождение вовсе — это и есть блокируемый случай)."""

    text: str
    kind: ClaimKind
    value: float | None = None
    period_ref: str | None = None
    unit: str | None = None
    evidence_id: str | None = None


def check_grounding(
    claim: NumericClaim, evidence_by_id: dict[str, ScalarFact | DerivedFact | HumanOverride | QuoteEvidence],
) -> VerificationFinding:
    common = dict(
        produced_by_atom="verify.grounding", produced_by_atom_version=ATOM_VERSION,
        source_ref=claim.evidence_id or "no-evidence", period_ref=claim.period_ref, check_kind=CHECK_KIND,
    )
    if claim.kind != "financial":
        # Служебное число (дата/страница/id) — вне периметра барьера по
        # ПОЛИТИКЕ (явное поле), не потому что оно "не похоже на деньги".
        return VerificationFinding(**common, status="pass", detail=f"{claim.kind}: не финансовое утверждение")

    if not claim.evidence_id:
        return VerificationFinding(
            **common, status="fail", detail=f"финансовое утверждение {claim.text!r} без evidence_id",
        )

    fact = evidence_by_id.get(claim.evidence_id)
    if fact is None:
        return VerificationFinding(
            **common, status="fail",
            detail=f"evidence_id {claim.evidence_id!r} не разрешается ни в один факт текущей доказательной базы",
        )

    if isinstance(fact, QuoteEvidence):
        # Число внутри уже проверенной цитаты — законное основание, но
        # проверяется вхождением в буквальный текст цитаты, не численным
        # равенством (у цитаты нет структурированного `.value`). Целое
        # значение (500.0) в прозе почти всегда пишут без ".0" ("500", не
        # "500.0") — сверяем обе формы, не только `str(float)`.
        if claim.value is not None:
            candidates = {str(claim.value), f"{claim.value:,}"}
            if claim.value == int(claim.value):
                candidates.add(str(int(claim.value)))
                candidates.add(f"{int(claim.value):,}")
            if not any(c in fact.quote_text for c in candidates):
                return VerificationFinding(
                    **common, status="fail",
                    detail=f"число {claim.value!r} не найдено в тексте цитаты {claim.evidence_id!r}",
                )
        return VerificationFinding(**common, status="pass", detail="число подтверждено текстом проверенной цитаты")

    if isinstance(fact, HumanOverride):
        return VerificationFinding(**common, status="pass", detail="число подтверждено пользовательской поправкой")

    # ScalarFact | DerivedFact — числовое доказательство с value/period/unit.
    if claim.period_ref and fact.period_ref and claim.period_ref != fact.period_ref:
        return VerificationFinding(
            **common, status="fail",
            detail=f"подмена периода: утверждение называет {claim.period_ref!r}, доказательство — {fact.period_ref!r}",
        )
    if claim.unit and getattr(fact, "unit", None) and claim.unit != fact.unit:
        return VerificationFinding(
            **common, status="fail",
            detail=f"подмена единицы: утверждение называет {claim.unit!r}, доказательство — {fact.unit!r}",
        )
    if claim.value is not None:
        denom = abs(fact.value) or 1.0
        rel_delta = abs(claim.value - fact.value) / denom
        if (claim.value < 0) != (fact.value < 0) and fact.value != 0 and claim.value != 0:
            return VerificationFinding(
                **common, status="fail",
                detail=f"подмена знака: утверждение {claim.value!r}, доказательство {fact.value!r}",
            )
        if rel_delta > ROUNDING_TOLERANCE:
            return VerificationFinding(
                **common, status="fail",
                detail=f"значение не совпадает с доказательством за пределами допуска округления: "
                       f"{claim.value!r} против {fact.value!r}",
            )
    return VerificationFinding(**common, status="pass", detail=f"число подтверждено доказательством {claim.evidence_id}")


# ---------------------------------------------------------------------------
# Срез 6b: барьер как настоящий узел графа (16a карточка «Как встроить: сначала
# применить только к новым типизированным результатам анализа, затем сделать
# обязательным барьером всех финансовых ответов» — здесь: `mandatory_gates`
# у `out.narrative`). `NarrativeOutput.claims` (Срез 6b, `contracts/results.py`)
# — первый настоящий производитель структурированных утверждений, которого
# карточка 16a ждала при определении `NumericClaim` как «тестового контракта».
# `check_grounding` не меняется: `NumericClaimRecord` несёт те же имена полей
# (`kind`/`value`/`period_ref`/`unit`/`evidence_id`/`text`) и принимается по
# утиной типизации без конвертации.
def verify_grounding_requires(node_args: NodeArgs, run_passport: RunPassport | None) -> tuple[InputSpec, ...]:
    # `EvidenceBundleV2` — единственный источник `evidence_by_id` в этой
    # сессии (Срез 6b, scoping-решение): полноценная сверка по `QuoteEvidence`/
    # `HumanOverride` требовала бы прокидывать те же per-topic входы, что
    # `out.narrative` (какие темы цитат реально были в тексте) — отдельная
    # механика, не построенная в этом срезе (см. отчёт по Срезу 6b). Заявление
    # с `evidence_id` цитаты/поправки сегодня честно получает `fail`
    # («не разрешается ни в один факт текущей доказательной базы»), а не
    # ложный `pass` — тот же класс исхода, что «источник не подключён», не
    # тихая дыра.
    return (InputSpec(input_type="EvidenceBundleV2"),)


def verify_grounding_gate(args: NodeArgs, ctx) -> list:
    result = next(
        (e for e in ctx.dependencies() if isinstance(e, (NarrativeOutput, ReportOutput))),
        None,
    )
    if result is None:
        return []
    evidence_by_id: dict[str, ScalarFact | DerivedFact | HumanOverride | QuoteEvidence] = {}
    for e in ctx.dependencies():
        if isinstance(e, EvidenceBundleV2):
            for fact in (*e.scalar_facts, *e.derived_facts):
                evidence_by_id[fact.evidence_id] = fact
        elif isinstance(e, (QuoteEvidence, HumanOverride)):
            evidence_by_id[e.evidence_id] = e

    if isinstance(result, NarrativeOutput):
        claims = list(result.claims)
    else:
        claims = [
            claim
            for section in result.sections
            for thesis in section.theses
            for claim in thesis.claims
        ]
    findings = [check_grounding(claim, evidence_by_id) for claim in claims]
    if any(f.status == "fail" for f in findings):
        # Барьер: незаземлённое утверждение блокирует ВЕСЬ текст, не только
        # свою строку — `NarrativeOutput` не проходит дальше по графу.
        return findings
    return [*findings, result]


__all__ = [
    "NumericClaim", "ClaimKind", "check_grounding", "ROUNDING_TOLERANCE", "ATOM_VERSION", "CHECK_KIND",
    "verify_grounding_requires", "verify_grounding_gate",
]
