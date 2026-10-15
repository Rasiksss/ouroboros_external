"""Область «анализ»: сравнение по правилам, суждение по методике, объяснение.

Суждение формулирует модель по методике (нерушимое решение №5) — код здесь
только хранит и типизирует результат, не вычисляет финансовый смысл.
"""
from __future__ import annotations

import math
from typing import Literal

from pydantic import Field, field_validator

from .base import Contract, NonEmptyStr, new_id
from .evidence import ConditionOperator

# Тип связи одного тезиса объяснения (карточка `explain.internal`, 16b:
# «Если причинность не доказана, формулировать связь/сопутствующее
# изменение, а не утверждать причину»). Закрытый список — три степени
# доказанности, не пять оттенков уверенности, которые нечем было бы отличить
# программно.
CausalType = Literal["causal", "correlation", "co_movement"]


class RuleComparison(Contract):
    """Детерминированное сравнение факта с порогом/условием (`assess.rule`,
    Срез 9, B10) — ТОЛЬКО формальный результат оператора (нерушимое решение
    №5: «финансовое смысловое суждение формулирует модель», не этот атом).
    Никаких качественных ярлыков "высокий риск"/"хорошо"/"плохо" здесь нет и
    не должно появиться — смысл добавляет `assess.judgment` по методике,
    используя `rule_id` как ссылку.

    `operator`/`actual_value`/`threshold_value` — то, что реально сравнили;
    `delta` = `actual_value - threshold_value` (знак не зависит от
    оператора); `headroom` — запас/превышение С УЧЁТОМ направления оператора
    (`threshold - actual` для `<`/`<=`, `actual - threshold` для `>`/`>=`,
    `-|actual - threshold|` для `==`/`!=`): положительный — запас до
    нарушения, отрицательный — величина превышения. `relative_delta` —
    `delta`, отнесённая к `|threshold_value|` (`None`, если порог — ноль).

    Обе цепочки происхождения (карточка: «operator, actual, threshold, delta,
    relative delta/headroom and both provenance chains») — `actual_evidence_id`
    (факт: `ScalarFact`/`DerivedFact.evidence_id`) и `threshold_reference_id`
    (`ReferenceValue.reference_id` из `ref.plan`/`ref.norm`, которая сама несёт
    дальнейшую ссылку на `ConditionEvidence`/версionированный источник) — ни
    одна не разрешается в исполняемый код здесь, обе остаются данными для
    последующей проверки/цитирования."""

    rule_id: str = Field(default_factory=new_id)
    concept: NonEmptyStr
    operator: ConditionOperator
    actual_value: float
    actual_evidence_id: NonEmptyStr
    threshold_value: float
    threshold_reference_id: NonEmptyStr
    unit: str | None = None
    delta: float
    relative_delta: float | None = None
    headroom: float | None = None
    passed: bool
    detail: str = ""

    @field_validator("actual_value", "threshold_value", "delta")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("RuleComparison: числовое поле должно быть конечным, получено NaN/inf")
        return v


class Judgment(Contract):
    """Финансовое суждение модели по методике, со ссылкой на доказательства."""

    judgment_id: str = Field(default_factory=new_id)
    concept: NonEmptyStr
    verdict: NonEmptyStr
    supporting_evidence_ids: list[str]
    methodology_ref: NonEmptyStr


class ExplanationFactor(Contract):
    """Один тезис объяснения — связь и её степень доказанности проверяются
    ПООДИНОЧКЕ, не домысливаются из общего текста (карточка `explain.
    internal`, 16b). `evidence_ids` пустой — законное состояние для тезиса
    без собственной опоры (например чисто макро-контекстного, ссылающегося
    только на `ExternalFactRef` через отдельное поле вызывающего атома), но
    `causal_type="causal"` без evidence_ids запрещает поведенческий инвариант
    самого `explain.internal`, не схема."""

    text: NonEmptyStr
    causal_type: CausalType
    evidence_ids: list[str] = Field(default_factory=list)


class Explanation(Contract):
    """Текстовое объяснение суждения с явными ссылками на доказательства.

    `factors` по умолчанию пуст — обратная совместимость с производителями
    Фазы 0, которые не различали причину/корреляцию. `explain.internal`/
    `explain.external` (16b) обязаны его заполнять — это поведенческий
    инвариант атомов, не ограничение схемы."""

    explanation_id: str = Field(default_factory=new_id)
    judgment_id: NonEmptyStr
    text: NonEmptyStr
    cited_evidence_ids: list[str]
    factors: list[ExplanationFactor] = Field(default_factory=list)


class ExternalFactRef(Contract):
    """Факт вне отчёта — источник, дата, привязка к сущности и юрисдикции
    (карточка `explain.external`, 16b). Внешний факт не может подменить
    число из отчётности (нерушимое решение №3) — самостоятельная запись со
    своим происхождением, не `MetricEvidence` и не `EvidenceRecord` (не
    факт ИЗ документа, поэтому вне решения №4 про факты/цитаты/условия)."""

    fact_id: str = Field(default_factory=new_id)
    text: NonEmptyStr
    entity_id: NonEmptyStr
    jurisdiction: NonEmptyStr
    date: NonEmptyStr
    source: NonEmptyStr
    url: NonEmptyStr
    period_ref: str | None = None
