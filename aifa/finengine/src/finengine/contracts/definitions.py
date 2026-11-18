"""Область «определения»: версионированные planning-artifacts, которые
`GraphPlanner` обязан раскрыть ДО запуска зависимого handler'а (A1.6) — не
исполняемые доказательства (`EvidenceRecord`), а спецификации, из которых
`calc.formula`/`norm.*` строят конкретные требуемые входы.

`MetricDefinition`/`FormulaDefinition` не заменяют `skills.metric_schema.
MetricDef/CalcDef` (те остаются схемой ФАЙЛА реестра ячейки) — их
материализует детерминированный definition resolver (`skills/atoms/
definitions.py`) из зарегистрированной строки реестра ИЛИ из `AdHocConceptSpec`/
`AdHocFormulaSpec` текущего запроса, давая `GraphPlanner`/атомам один
типизированный вид независимо от происхождения (16a, A1.6: «Реестровые
MetricDefinition/FormulaDefinition материализуются детерминированным
definition resolver ... доступны Planner как specification leaves»).
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import Contract, NonEmptyStr

DefinitionSource = Literal["registry", "ad_hoc"]


class MetricDefinition(Contract):
    """Каноническая семантика одного концепта для `norm.unit`/`norm.time`/
    `verify.bounds` — что переносится из `MetricDef` (`metric_schema.py`) в
    типизированный вид, доступный planner/атомам. Отсутствующее поле значит
    «схема ячейки его не задаёт» (сегодня почти всегда так для `unit_kind`/
    `scale_hint`/`sign_policy` — см. план миграции 16a), не «любое значение
    подходит» — потребитель обязан явно решить, что делать с неизвестным."""

    concept: NonEmptyStr
    version: NonEmptyStr
    unit_kind: str = ""
    scale_hint: str = ""
    # Передаётся как есть из `MetricDef.formula_default_abs_behavior`
    # (`metric_schema.py`) — узкое правило подстановки в формулу
    # (`formula_engine`), не отдельная заново изобретённая политика знака.
    formula_default_abs_behavior: bool = False
    period_type: str = ""
    source: DefinitionSource = "registry"


class FormulaDefinition(Contract):
    """Формула + версия + зависимости — то, что `calc.formula.dynamic_requires`
    обязан получить ДО раскрытия зависимых `MetricEvidence` Targets (A1.6).
    `hard_min`/`hard_max` — из `CalcDef.min_value/max_value` (уже существующее,
    версионированное, owner-заданное поле, Р7 24.08.2026); `soft_min`/
    `soft_max` — из мигрированных границ effective registry. Оба необязательны:
    отсутствие диапазона — законное состояние («не у всякой величины есть
    осмысленный диапазон»), не повод его выдумывать."""

    formula_id: NonEmptyStr
    version: NonEmptyStr
    expression: str = ""
    dependencies: list[str] = Field(default_factory=list)
    # Срез 31: зависимости, которых нет в реестре профиля документа (ad-hoc
    # компоненты формулы, придуманной моделью) — их извлекает экстрактор по
    # синонимам/определению; остальные — обычные `MetricEvidence` реестра.
    ad_hoc_dependencies: list[str] = Field(default_factory=list)
    unit_kind: str = ""
    annualize: bool = False
    hard_min: float | None = None
    hard_max: float | None = None
    soft_min: float | None = None
    soft_max: float | None = None
    source: DefinitionSource = "registry"


__all__ = ["DefinitionSource", "MetricDefinition", "FormulaDefinition"]
