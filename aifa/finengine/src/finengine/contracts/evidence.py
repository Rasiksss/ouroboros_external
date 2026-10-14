"""Область «доказательства»: факты, цитаты, условия, связи, правки, проверки.

Восемь из девяти типов наследуют `EvidenceRecord` (нерушимое решение №4): у
каждого есть атом-производитель, его версия, источник, и исправление — это
НОВАЯ запись со `supersedes`, указывающим на исходную, а не мутация
существующей. Девятый, `CandidateLocator` (Срез 5a, атом `locate`), —
намеренное исключение: решение №4 перечисляет факты/цитаты/условия/структуры/
связи/правки как то, что обязано иметь единую идентичность и происхождение;
адрес-КАНДИДАТ к ним не относится, пока `read.*` не подтвердил его
содержимое («модель предлагает адрес, код проверяет адрес до доверия
содержимому» — инженерный принцип карточки `locate`).
"""
from __future__ import annotations

import math
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .base import Contract, EvidenceRecord, NonEmptyStr, new_id


class SourceLocator(Contract):
    """Точный адрес фрагмента внутри документа: страница, таблица/раздел,
    фрагмент текста (карточки `locate`/`read.value`). Кандидат без ХОТЯ БЫ
    одного из трёх якорей не допускается в проверенный путь чтения — краевой
    случай карточки `locate`."""

    page: int | None = None
    table_or_section: str | None = None
    text_fragment: str = ""

    @model_validator(mode="after")
    def _has_at_least_one_anchor(self):
        if self.page is None and not self.table_or_section and not self.text_fragment:
            raise ValueError(
                "SourceLocator: нужен хотя бы один якорь (page/table_or_section/text_fragment) — "
                "адрес без единого якоря не допускается в проверенный путь чтения"
            )
        return self


class CandidateLocator(Contract):
    """Найденный, но НЕ подтверждённый адрес доказательства под один концепт
    (`locate`, A7). НЕ `EvidenceRecord` — см. докстринг модуля.

    `origin="alias_match"` — детерминированное совпадение псевдонима
    (карточка `locate`: «детерминированные совпадения псевдонимов добавляются
    как кандидаты, но не как окончательная истина»), не модельный вывод.
    """

    locator_id: str = Field(default_factory=new_id)
    produced_by_atom: NonEmptyStr
    produced_by_atom_version: NonEmptyStr
    document_id: NonEmptyStr
    document_version: NonEmptyStr
    concept: NonEmptyStr
    locator: SourceLocator
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    origin: Literal["model", "alias_match"] = "model"


class ScalarFact(EvidenceRecord):
    """Одно извлечённое или подтверждённое число с периодом и единицей."""

    value: float
    unit: NonEmptyStr
    period_ref: NonEmptyStr
    page: int | None = None

    @field_validator("value")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("value must be finite, got NaN/inf")
        return v


class StructuredFact(EvidenceRecord):
    """Структурированная таблица/раскрытие (сегменты, сроки погашения, группа)."""

    table_kind: NonEmptyStr
    rows: list[dict]
    page: int | None = None


class QuoteEvidence(EvidenceRecord):
    """Буквальная цитата из документа со страницей (карточка `read.quote`,
    15b). `section`/`range_start`/`range_end` — «диапазон/якорь» карточки:
    необязательны (не каждый источник даёт секцию/оффсет), но `read.quote`
    обязан заполнять хотя бы `page`, унаследованный `document_version`
    (`EvidenceRecord`) и `fragment_hash` для повторной проверки — это
    инвариант АТОМА (свой набор тестов), не ограничение схемы: как и у
    `ScalarFact.page`, отсутствие поля здесь означает «не измерено этим
    производителем», а не «не нужно вовсе»."""

    quote_text: NonEmptyStr
    page: int = Field(ge=1)
    section: str | None = None
    range_start: int | None = None
    range_end: int | None = None
    fragment_hash: str | None = None

    @model_validator(mode="after")
    def _range_is_ordered(self):
        if self.range_start is not None and self.range_end is not None and self.range_end < self.range_start:
            raise ValueError(
                f"QuoteEvidence: range_end={self.range_end} < range_start={self.range_start}"
            )
        return self


ConditionOperator = Literal["<", "<=", ">", ">=", "==", "!="]


class ConditionEvidence(EvidenceRecord):
    """Договорное/нормативное условие (ковенант) с точным адресом источника
    (карточка `read.condition`, Срез 9, B10) — тот же паттерн «модель находит
    адрес, код проверяет его до доверия содержимому», что `QuoteEvidence`
    (`read.quote`), поэтому `page` здесь ОБЯЗАТЕЛЕН, как и там: `read.
    condition` не создаёт запись без подтверждённой цитаты.

    `concept` (унаследован от `EvidenceRecord`) — тестируемый показатель,
    к которому относится условие; отдельного `applies_to_concept` не заводим
    — было бы голым дублированием того же поля Фазы 0.

    Оператор/порог/единица/субъект — все опциональны (`None` = «эта ось не
    разобрана детерминированно», тот же принцип, что `Basis`/`MetricDefinition`):
    неясная или неполная формулировка НЕ подменяется угадыванием, а условие
    остаётся неполным доказательством (карточка: «неясное или неполное
    условие не превращается в точное числовое правило») — атом-производитель
    НЕ решает, соблюдается ли условие, это `assess.rule`/`assess.judgment`.
    `definition_text`/`exceptions` — формула и оговорки как текст, до
    разрешения в канонические концепты (карточка: «формула/термины хранятся
    как текст»)."""

    condition_text: NonEmptyStr
    page: int = Field(ge=1)
    operator: ConditionOperator | None = None
    threshold_value: float | None = None
    unit: str | None = None
    subject: str | None = None
    definition_text: str | None = None
    exceptions: list[str] = Field(default_factory=list)

    @field_validator("threshold_value")
    @classmethod
    def _threshold_finite(cls, v: float | None) -> float | None:
        if v is not None and not math.isfinite(v):
            raise ValueError("threshold_value must be finite, got NaN/inf")
        return v


class EntityRelation(EvidenceRecord):
    """Связь между двумя сущностями (например, дочернее общество — материнское;
    карточка `read.entities`, 15b). `relation` остаётся свободной строкой
    (`subsidiary`/`associate`/`joint_venture`/любая другая, включая
    неизвестную — карточка прямо запрещает молча отбрасывать незнакомый вид),
    не закрытым `Literal`: закрытый список превратил бы «сохранить
    неподдерживаемое отношение» в невозможное состояние схемы.

    `direction` фиксирует, кто владеет кем, ОТДЕЛЬНО от текста `relation`:
    `subject_entity`/`object_entity` сами по себе не гарантируют порядок
    (какая сторона в контракте subject, а какая object — соглашение
    конкретного производителя), поэтому направление владения читается по
    этому полю, а не подразумевается расположением сторон.

    `ownership_percent`/`ownership_percent_raw` — числовая и исходная
    текстовая форма ОДНОЙ и той же доли (не два независимых значения):
    `ownership_percent` может быть `None`, когда исходный текст не сводится
    к чистому проценту (например `"75% минус одна акция"` или `"-"`), но
    `ownership_percent_raw` сохраняет буквальный текст всегда, когда он есть
    в источнике — потеря исходной формулировки ради красивого числа была бы
    домысливанием (решение репозитория «не угадывать»)."""

    subject_entity: NonEmptyStr
    relation: NonEmptyStr
    object_entity: NonEmptyStr
    direction: Literal["subject_owns_object", "object_owns_subject", "unspecified"] = "unspecified"
    ownership_percent: float | None = None
    ownership_percent_raw: str | None = None
    page: int | None = None


class HumanOverride(EvidenceRecord):
    """Пользовательская поправка к существующему доказательству.

    Срез 10 сохраняет старые `reviewer`/`justification` для совместимости с
    уже сериализованными контрактами, но новый путь дополнительно штампует
    owner/case, исходную формулировку и нормализованное числовое значение.
    Исходный факт никогда не меняется: связь только через
    `overridden_evidence_id`/`supersedes`.
    """

    overridden_evidence_id: NonEmptyStr
    new_value: NonEmptyStr
    reviewer: NonEmptyStr
    justification: NonEmptyStr
    owner_id: str | None = None
    case_id: str | None = None
    original_wording: str = ""
    reason: str = ""
    numeric_value: float | None = None
    unit: str | None = None


class DerivedFact(EvidenceRecord):
    """Расчётный факт: формула, её версия и evidence_id входных доказательств
    — dependency graph, а не только число (карточка `calc.formula`, 16a).

    `inputs` — `evidence_id` зависимостей, которые реально использовал этот
    расчёт (`ScalarFact`/`DerivedFact` входов, разрешённых Planner'ом ДО
    исполнения — A1.6), НЕ голые имена концептов: имя концепта уже есть у
    каждой зависимости через её собственный `concept` (`EvidenceRecord`), а
    `evidence_id` даёт возможность дойти до КОНКРЕТНОЙ версии/ревизии факта,
    которая была на входе именно этого расчёта, а не «любой факт с таким
    именем». `formula_version`/`expression` — версия и точный текст формулы
    из `FormulaDefinition`, зафиксированной ДО расчёта (не подставляются
    задним числом из текущего состояния реестра)."""

    formula_id: NonEmptyStr
    formula_version: NonEmptyStr
    expression: str = ""
    inputs: list[str]
    value: float
    unit: str | None = None

    @field_validator("value")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("value must be finite, got NaN/inf")
        return v


class VerificationFinding(EvidenceRecord):
    """Результат проверки (арифметика, цитата, противоречие) над другим доказательством."""

    check_kind: NonEmptyStr
    status: NonEmptyStr
    detail: str = ""


class ExtractedMetric(Contract):
    """Planning/domain wrapper вокруг `ScalarFact`, добытого чтением отчёта
    (`read.value`, Срез 5a). Не хранит второй копии значения — единственное
    число живёт в `scalar_fact.value`; wrapper несёт происхождение
    (`registry`/`ad_hoc`) и делает `ExtractedMetric` и `CalculatedMetric`
    различимыми РОДАМИ типа для `GraphPlanner`/`Target`, даже когда концепт,
    период и итоговое значение совпадают (R2 Definition of Done, v9).

    `output_type` — дискриминант терминального typed-результата (Срез 27):
    делает `ExtractedMetric` адресуемым в `result_events.TypedResultModel`
    наравне с `ReportOutput`/`FormOutput`, тем же полем, на которое уже
    опирается station listener и веб-фронтенд."""

    schema_version: Literal["1.0"] = "1.0"
    output_type: Literal["ExtractedMetric"] = "ExtractedMetric"
    status: Literal["supported"] = "supported"
    metric_id: str = Field(default_factory=new_id)
    scalar_fact: ScalarFact
    concept: NonEmptyStr
    period_ref: NonEmptyStr
    definition_origin: Literal["registry", "ad_hoc"] = "registry"


class CalculatedMetric(Contract):
    """Planning/domain wrapper вокруг `DerivedFact`, посчитанного `calc.
    formula_engine`. Симметричен `ExtractedMetric` — та же роль, другой род."""

    schema_version: Literal["1.0"] = "1.0"
    output_type: Literal["CalculatedMetric"] = "CalculatedMetric"
    status: Literal["supported"] = "supported"
    metric_id: str = Field(default_factory=new_id)
    derived_fact: DerivedFact
    concept: NonEmptyStr
    period_ref: NonEmptyStr
    definition_origin: Literal["registry", "ad_hoc"] = "registry"


class UserProvidedMetric(Contract):
    """Planning/domain wrapper вокруг пользовательской поправки
    (`HumanOverride`) — третий род происхождения числа наравне с
    `ExtractedMetric`/`CalculatedMetric`."""

    metric_id: str = Field(default_factory=new_id)
    human_override: HumanOverride
    concept: NonEmptyStr
    period_ref: NonEmptyStr
    definition_origin: Literal["registry", "ad_hoc"] = "registry"


class ExtractedStructure(Contract):
    """Planning/domain wrapper вокруг `StructuredFact` (сегменты, сроки
    погашения, состав группы) — тот же принцип, что `ExtractedMetric`, для
    структурированных, а не скалярных доказательств."""

    structure_id: str = Field(default_factory=new_id)
    structured_fact: StructuredFact
    table_kind: NonEmptyStr
    definition_origin: Literal["registry", "ad_hoc"] = "registry"


# Общий downstream union для анализа/форм (R2): "любое происхождение числа
# показателя", не различая род, — ровно то, что нужно потребителю, которому
# всё равно, откуда взято число, лишь бы оно было доказано.
MetricEvidence = ExtractedMetric | CalculatedMetric | UserProvidedMetric


class EvidenceBundleV2(Contract):
    """Единая доказательная база одного запуска (решения №3/№4; Срез 2, B2).

    Не новый источник чисел — контейнер-адаптер над сегодняшним результатом
    `two_stage_extractor` (`{run, metrics, segments, group}`) и над строками,
    которые добавляет `formula_engine` (через `skills.ratios.compute_ratios`) в
    тот же конверт. `skills.evidence_bundle.evidence_bundle_from_envelope`
    строит этот контракт, не трогая сами извлечение и расчёт.
    """

    bundle_id: str = Field(default_factory=new_id)
    run_id: NonEmptyStr
    scalar_facts: list[ScalarFact] = Field(default_factory=list)
    structured_facts: list[StructuredFact] = Field(default_factory=list)
    derived_facts: list[DerivedFact] = Field(default_factory=list)
    # Срез 6b: состав группы/доли участия как evidence анализа (карточка
    # `explain.internal`/критерий 5 «Критерия готовности связки анализа»,
    # 16b) — тем же принципом, что `structured_facts` для сегментов: контейнер
    # чужого payload, не второе извлечение.
    entity_relations: list[EntityRelation] = Field(default_factory=list)
