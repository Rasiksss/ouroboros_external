"""Область «намерение»: перевод пользовательского запроса в цели запуска."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import Contract, NonEmptyStr, new_id

# Закрытый список канонических типов задачи (goal_type). Аудит Среза 4 (18.09,
# см. `docs/build-plan/00-kak-ispolzovat.md`, раздел «Если Срезы 1–5а уже
# сделаны...») подтвердил, что прежнее перечисление
# (`point_metric`/`full_analysis`/`form`) было только тремя иллюстративными
# примерами исходной карточки `intent.parse`, а не полным списком — расширено
# до каталога из 11 `goal_type`
# (`docs/build-plan/26-prilozhenie-ad-hoc-i-goalir-katalog.md`, раздел
# «Каталог goal_type (11 типов)»). Соответствие:
#
#   full_analysis               ↔ FULL_FINANCIAL_ANALYSIS
#   extract_registered_metric   ↔ EXTRACT_REGISTERED_METRIC
#   extract_ad_hoc_metric       ↔ EXTRACT_AD_HOC_METRIC
#   calculate_registered_metric ↔ CALCULATE_REGISTERED_METRIC
#   calculate_ad_hoc_metric     ↔ CALCULATE_AD_HOC_METRIC
#   extract_structure           ↔ EXTRACT_STRUCTURE
#   document_qa                 ↔ DOCUMENT_QA
#   compare_metrics             ↔ COMPARE_METRICS
#   explain_change               ↔ EXPLAIN_CHANGE
#   validate_report              ↔ VALIDATE_REPORT
#   plan_fact_analysis            ↔ PLAN_FACT_ANALYSIS
#   form                          ↔ (нет в каталоге из 11 — см. ниже)
#
# `form` — запрос конкретной формы/layout по роли заказчика (ДГР/CIB/BAIFP/
# DIE/risks/other) — сохранён как ДВЕНАДЦАТОЕ, дополнительное значение сверх
# каталога, а не удалён и не слит с `full_analysis`. Каталог из 11 типов его
# не упоминает вовсе (вероятно, потому что строка `FULL_FINANCIAL_ANALYSIS`
# описана как «джокер»-разворот через `layouts.json` по роли — форма могла
# задумываться как один из исходов той же ветки, где роль определяет форму/
# таблицу вместо текстового разбора). Но `form`/`FormOutput` глубоко зашиты в
# уже реализованный код (`graph_atoms.py`, `evidence.py`, `planning.py`,
# `results.py`, `roles/formatter.py`), и форма ДГР — защищённое нерушимое
# решение (`01-nerushimye-resheniya.md`, п.8: «ДГР и его форма являются
# полноценным типом результата»). Слияние с `full_analysis` — архитектурное
# решение (C2 методики: меняется публичный интерфейс/семантика планировщика),
# выходящее за периметр аудита Среза 4; открытый вопрос зафиксирован здесь,
# не решён кодом.
#
# `extract_ad_hoc_metric`/`calculate_ad_hoc_metric` распознаются моделью по
# формулировке (концепт явно не среди кандидатов, показанных модели), но
# РАЗРЕШАЮТСЯ ли они фактически в реестр — по-прежнему решает уже
# существующая, НЕИЗМЕНЁННАЯ проверка `_finalize_goal`
# (`skills/atoms/intent.py`): код есть среди `candidate_concepts` → `targets`,
# кода нет → `unresolved`, тот же путь, что раньше обслуживал любой незнакомый
# термин `point_metric`. Атомы `formula.infer`/`concept.infer`, которые
# довели бы ad hoc путь до реального извлечения/расчёта, — предмет отдельной
# сессии 18 (`26-...md`), не этого аудита.
#
# "ambiguous"/"unknown_concept" из `tests/golden/intent/queries.json` —
# по-прежнему не типы задачи, а СВОЙСТВА запроса (неоднозначность/незнакомый
# концепт), выражаемые через `unresolved`/`ambiguities`, а не через
# `intent_kind`.
#
# Retrofit v9 (docs/build-plan/26-arhitekturnaya-fiksaciya-v9.md, §1):
# каталог GoalType закрыт на РОВНО 13 значениях (02.10.2026 расширен до 14: добавлен
# `custom_analysis` — обработка сверх каталога атомов, всегда идёт в DynamicRun; в таблице
# соответствия выше у него нет пары — GoalType v9 он не принадлежит). Двенадцать перечисленных выше
# уже были в реализации; тринадцатое — `monitor_changes` (↔ `MONITOR_CHANGES`).
# Срез 11 подключает его к обычному графу через `root_from_goal` и
# `Target(output_type="MonitorResult")`; остальные неподключённые виды по-прежнему
# fail closed в `_NOT_YET_WIRED_KINDS` (`skills/atoms/intent.py`).
IntentKind = Literal[
    "full_analysis",
    "extract_registered_metric",
    "extract_ad_hoc_metric",
    "calculate_registered_metric",
    "calculate_ad_hoc_metric",
    "extract_structure",
    "document_qa",
    "compare_metrics",
    "explain_change",
    "validate_report",
    "plan_fact_analysis",
    "monitor_changes",
    "form",
    "custom_analysis",
]


class ClassificationResult(Contract):
    """Профиль документа: (стандарт, тип компании, язык).

    Форма зеркалит `finengine.skills.extractors._classifier.Profile` —
    существующий рабочий классификатор (`detect_profile`), который эта
    сессия переиспользует, а не заменяет (02-tekushee-sostoyanie, §2.1).
    """

    profile_id: NonEmptyStr
    standard: NonEmptyStr
    company_type: NonEmptyStr
    language: NonEmptyStr
    confidence_low: bool = False
    reason: str = ""
    signals: dict = Field(default_factory=dict)
    axis_confidence_low: dict[str, bool] = Field(default_factory=dict)
    # Срез 1 (id.frame, B1): по оси "user" — пользователь явно переопределил
    # её (standard_override/company_type_override), "classifier" или
    # отсутствие ключа — определил детерминированный классификатор.
    override_source: dict[str, str] = Field(default_factory=dict)
    # Отрасль нефинансовой компании (manufacturing/retail/...) — финансовый
    # контекст, а не технический тип PDF (`doc.probe` эту ось не трогает).
    # Пусто, если отраслевых признаков в тексте недостаточно.
    industry: str = ""
    # Заказчик не определяется из финансового документа. Это уже известный
    # контекст запуска, по которому после id.frame выбирается последний слой
    # канонического реестра (Срез 13); пусто = только общие слои.
    customer_id: str = ""


# Срез 4: те же три формы результата, которые уже раскрывает реальный граф
# (`skills/atoms/graph_atoms.py::build_registry` — "scalar" = extract.scalar/
# calc.formula_engine напрямую как корень, "bundle" = analyze.full_bundle,
# "form" = out.form). Закрыто намеренно: пятая форма результата не
# появляется молча — под неё нет атома-производителя, и `GraphPlanner` такой
# корень пометит `Unreachable`, а не то, что `intent.parse` тихо её выдумает.
OutputKind = Literal["scalar", "bundle", "form"]


class TargetNode(Contract):
    """Один явно названный показатель внутри `GoalIR.targets` — то, что модель
    сопоставила со словарём кандидатов в свободном тексте запроса (`intent.
    parse`), не исполняемая planning-семантика.

    Retrofit v9 (R3, `RETROFIT-REPORT-v9.md`): корнем `GraphPlanner.plan()`
    является `Target` (`skills/spec/target.py`), не `TargetNode` — `Target`
    несёт `output_type`, различающий `ExtractedMetric`/`CalculatedMetric` для
    одного и того же `concept`, чего `output_kind` здесь не выражает.
    `TargetFactory` (`skills/atoms/intent.py::root_from_goal`) строит `Target`
    ИЗ `TargetNode`, не наоборот."""

    target_id: str = Field(default_factory=new_id)
    concept: NonEmptyStr
    period_ref: str | None = None
    entity_ref: str | None = None
    output_kind: OutputKind


class AmbiguityNote(Contract):
    """Один неоднозначный термин запроса (карточка intent.parse: «неоднозначный
    термин не угадывается» — минимальный тестовый сценарий Среза 4).

    `candidates` — закрытые кандидаты из того же ограниченного набора, что
    получила модель (`find_metrics`/роли/периоды), а не свободный текст: если
    неоднозначность не сводится к перечислимым кандидатам, она превращается в
    `GoalIR.unresolved`, а не в `AmbiguityNote` с пустым `candidates`.
    """

    term: NonEmptyStr
    candidates: list[str] = Field(default_factory=list)
    reason: str = ""


class CorrectionIntent(Contract):
    """Явная пользовательская поправка, распознанная `intent.parse` (Срез 10).

    На этом слое `evidence_id` может быть неизвестен: точное связывание
    выполняет owner-scoped Case по concept/period/unit и при неоднозначности
    возвращает ChoicePoint, а не выбирает один факт эвристикой.
    """

    action: Literal["apply", "cancel"] = "apply"
    concept: str | None = None
    evidence_id: str | None = None
    value: float | None = None
    unit: str | None = None
    period_ref: str | None = None
    basis: str | None = None
    reason: str = ""
    original_wording: NonEmptyStr


class GoalClause(Contract):
    """One independently plannable clause of a compound user request.

    ``goal_ref`` is local to the request and is the stable handle used by
    ``depends_on``.  The model may describe semantic dependencies, but it never
    names atoms or execution steps; those remain the planner's responsibility.
    """

    goal_ref: NonEmptyStr
    raw_query: NonEmptyStr
    intent_kind: IntentKind
    targets: list[TargetNode] = Field(default_factory=list)
    entity_ref: str | None = None
    period_ref: str | None = None
    role: str | None = None
    parameters: dict = Field(default_factory=dict)
    unresolved: list[str] = Field(default_factory=list)
    ambiguities: list[AmbiguityNote] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


class GoalIR(Contract):
    """Результат `intent.parse`: запрос пользователя, переведённый в цели.

    `targets` — по одному `TargetNode` на КАЖДЫЙ явно названный и
    детерминированно подтверждённый показатель, только для
    `intent_kind="extract_registered_metric"`/`"calculate_registered_metric"`
    (карточка Среза 4: «После модельного ответа детерминированная проверка
    подтверждает существование концептов»); может быть больше одного
    (`intent-090` золотого набора — «два конкретных показателя... всё ещё
    точечный запрос», не `full_analysis`). Для остальных значений
    `intent_kind` (включая `*_ad_hoc_metric` — концепт там по определению не
    среди кандидатов) список ПУСТОЙ: A1.5 прямо запрещает `GoalIR` содержать
    «список показателей, которые якобы составляют полный анализ» — какой
    именно граф раскрыть за `EvidenceBundleV2`/`FormOutput`, решает
    `GraphPlanner` по `RunPassport` (роли/реестру), не `intent.parse`. Сам
    синтетический корень для `full_analysis`/`form` строит `root_from_goal`
    (`skills/atoms/intent.py`) по одному `intent_kind` — `intent.parse` не
    обязан знать имя графового концепта (`"ReportOutput"`), это деталь
    `AtomRegistry`, а не намерения. Для остальных значений `intent_kind` из
    каталога 11 `goal_type`, у которых пока нет собственного producer'а в
    графе, `root_from_goal` явно поднимает `NotImplementedError` — они уже
    распознаются `intent.parse`, но их разворот в `PlanGraph` — предмет
    будущих срезов.

    Каждый `TargetNode` в `targets` несёт `entity_ref`/`period_ref` пустыми:
    единственное значение на весь запрос живёт здесь, в `GoalIR.entity_ref`/
    `.period_ref`, и `root_from_goal` копирует его на КАЖДЫЙ корень, который
    строит, — если бы значение дублировалось на каждом `TargetNode` уже здесь,
    у одного запроса было бы N мест, где может незаметно разойтись одна и та
    же сущность/период (методология C4 против необъявленных вторых источников
    истины).

    `entity_ref`/`period_ref`/`role` — то, что модель извлекла из СВОБОДНОГО
    текста запроса (имя компании, период, роль заказчика), не идентификаторы
    после разрешения (`EntityIdentity`/`PeriodAxis` — более поздние срезы).
    """

    goal_id: str = Field(default_factory=new_id)
    raw_query: NonEmptyStr
    intent_kind: IntentKind
    targets: list[TargetNode] = Field(default_factory=list)
    entity_ref: str | None = None
    period_ref: str | None = None
    role: str | None = None
    parameters: dict = Field(default_factory=dict)
    unresolved: list[str] = Field(default_factory=list)
    ambiguities: list[AmbiguityNote] = Field(default_factory=list)
    correction: CorrectionIntent | None = None
    # Backward-compatible compound intent: the fields above are the primary
    # clause; every additional GoalType is an independently plannable clause.
    goal_ref: NonEmptyStr = "goal-1"
    depends_on: list[str] = Field(default_factory=list)
    additional_goals: list[GoalClause] = Field(default_factory=list)

    def clauses(self) -> list[GoalClause]:
        """Return all request clauses in deterministic declaration order."""
        primary = GoalClause(
            goal_ref=self.goal_ref, raw_query=self.raw_query, intent_kind=self.intent_kind, targets=self.targets,
            entity_ref=self.entity_ref, period_ref=self.period_ref, role=self.role,
            parameters=self.parameters, unresolved=self.unresolved,
            ambiguities=self.ambiguities, depends_on=self.depends_on,
        )
        return [primary, *self.additional_goals]


class AdHocConceptSpec(Contract):
    """Временное определение показателя вне `AtomRegistry`/canonical registry
    (R4, retrofit v9) — для `extract_ad_hoc_metric`: пользователь однозначно
    назвал показатель, которого нет среди зарегистрированных концептов.

    Живёт только в `Case` текущего запроса (`TargetFactory` создаёт его из
    `GoalIR`, не из отдельного `concept.infer` atom — эта семантическая работа
    относится к intent/definition resolution, не к `AtomRegistry`). Не
    записывается в реестр автоматически."""

    # Срез 31 (П5): семантику ad-hoc показателя придумывает МОДЕЛЬ из своих
    # знаний — имя, синонимы и определение, по которым двухстадийный экстрактор
    # (`locate` → `read.value`) ищет его в отчёте. Поля зеркалят строку реестра
    # (`name_ru`/`name_en`/`aliases_*`/`description`/`hint`), чтобы экстрактор
    # получал ровно ту же форму, что и для зарегистрированного показателя.
    # Все поля необязательны: старые сохранённые спецификации остаются валидны.
    spec_id: str = Field(default_factory=new_id)
    term: NonEmptyStr
    assumptions: str = ""
    origin: Literal["ad_hoc"] = "ad_hoc"
    # `key` — идентификатор понятия в графе (`Target.concept_id`/символ формулы);
    # пусто = `term`. Для компонентов формулы — ASCII `[A-Z][A-Z0-9_]*`.
    key: str = ""
    name_ru: str = ""
    name_en: str = ""
    aliases_ru: list[str] = Field(default_factory=list)
    aliases_en: list[str] = Field(default_factory=list)
    definition: str = ""
    hint: str = ""
    value_kind: Literal["flow", "stock", "ratio", "other"] = "other"

    @property
    def concept_key(self) -> str:
        return self.key or self.term


class AdHocFormulaSpec(Contract):
    """Временная формула вне реестра — для `calculate_ad_hoc_metric`.
    `expression`/`dependencies` описывают, как посчитать показатель, но НЕ
    несут числового результата — это по-прежнему работа `calc.formula_engine`
    по данной спецификации, не работа intent-слоя.

    Срез 31 (П4): формулу и её состав выдаёт МОДЕЛЬ из знаний, если пользователь
    их не назвал. `components` описывают КАЖДЫЙ символ выражения: код реестра
    (если понятие есть в реестре профиля) либо ad-hoc понятие с синонимами и
    определением — его извлекает двухстадийный экстрактор."""

    spec_id: str = Field(default_factory=new_id)
    term: NonEmptyStr
    expression: str = ""
    dependencies: list[str] = Field(default_factory=list)
    assumptions: str = ""
    origin: Literal["ad_hoc"] = "ad_hoc"
    name_ru: str = ""
    name_en: str = ""
    definition: str = ""
    unit: str = ""
    components: list[AdHocConceptSpec] = Field(default_factory=list)
