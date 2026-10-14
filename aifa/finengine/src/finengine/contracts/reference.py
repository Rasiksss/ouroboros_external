"""Область «сравнение»: типизированная база сравнения для показателя —
сопоставимое значение предыдущего периода (`ref.prior`, 16b), плановое/
целевое значение заказчика (`ref.plan`, Срез 9) или нормативный/договорный
порог (`ref.norm`, Срез 9), с явным `source_kind`, а не тремя похожими, но
раздельными типами.

**Почему один тип, не три (C2, Срез 9).** Карточки `ref.plan`/`ref.norm`
(`19-srez-09-...md`) прямо называют результат `ReferenceValue`, тем же именем,
что уже несёт `ref.prior` (16b) — это согласуется с нерушимым решением №4
(«единый механизм идентичности, происхождения и версионирования» для разных
видов доказательств/баз сравнения), а не совпадение имён. Различение по
`OutputSpec.output_type` НА УРОВНЕ РЕЕСТРА АТОМОВ (`ReferenceValue:plan`/
`ReferenceValue:norm` — см. `graph_atoms.py`, тот же приём, что `Verified:
citation`/`doc.textify:pages`) не мешает трём producer'ам делить один
Python-тип: `assess.rule` запрашивает `ReferenceValue:plan`/`ReferenceValue:
norm` явно и никогда не получает `prior_period`-запись случайно.

**Почему поля `prior_*` НЕ обобщены под общее имя.** `ref.prior` ссылается на
`MetricEvidence`, УЖЕ существующее в ТОМ ЖЕ прогоне («не второе число рядом с
текущим» — `prior_evidence_id`/`prior_metric_id` устраняют любую
денормализацию значения). `ref.plan`/`ref.norm`, напротив, чаще ссылаются на
значение из ДРУГОГО документа/версии политики без собственного `EvidenceRecord`
в этом прогоне (Срез 8 уже честно зафиксировал: каждый документ проходит свой
отдельный однодокументный прогон, см. `document_set.py`) — для них `value`/
`unit`/`operator` денормализуются на уровень обёртки (тот же приём, что
`ExtractedMetric.concept`/`.period_ref` уже дублируют `scalar_fact.concept`/
`.period_ref` ради удобства потребителя), а `source_evidence_id`/
`source_citation` несут происхождение. Переименовывать уже работающие
`current_period_ref`/`prior_period_ref`/... под generic-имена ради чистой
формы означало бы правку `ref_prior.py`/`explain_internal.py` без
функциональной необходимости — не делаем (CLAUDE.md §3)."""
from __future__ import annotations

import math
from typing import Literal

from pydantic import Field, field_validator

from .base import Contract, NonEmptyStr, new_id
from .basis import BasisComparisonStatus
from .evidence import ConditionOperator
from .periods import RevisionInfo

ReferenceSourceKind = Literal["prior_period", "plan", "norm"]


class ReferenceValue(Contract):
    concept: NonEmptyStr
    reference_id: str = Field(default_factory=new_id)
    # По умолчанию "prior_period" — единственный вид источника, который эта
    # запись несла до Среза 9; `ref_prior.py` не обязан объявлять его явно,
    # чтобы не переписывать уже проверенный, работающий атом без надобности.
    source_kind: ReferenceSourceKind = "prior_period"
    selection_reason: NonEmptyStr
    basis_status: BasisComparisonStatus = "comparable"
    # Ревизия учитывается, когда её несёт исходный факт — сегодня ни один
    # producer её не прикрепляет (тот же честный пробел, что `Basis` у
    # `verify_identity_cross.py`); поле не удаляется ради будущего
    # producer'а, просто не заполняется раньше времени.
    revision: RevisionInfo | None = None

    # --- source_kind="prior_period" (ref.prior, 16b) ---
    current_period_ref: str | None = None
    current_evidence_id: str | None = None
    prior_period_ref: str | None = None
    prior_evidence_id: str | None = None
    prior_metric_id: str | None = None

    # --- source_kind in {"plan", "norm"} (ref.plan/ref.norm, Срез 9) ---
    period_ref: str | None = None
    unit: str | None = None
    operator: ConditionOperator | None = None
    value: float | None = None
    basis: str | None = None
    # Ссылка на реальный `EvidenceRecord` этого прогона (например
    # `ConditionEvidence.evidence_id` для `ref.norm`, сославшегося на
    # проверенное условие договора) — задана, когда такая запись есть.
    source_evidence_id: str | None = None
    # Свободный текст-указатель на источник БЕЗ отдельного `EvidenceRecord`
    # (версionированная методика заказчика/справочник/файл плана) — задан,
    # когда `source_evidence_id` не применим (карточка `ref.plan`: «источник
    # и версия... сохраняются в доказательной цепочке»).
    source_citation: str | None = None
    source_version: str | None = None
    jurisdiction: str | None = None

    @field_validator("value")
    @classmethod
    def _value_finite(cls, v: float | None) -> float | None:
        if v is not None and not math.isfinite(v):
            raise ValueError("ReferenceValue.value must be finite, got NaN/inf")
        return v


__all__ = ["ReferenceSourceKind", "ReferenceValue"]
