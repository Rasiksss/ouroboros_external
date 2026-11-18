"""`Target`/`InputSpec`/`OutputSpec` — типизированная граница `AtomDef` (R3,
retrofit v9, docs/build-plan/13-srez-03-graph-planner-executor.md, A1/A1.2).

Те же самые frozen-dataclass'ы, что и `AtomDef`/`NodeArgs` (`atom_registry.py`)
— не `finengine.contracts.Contract`: `Target` не сериализуется через границу
процесса (`GraphPlanner` строит и потребляет его в одном процессе, как и
`NodeArgs`), это внутренняя структура планировщика, а не доменный контракт,
который путешествует между `aifa-extract`/`aifa-analyze`/бэкендом.

Заменяет `TargetNode` (`contracts/intent.py`) как корень `GraphPlanner.plan()`
— `TargetNode` остаётся только внутри `GoalIR.targets` как DTO «показатель,
упомянутый в тексте запроса» (namely то, что видит `intent.parse`), не как
исполняемая planning-семантика.

Ключевое отличие от `TargetNode.output_kind` ("scalar"/"bundle"/"form"):
`Target.output_type` различает РОД доказательства показателя, не только форму
результата — `ExtractedMetric` и `CalculatedMetric` для одного и того же
`concept_id` это ДВА разных `Target`, а не один `Target(concept=X)`,
разрешаемый через `PlanPolicy` tie-break между конкурирующими producer'ами
(была именно эта путаница до retrofit — см. `RETROFIT-REPORT-v9.md`, R3.3).

`entity_ref`/`period_ref`/`basis` остаются ЕДИНСТВЕННЫМ, не мн.ч. полем (не
`entity_ids: tuple[str, ...]`, как дан пример в 13-srez-03-...md) — намеренное
отклонение от буквального образца: продукт нигде сегодня не работает с
multi-entity/multi-period целью, а `NodeArgs`/`fingerprint()`/`key_fields`
(`atom_registry.py`) уже построены вокруг единственного значения каждого поля.
Расширять эту машинерию до кортежей ради формы примера без единого реального
сценария использования было бы спекулятивным расширением (CLAUDE.md §2).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

Cardinality = Literal["ONE", "MANY"]

# `MetricEvidence` (contracts/evidence.py) как output_type — не реальный
# производимый тип, а alias для резолюции: `AtomRegistry.producers_of(
# "MetricEvidence", concept)` разворачивается в объединение производителей
# ЭТИХ трёх настоящих типов для того же концепта (см. `atom_registry.py`).
# Используется там, где потребителю нужно «любое доказанное значение
# показателя», не различая род происхождения — например, собственные входы
# формулы (`calc.formula_engine`) или строка формы/полного анализа.
METRIC_EVIDENCE_MEMBERS: tuple[str, ...] = ("ExtractedMetric", "CalculatedMetric", "UserProvidedMetric")


@dataclass(frozen=True)
class OutputSpec:
    """Один заявленный выход `AtomDef` (A1: «`produces` — типизированная
    граница»). `concept=None` — атом с фиксированным неизменным выходом без
    измерения «какой показатель» (`RunPassport`, `EvidenceBundleV2`,
    `FormOutput`, `document_text`, ...); `concept` задан — per-показатель
    выход (`ExtractedMetric`/`CalculatedMetric`/`CandidateLocator`/...)."""

    output_type: str
    concept: str | None = None

    def label(self) -> str:
        """Человекочитаемая строка для `PlanNode.produces` (contracts/
        planning.py остаётся `list[str]` — граф не меняет форму сериализации
        ради внутреннего типа `AtomDef`, см. докстринг `graph_planner.py`).

        `concept is None` (продуктовый артефакт без измерения концепта) и
        `concept == output_type` (тестовая голая-строка-конвенция,
        `tests/planner/conftest.py::_as_output`) оба дают просто
        `output_type` — второй случай иначе задваивал бы одну и ту же
        строку (`"BRAND_KARMA_SCORE:BRAND_KARMA_SCORE"`)."""
        if self.concept is None or self.concept == self.output_type:
            return self.output_type
        return f"{self.output_type}:{self.concept}"


@dataclass(frozen=True)
class InputSpec:
    """Один заявленный обязательный вход `AtomDef.requires` — все `InputSpec`
    обязательны (A1.7: optional requires запрещены на базовом уровне)."""

    input_type: str
    concept: str | None = None
    cardinality: Cardinality = "ONE"


@dataclass(frozen=True)
class Target:
    """Конкретный запрошенный planning-результат — корень `GraphPlanner.plan()`
    (A1.2). `PlanNode` = применение `AtomDef` к `Target`; `ExtractedMetric(
    EBITDA, FY2025)` и `CalculatedMetric(EBITDA, FY2025)` — разные `Target`,
    не один `Target`, различаемый способом производства post-factum."""

    output_type: str
    concept_id: str | None = None
    entity_ref: str | None = None
    period_ref: str | None = None
    basis: str | None = None
    document_version: str | None = None
    policy_version: str | None = None
    scope_id: str | None = None
    params: Mapping[str, str] = field(default_factory=dict)


__all__ = [
    "Cardinality",
    "METRIC_EVIDENCE_MEMBERS",
    "OutputSpec",
    "InputSpec",
    "Target",
]
