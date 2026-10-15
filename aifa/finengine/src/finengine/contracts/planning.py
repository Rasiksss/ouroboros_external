"""Область «планирование»: раскрытие целевой вершины в граф атомов.

`GraphPlanner` строит граф от корня вниз к листьям (02, §3). Фаза 0 завела
здесь только сами структуры (`NodeKey`/`PlanNode`/`PlanGraph`/`PlanFeasibility`);
`PlanPolicy`, `Unreachable`, `ChoicePoint`, `InvalidGoal` и `PlanBuildResult`
добавлены в Срезе 3 (A1.3) — ровно те типы, которые сам A1.3 называет выходом
планировщика: «готовый или частично раскрытый `PlanGraph`, а также
формализованные состояния `ChoicePoint`, `Unreachable` либо `InvalidGoal`».
"""
from __future__ import annotations

from pydantic import Field

from .base import Contract, NonEmptyStr


class PlanFeasibility(Contract):
    """Итог проверки корректности `GoalIR`/графа перед исполнением."""

    feasible: bool
    unreachable_targets: list[str] = Field(default_factory=list)
    reason: str | None = None


class NodeKey(Contract):
    """Идентичность узла плана: какой атом, какой версии, с какими входами."""

    atom_name: NonEmptyStr
    atom_version: NonEmptyStr
    args_fingerprint: NonEmptyStr


class PlanNode(Contract):
    """Один узел графа: его зависимости, что он производит, в каком профиле песочницы."""

    node_key: NodeKey
    requires: list[NodeKey] = Field(default_factory=list)
    produces: list[str] = Field(default_factory=list)
    sandbox_profile: NonEmptyStr
    # Compound requests may share a node between scenarios.  Empty means a
    # legacy/single-goal graph; otherwise the UI can show exact membership.
    scenario_refs: list[str] = Field(default_factory=list)


class PlanScenario(Contract):
    """A user-visible subgraph within one executable compound DAG."""

    goal_ref: NonEmptyStr
    goal_type: NonEmptyStr
    root: NodeKey
    depends_on: list[str] = Field(default_factory=list)


class PlanGraph(Contract):
    """Весь раскрытый граф для одной корневой вершины `TargetNode`.

    `planner_version` добавлено в Срезе 3 (A1.9: «`PlanGraph` хранит
    `planner_version` и канонический хэш»); значение по умолчанию делает поле
    обратно совместимым с уже существующими golden-фикстурами Фазы 0
    (`tests/golden/planner/manual/*.json`), которые были написаны раньше
    этого требования и его не несут."""

    plan_id: NonEmptyStr
    root: NodeKey
    nodes: list[PlanNode]
    graph_hash: NonEmptyStr
    planner_version: NonEmptyStr = "1.0.0"
    scenarios: list[PlanScenario] = Field(default_factory=list)


class PlanPolicy(Contract):
    """Версионированные правила выбора производителя (A1.6).

    `producer_priority` — concept → упорядоченный список `atom_name`,
    которым `GraphPlanner` разрешает молча выбрать производителя, когда их
    несколько достижимо. Пустой список кандидатов для концепта = решение №7
    («несколько производителей») отдаётся `ChoicePoint`, а не первому
    зарегистрированному атому — так A1.6 прямо запрещает угадывать."""

    policy_version: NonEmptyStr
    producer_priority: dict[str, list[str]] = Field(default_factory=dict)
    # Retrofit v9 (R3, A1.4 «cost model v1»): вес одного НОВОГО узла по его
    # `AtomDef.cost_class` — версionируемые данные политики, не хардкод внутри
    # `ProducerResolver`. Отсутствующий в словаре `cost_class` весит 1 (см.
    # `GraphPlanner._incremental_cost`).
    cost_weights: dict[str, int] = Field(
        default_factory=lambda: {"free": 1, "cheap": 2, "model_call": 5}
    )


class Unreachable(Contract):
    """Одна ветвь раскрытия, которая не смогла закончиться доступным листом
    (A1.4, шаг 5): концепт запрошен, но в `AtomRegistry` нет ни одного атома,
    объявляющего его в `produces`."""

    concept: NonEmptyStr
    reason: NonEmptyStr
    requested_by: NonEmptyStr


class ChoicePoint(Contract):
    """Концепт достижим несколькими путями, и `PlanPolicy` не называет
    приоритет (A1.6): решение не принимается молча первым зарегистрированным
    атомом, а откладывается на слой оркестрации."""

    concept: NonEmptyStr
    candidate_atoms: list[str]
    reason: NonEmptyStr


class InvalidGoal(Contract):
    """`GoalIR`/`TargetNode` на входе `GraphPlanner` структурно непригоден
    для раскрытия (например, корень без `concept`/`output_kind`) — это не
    `Unreachable` (граф корректен, но недостижим), а брак входа."""

    reason: NonEmptyStr


class PlanBuildResult(Contract):
    """Результат одного вызова `GraphPlanner.plan(...)` (A1.3/A1.5).

    `graph` присутствует всегда — даже нераскрываемый корень остаётся в
    графе одним узлом без производителя (ср. golden-фикстур
    `planner-05-unreachable-target`). `pending_run_passport=True` — это
    первый проход двухпроходного раскрытия (A1.5): `graph` тут покрывает
    только идентификационную часть, и раскрытие ТОЙ ЖЕ корневой вершины
    обязано продолжиться повторным вызовом `plan(...)` с уже известным
    `RunPassport`, а не постройкой нового плана."""

    graph: PlanGraph
    feasibility: PlanFeasibility
    unreachable: list[Unreachable] = Field(default_factory=list)
    choice_points: list[ChoicePoint] = Field(default_factory=list)
    invalid_goal: InvalidGoal | None = None
    pending_run_passport: bool = False
    # Retrofit v9 (R3, A1.7 «Optionality»): члены необязательной коллекции
    # (полный анализ/форма роли), которые не нашли достижимого производителя,
    # НЕ становятся nullable-рёбрами в графе и НЕ портят `feasibility.feasible`
    # — политика полноты root result вместо optional-requires. Каждая запись
    # здесь — "эта ветка отсутствует в этом Target/scope", ортогонально
    # `unreachable` (который по-прежнему обязателен для настоящих required
    # входов и обязан гейтить `feasible=False`).
    incomplete_scope: list[Unreachable] = Field(default_factory=list)
