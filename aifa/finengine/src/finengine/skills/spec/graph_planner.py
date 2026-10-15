"""`GraphPlanner` — детерминированный графовый планировщик (A1, docs/build-plan/
13-srez-03-graph-planner-executor.md, retrofit v9 — `RETROFIT-REPORT-v9.md`,
раздел R3). Строит `PlanGraph` от ОДНОЙ корневой вершины (`Target`) вниз к
доступным листьям (A1.3/A1.4): не `intent.contract`, не `update_plan`, не
ручной порядок `steps:` — эти пути читают этот же механизм
(`AtomRegistry`/`PlanPolicy`), не дублируют его.

Направление построения — обратное (A1.4): планировщик спрашивает «что нужно,
чтобы получить корень», а не «что можно сделать с тем, что есть». `_resolve()`
ниже — один и тот же порядок для каждой требуемой пары `(output_type,
concept)`: доступный лист → иначе производитель(и) из `AtomRegistry`
(reachability-first, затем minimum incremental cost, затем policy tie-break,
иначе `ChoicePoint`) → иначе `Unreachable`.

Retrofit v9 (R3) заменил `TargetNode` на `Target` (`target.py`): `ExtractedMetric
(EBITDA)` и `CalculatedMetric(EBITDA)` — теперь разные `Target` с разными,
единственными producer'ами, а не один `Target(concept=EBITDA)`, разрешаемый
через `PlanPolicy` tie-break между `read.value`/`calc.formula_engine`. Тот же
retrofit убрал `optional_requires` как базовую семантику `_visit`/`_resolve` —
необязательные коллекции (`analyze.full_bundle`/`out.form`) раскрываются
ИЗОЛИРОВАННЫМ пробным проходом (`_try_visit_isolated`), не параметром
`optional=True` на обычном пути обязательных `requires`.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields

from pydantic import BaseModel

from finengine.contracts import (
    ChoicePoint,
    NodeKey,
    PlanBuildResult,
    PlanFeasibility,
    PlanGraph,
    PlanNode,
    PlanScenario,
    PlanPolicy,
    RunPassport,
    Unreachable,
)
from finengine.skills.spec.atom_registry import (
    RUN_PASSPORT_CONCEPT,
    AtomDef,
    AtomRegistry,
    NodeArgs,
    _norm_input_specs,
    fingerprint,
)
from finengine.skills.spec.target import InputSpec, OutputSpec, Target

PLANNER_VERSION = "1.0.0"
# Версия плана, собранного агентом-планировщиком (Срез 31, П8): по ней UI и журнал
# отличают динамический план от детерминированного; входит в хэш графа.
DYNAMIC_PLANNER_VERSION = "dynamic-1.0.0"


@dataclass(frozen=True)
class AtomStep:
    """Один шаг плана агента: вызов ОДНОГО атома с аргументами. `output_type` —
    какой из типов, которые производит атом, этот шаг должен дать."""

    atom: str
    output_type: str
    concept: str | None = None
    period_ref: str | None = None
    extra: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AvailableLeaf:
    """Узел, который планировщику НЕ нужно производить — он уже есть среди
    входов запуска или в `Case` (A1.4, шаг 2-3): загруженный документ,
    параметр, ранее подтверждённое доказательство. `node_key` даёт узлу
    идентичность в графе (он всё равно попадает в `PlanGraph.nodes`, просто
    без атома-производителя выше него), `evidence_ids` — то, чем
    `GraphExecutor` заранее засевает `NodeRun` как `succeeded`, не запуская
    исполнение (Срез 3, A2)."""

    node_key: NodeKey
    evidence_ids: tuple[str, ...] = ()
    evidence: tuple[BaseModel, ...] = ()


class AvailableIndex:
    """Индекс уже доступных узлов, по значимым признакам идентичности
    (A1.4, шаг 2: «не только по типу, но и по документу/версии, сущности,
    концепту, периоду, базису»). Точное совпадение по кортежу — никаких
    подстановочных знаков: концепт того же имени для другой сущности/периода
    — ДРУГОЙ узел (A1.2). Retrofit v9: ключ расширен `output_type`'ом — тот
    же принцип, каким `AtomRegistry.producers_of` теперь ищет производителей.

    Единственное семантическое уточнение: лист с ``period_ref=None`` означает
    «все доступные периоды» и может удовлетворить более узкий запрос. Сам набор
    значений отфильтрует evidence gate; остальные оси ключа остаются точными."""

    def __init__(self) -> None:
        self._entries: dict[tuple, AvailableLeaf] = {}

    @staticmethod
    def _key(
        output_type: str, concept: str | None, *, entity_ref: str | None, period_ref: str | None, basis: str | None,
        document_version: str | None, policy_version: str | None,
    ) -> tuple:
        return (output_type, concept, entity_ref, period_ref, basis, document_version, policy_version)

    def put(
        self, output_type: str, leaf: AvailableLeaf, *,
        concept: str | None = None, entity_ref: str | None = None, period_ref: str | None = None,
        basis: str | None = None, document_version: str | None = None, policy_version: str | None = None,
    ) -> None:
        self._entries[self._key(
            output_type, concept, entity_ref=entity_ref, period_ref=period_ref, basis=basis,
            document_version=document_version, policy_version=policy_version,
        )] = leaf

    def lookup(
        self, output_type: str, concept: str | None = None, *,
        entity_ref: str | None = None, period_ref: str | None = None, basis: str | None = None,
        document_version: str | None = None, policy_version: str | None = None,
    ) -> AvailableLeaf | None:
        exact = self._entries.get(self._key(
            output_type, concept, entity_ref=entity_ref, period_ref=period_ref, basis=basis,
            document_version=document_version, policy_version=policy_version,
        ))
        if exact is not None:
            return exact
        if policy_version is not None:
            # Run/input leaves created before Slice 10 have no policy stamp.
            # They remain valid inputs; persisted Case artifacts DO carry a
            # policy version and therefore normally match the branch above.
            exact = self._entries.get(self._key(
                output_type, concept, entity_ref=entity_ref, period_ref=period_ref, basis=basis,
                document_version=document_version, policy_version=None,
            ))
            if exact is not None:
                return exact
        if period_ref is None:
            return None
        all_periods = self._entries.get(self._key(
            output_type, concept, entity_ref=entity_ref, period_ref=None, basis=basis,
            document_version=document_version, policy_version=policy_version,
        ))
        if all_periods is not None or policy_version is None:
            return all_periods
        return self._entries.get(self._key(
            output_type, concept, entity_ref=entity_ref, period_ref=None, basis=basis,
            document_version=document_version, policy_version=None,
        ))

    def all_leaves(self) -> tuple[AvailableLeaf, ...]:
        """Все зарегистрированные листья — `GraphExecutor` (A2) засевает по
        ним `NodeRun` как `succeeded` сразу, без вызова `executor`."""
        return tuple(self._entries.values())

    def entries(self) -> tuple[tuple[tuple, AvailableLeaf], ...]:
        """Compatibility descriptors together with leaves (Slice 10 persistence)."""
        return tuple(self._entries.items())


def _leaf_belongs_to_step(leaf: AvailableLeaf, atom: AtomDef) -> bool:
    """Whether a leaf came from an explicit step or one of its mandatory gates."""
    producers = []
    for value in leaf.evidence:
        producer = getattr(value, "produced_by_atom", None)
        if producer is None:
            for field in ("scalar_fact", "derived_fact", "human_override"):
                nested = getattr(value, field, None)
                producer = getattr(nested, "produced_by_atom", None) if nested is not None else None
                if producer is not None:
                    break
        if producer is not None:
            producers.append(str(producer))
    allowed = {atom.name, *atom.mandatory_gates}
    return bool(producers) and all(producer in allowed for producer in producers)


class _PlannerCycle(Exception):
    """Внутренний сигнал: узел, ещё раскрывающийся на стеке вызовов, снова
    запрошен как своя же зависимость (A1.4, шаг 11). Не долетает наружу
    `plan()` — там превращается в `feasible=False` с указанием на цикл."""

    def __init__(self, node_key: NodeKey):
        self.node_key = node_key
        super().__init__(f"cycle at {node_key.atom_name}({node_key.args_fingerprint})")


def _produces_for(atom: AtomDef, output_type: str, concept: str | None) -> list[str]:
    """Что ИМЕННО производит этот конкретный узел, не вся способность атома.

    `read.value` в реестре объявляет `produces=(...десятки OutputSpec...)` —
    это капитал атома, а не то, что вернёт ОДИН вызов с конкретным `concept`.
    Вызов, раскрытый ради `EBITDA`, обязан нести `produces=["ExtractedMetric:
    EBITDA"]`, а не список из сотни показателей, которых этот узел не считал.
    Атомы с фиксированным неизменным выходом (`identify.classify`, всегда
    производящий `RunPassport`) продолжают нести полный список — для них
    `concept` просто не участвует в матче вовсе.

    R4/R5a: ad-hoc `concept` (вне реестра) матчится не точной записью, а
    catch-all `OutputSpec(concept=None)` атома (`AtomRegistry.
    _producers_of_single`) — ярлык узла всё равно называет ФАКТИЧЕСКИЙ
    concept, не всю капитал атома.

    `output_type="MetricEvidence"` (union-alias, не настоящий `OutputSpec` ни
    одного атома) — ищем по concept СРЕДИ ЛЮБОГО output_type атома, не по
    точному `(output_type, concept)`: `calc.formula_engine`/`read.value`
    объявляют `CalculatedMetric`/`ExtractedMetric`, никогда `MetricEvidence`
    буквально."""
    if concept is not None:
        for spec in atom.produces:
            if spec.output_type == output_type and spec.concept == concept:
                return [spec.label()]
        if output_type == "MetricEvidence":
            for spec in atom.produces:
                if spec.concept == concept:
                    return [spec.label()]
        for spec in atom.produces:
            if spec.output_type == output_type and spec.concept is None:
                return [OutputSpec(output_type=output_type, concept=concept).label()]
    return [spec.label() for spec in atom.produces]


def _node_args_difference(prior: NodeArgs, value: NodeArgs) -> str:
    """Какие поля двух `NodeArgs` одного `NodeKey` разошлись: без этого конфликт в `plan_many` не разобрать."""
    parts = [
        f"{item.name}: {getattr(prior, item.name)!r} / {getattr(value, item.name)!r}"
        for item in fields(NodeArgs) if getattr(prior, item.name) != getattr(value, item.name)
    ]
    return "; ".join(parts)


def _target_label(output_type: str, concept: str | None) -> str:
    return OutputSpec(output_type=output_type, concept=concept).label()


def _run_passport_significant_fields(passport: RunPassport | None) -> dict:
    """«Значимые поля `RunPassport`» (A1.9) — те, что реально меняют состав
    графа ниже (профиль, сущность, версии реестра/методики). НЕ `run_id`/
    `created_at`: два прогона одного и того же отчёта с одной и той же
    классификацией обязаны раскрыться в тот же граф, а не разойтись только
    потому, что паспорта выпущены в разные секунды."""
    if passport is None:
        return {}
    return {
        "profile_id": passport.classification.profile_id,
        "company_type": passport.classification.company_type,
        "standard": passport.classification.standard,
        "language": passport.classification.language,
        "entity_id": passport.entity.entity_id,
        "registry_version": passport.registry_version,
        "methodology_version": passport.methodology_version,
    }


def compute_graph_hash(
    *, root: NodeKey, nodes: Sequence[PlanNode], registry_fingerprint: str,
    policy_version: str, run_passport: RunPassport | None, planner_version: str = PLANNER_VERSION,
) -> str:
    """Канонический хэш плана (A1.9): функция корня, версий реестра/политики,
    значимых полей паспорта и ПОЛНОСТЬЮ раскрытой структуры зависимостей.
    Узлы сортируются перед хэшированием — порядок обхода реестра/раскрытия не
    должен влиять на итоговый хэш (A1.11.1: «канонически одинаковый... в 100%
    из 1000 генеративных прогонов»; краевой случай 42 из 99-matrica.md)."""
    payload = {
        "planner_version": planner_version,
        "root": (root.atom_name, root.atom_version, root.args_fingerprint),
        "registry_fingerprint": registry_fingerprint,
        "policy_version": policy_version,
        "run_passport": _run_passport_significant_fields(run_passport),
        "nodes": sorted(
            (
                (n.node_key.atom_name, n.node_key.atom_version, n.node_key.args_fingerprint),
                tuple(sorted((r.atom_name, r.atom_version, r.args_fingerprint) for r in n.requires)),
                tuple(sorted(n.produces)),
                n.sandbox_profile,
            )
            for n in nodes
        ),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


@dataclass
class _BuildState:
    nodes: dict[NodeKey, PlanNode] = field(default_factory=dict)
    # `NodeArgs` за каждым узлом — не часть `PlanGraph` (контракт хранит только
    # опаковую строку `args_fingerprint`, A1.2), но `GraphExecutor` (этот же
    # срез, A2) обязан знать структурные поля узла, чтобы вызвать исполнителя
    # атома и сгруппировать узлы в `BatchKey`. `plan_ex()` ниже отдаёт эту
    # карту наружу отдельно от сериализуемого `PlanBuildResult`.
    node_args: dict[NodeKey, NodeArgs] = field(default_factory=dict)
    visiting: set[NodeKey] = field(default_factory=set)
    unreachable: list[Unreachable] = field(default_factory=list)
    choice_points: list[ChoicePoint] = field(default_factory=list)
    pending_run_passport: bool = False
    # Retrofit v9 (R3, A1.7): ветки необязательной коллекции, которые не
    # достигли производителя — НЕ портят `unreachable`/`feasible` родителя.
    incomplete_scope: list[Unreachable] = field(default_factory=list)
    # Срез 6a — исправление реальной дыры в обходе гейтов (см. докстринг
    # `_expand_candidate`): «сырой» `NodeKey` атома-производителя → итоговый
    # `NodeKey` ПОСЛЕ применения его `mandatory_gates` (совпадают, если гейтов
    # нет). Без этой карты второй, независимый запрос ТОГО ЖЕ производителя
    # (например `read.value|X`, нужный одновременно и `calc.formula`, и
    # `analyze.full_bundle` РАЗНЫМИ путями раскрытия) находил бы узел уже
    # существующим в `state.nodes` и возвращал бы его СЫРОЙ ключ напрямую,
    # обходя `norm.unit`/`norm.time`/`verify.bounds` — обязательную проверку
    # можно было бы обойти, просто попросив уже раскрытый концепт вторым
    # путём.
    gated_key_for: dict[NodeKey, NodeKey] = field(default_factory=dict)


class GraphPlanner:
    """Раскрывает одну корневую вершину в `PlanGraph` (A1.3–A1.9). Без
    состояния между вызовами — каждый `plan()` независим; двухпроходное
    раскрытие (A1.5) — это ДВА вызова `plan()` с тем же `root`, второй раз с
    уже известным `run_passport`, не отдельный метод: узлы с одинаковым
    `NodeKey` дедуплицируются одинаково в обоих случаях, так что второй
    вызов естественно достраивает то, что первый оставил `pending_run_
    passport=True`."""

    def __init__(self, *, registry: AtomRegistry, policy: PlanPolicy):
        self.registry = registry
        self.policy = policy

    def plan(
        self, root: Target, *, available: AvailableIndex | None = None,
        run_passport: RunPassport | None = None, plan_id: str | None = None,
        root_extra: Mapping[str, str] | None = None,
    ) -> PlanBuildResult:
        return self.plan_ex(
            root, available=available, run_passport=run_passport, plan_id=plan_id, root_extra=root_extra,
        )[0]

    def plan_ex(
        self, root: Target, *, available: AvailableIndex | None = None,
        run_passport: RunPassport | None = None, plan_id: str | None = None,
        root_extra: Mapping[str, str] | None = None,
    ) -> tuple[PlanBuildResult, dict[NodeKey, NodeArgs]]:
        """То же построение, что `plan()`, плюс карта `NodeKey → NodeArgs` для
        каждого узла — вход `GraphExecutor.run()` (A2). `plan()` — тонкая
        обёртка поверх этого метода, не вторая реализация."""
        available = available or AvailableIndex()
        state = _BuildState()
        root_label = _target_label(root.output_type, root.concept_id)

        root_args = NodeArgs(
            concept=root.concept_id, entity_ref=root.entity_ref, period_ref=root.period_ref, basis=root.basis,
            document_version=root.document_version, policy_version=root.policy_version or self.policy.policy_version,
            extra=dict(root_extra or {}),
        )
        try:
            root_key = self._resolve(
                root.output_type, root.concept_id, entity_ref=root.entity_ref, period_ref=root.period_ref,
                basis=root.basis, document_version=root.document_version,
                policy_version=root.policy_version or self.policy.policy_version,
                extra=dict(root_extra or {}), requested_by="root",
                available=available, state=state, run_passport=run_passport,
            )
        except _PlannerCycle as cyc:
            feasibility = PlanFeasibility(
                feasible=False, unreachable_targets=[root_label],
                reason=f"цикл зависимостей на узле {cyc.node_key.atom_name}({cyc.node_key.args_fingerprint})",
            )
            # Цикл делает граф, построенный ДО его обнаружения, недостоверным
            # целиком — Definition of Done (A1.11.7) требует ловить цикл ДО
            # запуска `GraphExecutor`, не отдавать частично раскрытый граф.
            graph = PlanGraph(
                plan_id=plan_id or f"plan-{root_label}",
                root=NodeKey(atom_name="unresolved.cycle", atom_version="0.0.0",
                              args_fingerprint=f"target={root_label}"),
                nodes=[], graph_hash="sha256:cycle-" + root_label,
                planner_version=PLANNER_VERSION,
            )
            return PlanBuildResult(graph=graph, feasibility=feasibility), {}

        if root_key is None:
            # Корень сам недостижим (нет производителя) — golden-фикстур
            # planner-05: узел заведён, но без requires/produces.
            root_key = NodeKey(
                atom_name="unresolved.target", atom_version="0.0.0",
                args_fingerprint=fingerprint(root_args, ("concept", "entity_ref", "period_ref", "basis", "document_version", "policy_version")),
            )
            state.nodes[root_key] = PlanNode(
                node_key=root_key, requires=[], produces=[], sandbox_profile="compute_no_network",
            )
            state.node_args[root_key] = root_args

        return self._assemble(
            root_key=root_key, state=state, run_passport=run_passport, plan_id=plan_id, root_label=root_label,
        )

    def plan_many(
        self,
        requests: Sequence[tuple[Target, Mapping[str, str], str, str, Sequence[str]]],
        *, available: AvailableIndex | None = None, run_passport: RunPassport | None = None,
        plan_id: str | None = None,
    ) -> tuple[PlanBuildResult, dict[NodeKey, NodeArgs]]:
        """Compose several semantic roots into one executable scenario DAG.

        Each request is ``(target, root_extra, goal_ref, goal_type, depends_on)``.
        Normal planning remains isolated per root, then identical ``NodeKey``
        nodes are deduplicated.  Declared goal dependencies become edges
        between scenario roots; independent roots share only the synthetic
        ``goal.join`` fan-in and therefore become ready concurrently.
        """
        if not requests:
            raise ValueError("plan_many requires at least one request")
        available = available or AvailableIndex()
        refs = [item[2] for item in requests]
        if len(set(refs)) != len(refs):
            raise ValueError("compound plan goal_ref values must be unique")
        known = set(refs)
        deps_by_ref = {item[2]: tuple(item[4]) for item in requests}
        if any(dep not in known or dep == ref for ref, deps in deps_by_ref.items() for dep in deps):
            raise ValueError("compound plan has an unknown or self dependency")

        # Topological validation before any graph is returned.
        visiting: set[str] = set()
        visited: set[str] = set()
        def visit(ref: str) -> None:
            if ref in visiting:
                raise ValueError(f"compound plan dependency cycle at {ref!r}")
            if ref in visited:
                return
            visiting.add(ref)
            for dep in deps_by_ref[ref]:
                visit(dep)
            visiting.remove(ref)
            visited.add(ref)
        for ref in refs:
            visit(ref)

        builds: list[PlanBuildResult] = []
        args: dict[NodeKey, NodeArgs] = {}
        nodes: dict[NodeKey, PlanNode] = {}
        roots: dict[str, NodeKey] = {}
        for target, extra, goal_ref, _goal_type, _deps in requests:
            build, local_args = self.plan_ex(
                target, available=available, run_passport=run_passport,
                plan_id=f"{plan_id or 'compound'}:{goal_ref}", root_extra=extra,
            )
            builds.append(build)
            roots[goal_ref] = build.graph.root
            for key, value in local_args.items():
                prior = args.get(key)
                if prior is not None and prior != value:
                    raise ValueError(
                        f"same NodeKey has conflicting NodeArgs: {key} (расходятся {_node_args_difference(prior, value)})"
                    )
                args[key] = value
            for node in build.graph.nodes:
                prior = nodes.get(node.node_key)
                scenario_refs = sorted(set((prior.scenario_refs if prior else []) + [goal_ref]))
                if prior is not None and (prior.requires != node.requires or prior.produces != node.produces):
                    raise ValueError(f"same NodeKey has conflicting topology: {node.node_key}")
                nodes[node.node_key] = node.model_copy(update={"scenario_refs": scenario_refs})

        # A semantic dependency is both ordering and data flow: the downstream
        # root receives upstream root evidence in its ExecutionContext.
        for ref, dependencies in deps_by_ref.items():
            key = roots[ref]
            node = nodes[key]
            required = list(node.requires)
            for dep in dependencies:
                dep_root = roots[dep]
                # Two clauses can legitimately resolve to the same semantic
                # root. Dedup already satisfies the dependency; a self-edge
                # would turn that reuse into an artificial cycle.
                if dep_root != key and dep_root not in required:
                    required.append(dep_root)
            nodes[key] = node.model_copy(update={"requires": required})

        join_args = NodeArgs(extra={"scenario_refs": ",".join(refs)})
        join_key = NodeKey(
            atom_name="goal.join", atom_version="1.0.0",
            args_fingerprint=fingerprint(join_args, ("scenario_refs",)),
        )
        nodes[join_key] = PlanNode(
            node_key=join_key, requires=[roots[ref] for ref in refs],
            produces=["CompoundResult"], sandbox_profile="compute_no_network",
            scenario_refs=list(refs),
        )
        args[join_key] = join_args
        scenarios = [
            PlanScenario(goal_ref=ref, goal_type=goal_type, root=roots[ref], depends_on=list(deps))
            for _target, _extra, ref, goal_type, deps in requests
        ]
        all_nodes = list(nodes.values())
        planner_version = PLANNER_VERSION + "+compound"
        graph_hash = compute_graph_hash(
            root=join_key, nodes=all_nodes, registry_fingerprint=self.registry.fingerprint(),
            policy_version=self.policy.policy_version, run_passport=run_passport,
            planner_version=planner_version,
        )
        graph = PlanGraph(
            plan_id=plan_id or "plan-compound", root=join_key, nodes=all_nodes,
            graph_hash=graph_hash, planner_version=planner_version, scenarios=scenarios,
        )
        reasons = [b.feasibility.reason for b in builds if not b.feasibility.feasible]
        result = PlanBuildResult(
            graph=graph,
            feasibility=PlanFeasibility(
                feasible=not reasons, unreachable_targets=[] if not reasons else refs,
                reason="; ".join(r for r in reasons if r) or None,
            ),
            unreachable=[u for b in builds for u in b.unreachable],
            choice_points=[c for b in builds for c in b.choice_points],
            pending_run_passport=any(b.pending_run_passport for b in builds),
            incomplete_scope=[u for b in builds for u in b.incomplete_scope],
        )
        return result, args

    def _assemble(
        self, *, root_key: NodeKey, state: _BuildState, run_passport: RunPassport | None,
        plan_id: str | None, root_label: str, planner_version: str = PLANNER_VERSION,
    ) -> tuple[PlanBuildResult, dict[NodeKey, NodeArgs]]:
        """Хвост построения: узлы → `PlanGraph` с хэшем → `PlanFeasibility`. Общий
        для `plan_ex` (детерминированный корень) и `plan_with_atoms` (шаги агента)."""
        nodes = list(state.nodes.values())
        graph_hash = compute_graph_hash(
            root=root_key, nodes=nodes, registry_fingerprint=self.registry.fingerprint(),
            policy_version=self.policy.policy_version, run_passport=run_passport,
            planner_version=planner_version,
        )
        graph = PlanGraph(
            plan_id=plan_id or f"plan-{root_label}", root=root_key, nodes=nodes,
            graph_hash=graph_hash, planner_version=planner_version,
        )

        infeasible_reasons: list[str] = []
        if state.unreachable:
            infeasible_reasons.append(
                "недостижимые концепты: " + ", ".join(sorted({u.concept for u in state.unreachable}))
            )
        if state.choice_points:
            infeasible_reasons.append(
                "нерешённые точки выбора: " + ", ".join(sorted({c.concept for c in state.choice_points}))
            )
        feasibility = PlanFeasibility(
            feasible=not infeasible_reasons,
            unreachable_targets=[root_label] if infeasible_reasons else [],
            reason="; ".join(infeasible_reasons) or None,
        )
        result = PlanBuildResult(
            graph=graph, feasibility=feasibility, unreachable=state.unreachable,
            choice_points=state.choice_points, pending_run_passport=state.pending_run_passport,
            incomplete_scope=state.incomplete_scope,
        )
        return result, dict(state.node_args)

    def plan_with_atoms(
        self, steps: Sequence["AtomStep"], root_index: int, *, available: AvailableIndex | None = None,
        run_passport: RunPassport | None = None, plan_id: str | None = None,
        root_extra: Mapping[str, str] | None = None, entity_ref: str | None = None,
        document_version: str | None = None, planner_version: str = DYNAMIC_PLANNER_VERSION,
    ) -> tuple[PlanBuildResult, dict[NodeKey, NodeArgs]]:
        """План из ЯВНО названных атомов (динамический планировщик, Срез 31, П8).

        Агент выбирает, КАКИЕ атомы вызвать и с какими аргументами; всё остальное —
        входы по типам, обязательные гейты, паспорт, дедупликация одинаковых
        узлов — раскрывает тот же `ProducerResolver` (`_expand_candidate`), что и
        детерминированный путь. Поэтому агент не может ни обойти гейт, ни вызвать
        несуществующий вход: недостижимая ветвь честно попадает в `feasibility`.

        `root_index` — шаг, результат которого отвечает на запрос. Остальные
        шаги остаются в графе и исполняются (исполнитель запускает ВСЕ узлы).
        """
        available = available or AvailableIndex()
        state = _BuildState()
        policy_version = self.policy.policy_version
        keys: list[NodeKey] = []
        root_step = steps[root_index]
        root_label = _target_label(root_step.output_type, root_step.concept)
        try:
            for step in steps:
                atom = self.registry.get(step.atom)
                if atom is None:
                    raise ValueError(f"неизвестный атом {step.atom!r}")
                extra = {**dict(root_extra or {}), **dict(step.extra)}
                leaf = available.lookup(
                    step.output_type, step.concept, entity_ref=entity_ref,
                    period_ref=step.period_ref, basis=None, document_version=document_version,
                    policy_version=policy_version,
                )
                if leaf is not None and _leaf_belongs_to_step(leaf, atom):
                    keys.append(self._resolve(
                        step.output_type, step.concept, entity_ref=entity_ref,
                        period_ref=step.period_ref, basis=None, document_version=document_version,
                        policy_version=policy_version, extra=extra, requested_by=atom.name,
                        available=available, state=state, run_passport=run_passport,
                    ))
                else:
                    keys.append(self._expand_candidate(
                        atom, output_type=step.output_type, concept=step.concept, entity_ref=entity_ref,
                        period_ref=step.period_ref, basis=None, document_version=document_version,
                        policy_version=policy_version, extra=extra, available=available, state=state,
                        run_passport=run_passport,
                    ))
        except _PlannerCycle as cyc:
            reason = f"цикл зависимостей на узле {cyc.node_key.atom_name}({cyc.node_key.args_fingerprint})"
            graph = PlanGraph(
                plan_id=plan_id or f"plan-{root_label}",
                root=NodeKey(atom_name="unresolved.cycle", atom_version="0.0.0",
                             args_fingerprint=f"target={root_label}"),
                nodes=[], graph_hash="sha256:cycle-" + root_label, planner_version=planner_version,
            )
            return PlanBuildResult(
                graph=graph,
                feasibility=PlanFeasibility(feasible=False, unreachable_targets=[root_label], reason=reason),
            ), {}
        build, node_args = self._assemble(
            root_key=keys[root_index], state=state, run_passport=run_passport, plan_id=plan_id,
            root_label=root_label, planner_version=planner_version,
        )
        return build, node_args

    # ------------------------------------------------------------------
    def _resolve(
        self, output_type: str, concept: str | None, *, entity_ref: str | None, period_ref: str | None,
        basis: str | None, document_version: str | None, policy_version: str | None,
        extra: Mapping[str, str], requested_by: str, available: AvailableIndex,
        state: _BuildState, run_passport: RunPassport | None,
    ) -> NodeKey | None:
        """ProducerResolver (R3, A1.3): доступный лист → иначе, среди
        зарегистрированных производителей `(output_type, concept)`,
        reachability-first + minimum incremental cost → иначе `Unreachable`."""
        # 1. Уже доступный лист — ветвь заканчивается здесь (A1.4, шаг 2-3).
        leaf = available.lookup(
            output_type, concept, entity_ref=entity_ref, period_ref=period_ref, basis=basis,
            document_version=document_version, policy_version=policy_version,
        )
        if leaf is not None:
            if leaf.node_key not in state.nodes:
                state.nodes[leaf.node_key] = PlanNode(
                    node_key=leaf.node_key, requires=[], produces=[_target_label(output_type, concept)],
                    sandbox_profile="compute_no_network",
                )
                # Готовый узел не исполняется — `GraphExecutor` берёт из него только ключ и доказательства.
                # `extra` запрашивающей цепочки (корень формы, ad-hoc формула) к нему не относится; с ним
                # один и тот же лист у двух частей составной цели выглядел бы конфликтом `NodeArgs` в `plan_many`.
                state.node_args[leaf.node_key] = NodeArgs(
                    concept=concept, entity_ref=entity_ref, period_ref=period_ref, basis=basis,
                    document_version=document_version, policy_version=policy_version,
                )
            return leaf.node_key

        # 2. Производители из реестра (A1.4, шаг 4-5, 8).
        candidates = self.registry.producers_of(output_type, concept)
        label = concept or output_type
        if not candidates:
            state.unreachable.append(Unreachable(
                concept=label, reason="нет зарегистрированного атома-производителя", requested_by=requested_by,
            ))
            return None

        if len(candidates) == 1:
            return self._expand_candidate(
                candidates[0], output_type=output_type, concept=concept, entity_ref=entity_ref,
                period_ref=period_ref, basis=basis, document_version=document_version, policy_version=policy_version, extra=extra, available=available, state=state,
                run_passport=run_passport,
            )

        # 3. Несколько кандидатов: reachability-first, затем явный
        # `PlanPolicy.producer_priority` (если политика ЗНАЕТ порядок для
        # этого концепта — это декларативное продуктовое решение, «предпочесть
        # то, что реально написано в отчёте, расчёту», и оно обязано
        # применяться независимо от того, какой из producer'ов дешевле по
        # cost model — расчёт почти всегда дешевле извлечения, ЦЕНА не
        # выражает семантическое предпочтение, только объём новых узлов),
        # иначе — minimum incremental cost среди достижимых, а равенство
        # cost без записи в политике — `ChoicePoint`. Порядок регистрации
        # (`candidates` — порядок реестра) не участвует нигде в этом сравнении.
        trials: list[tuple[AtomDef, NodeKey, int, _BuildState]] = []
        for candidate in candidates:
            probe = _BuildState(
                nodes=dict(state.nodes), node_args=dict(state.node_args), visiting=set(state.visiting),
                gated_key_for=dict(state.gated_key_for),
            )
            try:
                key = self._expand_candidate(
                    candidate, output_type=output_type, concept=concept, entity_ref=entity_ref,
                    period_ref=period_ref, basis=basis, document_version=document_version, policy_version=policy_version, extra=extra, available=available, state=probe,
                    run_passport=run_passport,
                )
            except _PlannerCycle:
                continue
            if key is None or probe.unreachable or probe.choice_points:
                continue
            cost = self._incremental_cost(probe, baseline=state)
            trials.append((candidate, key, cost, probe))

        if not trials:
            state.unreachable.append(Unreachable(
                concept=label, reason="ни один из нескольких производителей не достижим по своим requires",
                requested_by=requested_by,
            ))
            return None

        by_name = {t[0].name: t for t in trials}
        for preferred in self.policy.producer_priority.get(label, ()):
            if preferred in by_name:
                _, key, _, probe = by_name[preferred]
                state.nodes.update(probe.nodes)
                state.node_args.update(probe.node_args)
                state.gated_key_for.update(probe.gated_key_for)
                state.pending_run_passport = state.pending_run_passport or probe.pending_run_passport
                state.incomplete_scope.extend(probe.incomplete_scope)
                return key

        # Явного приоритета для этого концепта нет (цикл выше ничего не нашёл)
        # —minimum incremental cost среди достижимых; равенство без записи в
        # политике — `ChoicePoint` (A1.6), не первый по порядку реестра.
        min_cost = min(t[2] for t in trials)
        best = [t for t in trials if t[2] == min_cost]
        if len(best) > 1:
            state.choice_points.append(ChoicePoint(
                concept=label, candidate_atoms=[t[0].name for t in best],
                reason="несколько производителей одинаковой минимальной стоимости достижимы, "
                       "PlanPolicy.producer_priority не задаёт порядок",
            ))
            return None

        _, key, _, probe = best[0]
        state.nodes.update(probe.nodes)
        state.node_args.update(probe.node_args)
        state.gated_key_for.update(probe.gated_key_for)
        state.pending_run_passport = state.pending_run_passport or probe.pending_run_passport
        state.incomplete_scope.extend(probe.incomplete_scope)
        return key

    def _incremental_cost(self, probe: _BuildState, *, baseline: _BuildState) -> int:
        """A1.4 cost model v1: сумма весов ТОЛЬКО новых узлов поверх
        `baseline` (уже существующие/переиспользуемые узлы = 0). Веса берутся
        из `PlanPolicy.cost_weights` (версionable данные политики), не
        хардкодятся здесь — этот метод только считает, не назначает цену."""
        new_keys = probe.nodes.keys() - baseline.nodes.keys()
        total = 0
        for key in new_keys:
            atom = self.registry.get(key.atom_name)
            if atom is None:
                continue  # доступный лист — уже посчитан как cost 0 по построению
            total += self.policy.cost_weights.get(atom.cost_class, 1)
        return total

    def _expand_candidate(
        self, atom: AtomDef, *, output_type: str, concept: str | None, entity_ref: str | None,
        period_ref: str | None, basis: str | None, document_version: str | None, policy_version: str | None,
        extra: Mapping[str, str], available: AvailableIndex, state: _BuildState, run_passport: RunPassport | None,
    ) -> NodeKey | None:
        """Дедупликация + обнаружение цикла (A1.4, шаги 10-11) вокруг
        `_expand_atom` — вынесено из `_resolve`, чтобы reachability-проба
        (`_resolve`, п.3) могла вызвать ровно ту же логику на СВОЁМ scratch
        `_BuildState`, не дублируя её.

        Повторный запрос уже раскрытого производителя обязан вернуть тот же
        ПОСЛЕ-гейтовый ключ, что и первый раз (`state.gated_key_for`) — не
        сырой `node_key` самого атома. Иначе второй, независимый путь к тому
        же концепту (calc.formula и analyze.full_bundle оба нуждаются в
        одном `read.value|X`) тихо обходил бы `mandatory_gates`."""
        node_args = NodeArgs(concept=concept, entity_ref=entity_ref, period_ref=period_ref, basis=basis, document_version=document_version, policy_version=policy_version, extra=extra)
        node_key = NodeKey(
            atom_name=atom.name, atom_version=atom.version,
            args_fingerprint=fingerprint(node_args, atom.key_fields),
        )
        if node_key in state.nodes:
            return state.gated_key_for.get(node_key, node_key)
        if node_key in state.visiting:
            raise _PlannerCycle(node_key)
        state.visiting.add(node_key)
        try:
            return self._expand_atom(
                atom, node_key, node_args, output_type=output_type, concept=concept, entity_ref=entity_ref,
                period_ref=period_ref, basis=basis, document_version=document_version, policy_version=policy_version, extra=extra, available=available, state=state,
                run_passport=run_passport,
            )
        finally:
            state.visiting.discard(node_key)

    def _run_passport_key(
        self, *, entity_ref: str | None, period_ref: str | None, basis: str | None,
        document_version: str | None, policy_version: str | None, available: AvailableIndex,
        state: _BuildState, run_passport: RunPassport | None,
    ) -> NodeKey | None:
        return self._resolve(
            RUN_PASSPORT_CONCEPT, None, entity_ref=entity_ref, period_ref=period_ref, basis=basis,
            document_version=document_version, policy_version=policy_version, extra={},
            requested_by="run_passport_gate", available=available, state=state, run_passport=run_passport,
        )

    def _expand_atom(
        self, atom: AtomDef, node_key: NodeKey, node_args: NodeArgs, *, output_type: str, concept: str | None,
        entity_ref: str | None, period_ref: str | None, basis: str | None,
        document_version: str | None, policy_version: str | None, extra: Mapping[str, str],
        available: AvailableIndex, state: _BuildState, run_passport: RunPassport | None,
    ) -> NodeKey:
        require_keys: list[NodeKey] = []

        if atom.needs_run_passport:
            gate_key = self._run_passport_key(
                entity_ref=entity_ref, period_ref=period_ref, basis=basis,
                document_version=document_version, policy_version=policy_version,
                available=available, state=state, run_passport=run_passport,
            )
            if gate_key is not None:
                require_keys.append(gate_key)
            if run_passport is None:
                # A1.5, первый проход: состав requires этого узла ЗАВИСИТ от
                # классификации, которой ещё нет. Узел заводится в граф уже
                # сейчас (он часть той же корневой вершины), но раскрытие ЕГО
                # requires останавливается на границе — второй вызов plan()
                # с готовым `run_passport` достроит их, не создавая новый план.
                state.pending_run_passport = True
                node = PlanNode(
                    node_key=node_key, requires=require_keys, produces=_produces_for(atom, output_type, concept),
                    sandbox_profile=atom.sandbox_profile,
                )
                state.nodes[node_key] = node
                state.node_args[node_key] = node_args
                return node_key
            concrete_requires = self._dynamic_or_static(atom.dynamic_requires, atom.requires, node_args, run_passport)
        else:
            concrete_requires = self._dynamic_or_static(atom.dynamic_requires, atom.requires, node_args, run_passport)

        for spec in concrete_requires:
            requirement_extra = (
                dict(atom.requirement_extra(node_args, spec))
                if atom.requirement_extra is not None else extra
            )
            req_key = self._resolve(
                spec.input_type, spec.concept, entity_ref=entity_ref, period_ref=period_ref, basis=basis,
                document_version=document_version, policy_version=policy_version, extra=requirement_extra,
                requested_by=atom.name, available=available, state=state, run_passport=run_passport,
            )
            if req_key is not None:
                require_keys.append(req_key)

        concrete_scope = self._dynamic_or_static(
            atom.dynamic_collection_scope, atom.collection_scope, node_args, run_passport,
        )
        for spec in concrete_scope:
            scope_key = self._try_visit_isolated(
                spec, entity_ref=entity_ref, period_ref=period_ref, basis=basis,
                document_version=document_version, policy_version=policy_version, available=available,
                state=state, run_passport=run_passport, requested_by=atom.name, extra=extra,
            )
            if scope_key is not None:
                require_keys.append(scope_key)

        node = PlanNode(
            node_key=node_key, requires=require_keys, produces=_produces_for(atom, output_type, concept),
            sandbox_profile=atom.sandbox_profile,
        )
        state.nodes[node_key] = node
        state.node_args[node_key] = node_args
        final_key = self._apply_mandatory_gates(
            atom, node_key, node_args, entity_ref=entity_ref, period_ref=period_ref, basis=basis,
            document_version=document_version, policy_version=policy_version,
            available=available, state=state, run_passport=run_passport,
        )
        # См. докстринг `_expand_candidate`/`_BuildState.gated_key_for`: без
        # этой записи повторный запрос ТОГО ЖЕ производителя другим путём
        # обошёл бы гейты, найдя `node_key` уже в `state.nodes`.
        state.gated_key_for[node_key] = final_key
        return final_key

    @staticmethod
    def _dynamic_or_static(dynamic, static: tuple[InputSpec, ...], node_args: NodeArgs, run_passport) -> tuple[InputSpec, ...]:
        if dynamic is None:
            return static
        result = dynamic(node_args, run_passport)
        if not result:
            return ()
        return _norm_input_specs(result)

    def _try_visit_isolated(
        self, spec: InputSpec, *, entity_ref: str | None, period_ref: str | None, basis: str | None,
        document_version: str | None, policy_version: str | None, available: AvailableIndex,
        state: _BuildState, run_passport: RunPassport | None, requested_by: str,
        extra: Mapping[str, str],
    ) -> NodeKey | None:
        """R3, A1.7: одна ветка необязательной коллекции (`analyze.
        full_bundle`/`out.form`). Раскрывается ИЗОЛИРОВАННЫМ пробным
        проходом — неудача (сама ветка недостижима, ИЛИ достижима, но её
        собственные глубинные requires — нет, ИЛИ неоднозначна) не создаёт
        nullable-ребро и не портит `state.unreachable`/`feasibility.feasible`
        родителя: ветвь просто отсутствует у него в `requires`
        («отсутствием ветви в конкретном Target/scope»,
        `02-tekushee-sostoyanie-i-mekhanika.md`), а причина уходит в
        `state.incomplete_scope` («политика полноты root result»)."""
        label = spec.concept or spec.input_type
        probe = _BuildState(
            nodes=dict(state.nodes), node_args=dict(state.node_args), visiting=set(state.visiting),
            gated_key_for=dict(state.gated_key_for),
        )
        try:
            key = self._resolve(
                spec.input_type, spec.concept, entity_ref=entity_ref, period_ref=period_ref, basis=basis,
                document_version=document_version, policy_version=policy_version, extra=extra,
                requested_by=requested_by, available=available, state=probe, run_passport=run_passport,
            )
        except _PlannerCycle:
            state.incomplete_scope.append(Unreachable(
                concept=label, reason="цикл внутри необязательной ветки коллекции", requested_by=requested_by,
            ))
            return None

        if key is None or probe.unreachable or probe.choice_points:
            for u in probe.unreachable:
                state.incomplete_scope.append(u)
            for c in probe.choice_points:
                state.incomplete_scope.append(Unreachable(
                    concept=c.concept, reason=f"ambiguous_producer: {c.reason}", requested_by=requested_by,
                ))
            if key is None and not probe.unreachable and not probe.choice_points:
                state.incomplete_scope.append(Unreachable(
                    concept=label, reason="нет зарегистрированного атома-производителя", requested_by=requested_by,
                ))
            return None

        state.nodes.update(probe.nodes)
        state.node_args.update(probe.node_args)
        state.gated_key_for.update(probe.gated_key_for)
        state.pending_run_passport = state.pending_run_passport or probe.pending_run_passport
        return key

    def _apply_mandatory_gates(
        self, atom: AtomDef, produced_key: NodeKey, node_args: NodeArgs, *,
        entity_ref: str | None, period_ref: str | None, basis: str | None,
        document_version: str | None, policy_version: str | None, available: AvailableIndex,
        state: _BuildState, run_passport: RunPassport | None,
    ) -> NodeKey:
        """A1.7: обязательные контрольные этапы — явные узлы графа, а не
        проверки, вписанные внутрь исполнителя. Каждый следующий потребитель
        зависит от ПОСЛЕДНЕГО узла цепочки гейтов, а не от сырого вывода
        атома напрямую — обойти обязательную проверку, потребовав узел
        раньше гейта, невозможно: раньше гейта его просто не существует ни
        для кого, кроме самого гейта.

        Срез 6a: гейт получает не только предыдущий узел цепочки (`current`),
        но и СВОИ СОБСТВЕННЫЕ `requires`/`dynamic_requires`, раскрытые тем же
        `_resolve`, что обычный узел. Без этого `verify.identity`/`verify.
        cross`/`verify.citation` не могли бы дотянуться до ВТОРОГО,
        независимо полученного источника (второй стороны сравнения, текста
        документа для сверки цитаты) — карточки 16a прямо требуют такую
        независимость («сравнивают два независимо полученных значения»), а
        единственный вход `[current]` физически даёт гейту только то, что
        произвёл его непосредственный предшественник. Обратная совместимость:
        гейт без объявленных `requires`/`dynamic_requires` (например
        существующий `verify.arithmetic`) получает ровно `[current]`, как и
        раньше — `_dynamic_or_static` на пустых `requires=()`/`dynamic_
        requires=None` возвращает `()`, и цикл по `extra_requires` не
        добавляет ничего."""
        current = produced_key
        for gate_name in atom.mandatory_gates:
            gate_atom = self.registry.get(gate_name)
            if gate_atom is None:
                raise ValueError(
                    f"атом {atom.name} объявляет обязательный этап {gate_name!r}, "
                    "которого нет в AtomRegistry — контракт реестра нарушен"
                )
            # A gate instance is identified by both its business arguments and
            # the exact upstream node it verifies.  Without the upstream part,
            # ``out.narrative`` and ``out.report`` with identical entity/period
            # arguments collapse onto one verify.grounding node.  The report
            # producer then becomes disconnected while the graph still looks
            # successful, and no ReportOutput reaches the root/event stream.
            gate_args = fingerprint(node_args, gate_atom.key_fields)
            upstream = f"{current.atom_name}@{current.atom_version}:{current.args_fingerprint}"
            gate_fingerprint = hashlib.sha256(
                f"{gate_args}|upstream={upstream}".encode("utf-8")
            ).hexdigest()
            gate_key = NodeKey(
                atom_name=gate_atom.name, atom_version=gate_atom.version,
                args_fingerprint=gate_fingerprint,
            )
            if gate_key not in state.nodes:
                require_keys = [current]
                extra_requires = self._dynamic_or_static(
                    gate_atom.dynamic_requires, gate_atom.requires, node_args, run_passport,
                )
                for spec in extra_requires:
                    req_key = self._resolve(
                        spec.input_type, spec.concept, entity_ref=entity_ref, period_ref=period_ref, basis=basis,
                        document_version=document_version, policy_version=policy_version,
                        extra=node_args.extra, requested_by=gate_atom.name, available=available, state=state,
                        run_passport=run_passport,
                    )
                    if req_key is not None:
                        require_keys.append(req_key)
                state.nodes[gate_key] = PlanNode(
                    node_key=gate_key, requires=require_keys, produces=[spec.label() for spec in gate_atom.produces],
                    sandbox_profile=gate_atom.sandbox_profile,
                )
                state.node_args[gate_key] = node_args
            current = gate_key
        return current


__all__ = [
    "PLANNER_VERSION",
    "DYNAMIC_PLANNER_VERSION",
    "AtomStep",
    "AvailableLeaf",
    "AvailableIndex",
    "GraphPlanner",
    "compute_graph_hash",
]
