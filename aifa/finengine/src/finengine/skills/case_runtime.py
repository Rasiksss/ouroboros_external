"""Slice 10 orchestration: persistent Case leaves, delta plans and overrides.

`GraphPlanner` and `GraphExecutor` deliberately remain stateless.  This module
is the thin owner-scoped bridge that turns persisted Case artifacts into cost-0
`AvailableLeaf` inputs, records `PlanRun`/`NodeRun`, and maintains immutable
user-correction audit.  Postgres is the production store; SQLite exercises the
same contract locally when Postgres is unavailable.
"""
from __future__ import annotations

import functools
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from pydantic import BaseModel

from finengine.contracts import (
    CalculatedMetric,
    CaseIdentity,
    CaseTarget,
    CorrectionIntent,
    DynamicArtifact,
    ExtractedMetric,
    ExtractedStructure,
    GoalIR,
    NodeKey,
    OverrideAuditEvent,
    OverrideChoicePoint,
    PlanRunRecord,
    RunPassport,
    RuleComparison,
    SubjectAccess,
    SubjectProfile,
    UserProvidedMetric,
)
from finengine.contracts.base import new_id
from finengine.skills.atoms.read_override import (
    MetricLike,
    metric_evidence_id,
    metric_value_unit_period,
    validate_correction,
)
from finengine.skills.atoms.subject_history import (
    MONITOR_HISTORY_WINDOW,
    comparable_snapshot_pair,
    subject_key_from_identity,
)
from finengine.skills.spec.atom_registry import AtomDef, AtomRegistry, NodeArgs
from finengine.skills.spec.graph_executor import (
    TERMINAL_FAILURE,
    ExecutionBudget,
    ExecutionResult,
    GraphExecutor,
    RunInputs,
)
from finengine.skills.spec.graph_planner import (
    DYNAMIC_PLANNER_VERSION,
    AtomStep,
    AvailableIndex,
    AvailableLeaf,
    GraphPlanner,
)
from finengine.skills.spec.target import METRIC_EVIDENCE_MEMBERS, Target
from finengine.store.cases import (
    SqlCaseStore,
    StalePlanRunError,
    case_lock,
    evidence_id_of,
    owner_key,
    passport_fingerprint,
)


@dataclass(frozen=True)
class PlannedCaseRun:
    record: PlanRunRecord
    available: AvailableIndex


class PlanInfeasible(ValueError):
    """Детерминированный планировщик не смог собрать исполнимый план.

    Подкласс `ValueError` (прежние обработчики продолжают работать); несёт текст
    причины — его получает динамический планировщик как ошибку, из-за которой его
    позвали (Срез 31, П8)."""


def _node_token(key: NodeKey) -> str:
    return json.dumps(key.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)


def _args_to_dict(args: NodeArgs) -> dict:
    return {
        "concept": args.concept,
        "entity_ref": args.entity_ref,
        "period_ref": args.period_ref,
        "basis": args.basis,
        "document_version": args.document_version,
        "policy_version": args.policy_version,
        "extra": dict(args.extra),
    }


def _args_from_dict(doc: Mapping[str, Any]) -> NodeArgs:
    return NodeArgs(
        concept=doc.get("concept"),
        entity_ref=doc.get("entity_ref"),
        period_ref=doc.get("period_ref"),
        basis=doc.get("basis"),
        document_version=doc.get("document_version"),
        policy_version=doc.get("policy_version"),
        extra=dict(doc.get("extra") or {}),
    )


def _case_target(target: Target) -> CaseTarget:
    return CaseTarget(
        output_type=target.output_type,
        concept_id=target.concept_id,
        entity_ref=target.entity_ref,
        period_ref=target.period_ref,
        basis=target.basis,
        document_version=target.document_version,
        policy_version=target.policy_version,
        scope_id=target.scope_id,
        params=dict(target.params),
    )


def _leaf_descriptor(key: tuple, leaf: AvailableLeaf) -> dict:
    output_type, concept, entity_ref, period_ref, basis, document_version, policy_version = key
    return {
        "output_type": output_type,
        "concept": concept,
        "entity_ref": entity_ref,
        "period_ref": period_ref,
        "basis": basis,
        "document_version": document_version,
        "policy_version": policy_version,
        "node_key": leaf.node_key.model_dump(mode="json"),
    }


def _payload_output_type(value: Any) -> str:
    # DynamicArtifact хранится под своим dyn:*-типом, а не под именем класса:
    # иначе все динамические результаты слиплись бы в один тип и не нашлись бы
    # на следующем ходе через AvailableIndex.
    if isinstance(value, DynamicArtifact):
        return value.output_type
    return type(value).__name__


def _planned_one_by_one(method):
    """Планирование частей одного Case идёт по очереди: ревизия читается и записывается под замком (ADR-0090)."""

    @functools.wraps(method)
    def wrapper(self, *args, owner_id: object, case_id: str, **kwargs):
        with case_lock(owner_id, case_id):
            return method(self, *args, owner_id=owner_id, case_id=case_id, **kwargs)

    return wrapper


class CaseRuntime:
    """Persistent owner-scoped execution facade for one conversational Case."""

    def __init__(
        self,
        *,
        store: SqlCaseStore,
        registry: AtomRegistry,
        policy,
        budget: ExecutionBudget | None = None,
    ):
        self.store = store
        self.registry = registry
        self.policy = policy
        self.planner = GraphPlanner(registry=registry, policy=policy)
        self.executor = GraphExecutor(registry=registry, budget=budget)

    def with_atoms(self, atoms: Sequence[AtomDef]) -> "CaseRuntime":
        """Та же Case-среда с реестром, дополненным временными атомами разговора.

        План и исполнение хода обязаны идти через ОДИН такой снимок: план хранит
        registry_fingerprint и сверяет его при execute, а _artifact_is_compatible
        сверяет версии атомов — по этому же реестру."""
        if not atoms:
            return self
        return CaseRuntime(
            store=self.store, registry=AtomRegistry([*self.registry.all(), *atoms]),
            policy=self.policy, budget=self.executor.budget,
        )

    # ------------------------------------------------------------------
    # Case/document identity
    def ensure_case(self, *, owner_id: object, case_id: str, tenant_id: str | None = None):
        return self.store.ensure_case(CaseIdentity(
            case_id=case_id, owner_id=owner_key(owner_id), tenant_id=tenant_id,
        ))

    def register_document(
        self,
        *,
        owner_id: object,
        case_id: str,
        logical_name: str,
        document_id: str,
        document_version: str,
        expected_revision: int | None = None,
    ):
        return self.store.register_document(
            owner_id, case_id, logical_name=logical_name, document_id=document_id,
            document_version=document_version, expected_revision=expected_revision,
        )

    def register_ad_hoc_spec(self, owner_id: object, case_id: str, spec):
        with case_lock(owner_id, case_id):
            return self.store.put_ad_hoc_spec(owner_id, case_id, spec)

    def _bind_target_to_case(self, owner_id: object, case_id: str, target: Target) -> Target:
        docs = self.store.active_documents(owner_id, case_id)
        active_versions = {d.document_version for d in docs}
        if target.document_version is not None and active_versions and target.document_version not in active_versions:
            raise StalePlanRunError(
                f"target document_version {target.document_version!r} is not active in Case {case_id!r}"
            )
        document_version = target.document_version
        history_only = target.params.get("document_scope") in {"subject_history", "none"}
        if document_version is None and len(active_versions) == 1 and not history_only:
            document_version = next(iter(active_versions))
        return Target(
            output_type=target.output_type,
            concept_id=target.concept_id,
            entity_ref=target.entity_ref,
            period_ref=target.period_ref,
            basis=target.basis,
            document_version=document_version,
            policy_version=target.policy_version or self.policy.policy_version,
            scope_id=target.scope_id,
            params=dict(target.params),
        )

    # ------------------------------------------------------------------
    # Persisted leaves / compatibility
    def _artifact_is_compatible(self, row: dict, *, run_passport: RunPassport | None, active_versions: set[str]) -> bool:
        key: NodeKey = row["node_key"]
        atom = self.registry.get(key.atom_name)
        if atom is not None and atom.version != key.atom_version:
            return False
        if row.get("policy_version") and row["policy_version"] != self.policy.policy_version:
            return False
        if row.get("passport_fingerprint"):
            if passport_fingerprint(run_passport) != row["passport_fingerprint"]:
                return False
        if row.get("document_version") and active_versions and row["document_version"] not in active_versions:
            return False
        return True

    def available_index(
        self, *, owner_id: object, case_id: str, run_passport: RunPassport | None = None,
    ) -> AvailableIndex:
        index = AvailableIndex()
        active_versions = {d.document_version for d in self.store.active_documents(owner_id, case_id)}
        artifacts = self.store.list_artifacts(owner_id, case_id, valid_only=True)
        evidence_artifact: dict[str, dict] = {}

        # Результат временного атома, посчитанный из выхода устаревшей версии другого
        # временного атома, тоже устарел: версия самого атома этого не видит.
        # Строки идут по created_at, производитель раньше потребителя.
        stale_dyn_evidence: set[str] = set()
        for row in artifacts:
            is_dyn = row["output_type"].startswith("dyn:")
            compatible = self._artifact_is_compatible(
                row, run_passport=run_passport, active_versions=active_versions)
            if compatible and is_dyn and stale_dyn_evidence & set(row.get("dependency_evidence_ids") or []):
                compatible = False
            if not compatible:
                if is_dyn:
                    stale_dyn_evidence.update(str(x) for x in row.get("evidence_ids") or [])
                continue
            payloads = tuple(v for v in row["payloads"] if isinstance(v, BaseModel))
            leaf = AvailableLeaf(
                node_key=row["node_key"],
                evidence_ids=tuple(str(x) for x in (row.get("evidence_ids") or [])),
                evidence=payloads,
            )
            kwargs = dict(
                concept=row.get("concept"), entity_ref=row.get("entity_ref"),
                period_ref=row.get("period_ref"), basis=row.get("basis"),
                document_version=row.get("document_version"), policy_version=row.get("policy_version"),
            )
            index.put(row["output_type"], leaf, **kwargs)
            if row["output_type"] in METRIC_EVIDENCE_MEMBERS:
                index.put("MetricEvidence", leaf, **kwargs)
            for eid in row.get("evidence_ids") or []:
                evidence_artifact[str(eid)] = row

        # Active override is a semantic selection overlay, not a mutation of
        # the document artifact.  Publish it last on MetricEvidence so it wins
        # the alias while the original ExtractedMetric remains directly
        # addressable and auditable.
        for root_evidence_id, wrapper in self.store.active_overrides(owner_id, case_id).items():
            if not isinstance(wrapper, UserProvidedMetric):
                continue
            h = wrapper.human_override
            if h.document_version and active_versions and h.document_version not in active_versions:
                continue  # override belongs to an invalidated document revision
            source_row = evidence_artifact.get(root_evidence_id) or evidence_artifact.get(h.overridden_evidence_id)
            entity_ref = source_row.get("entity_ref") if source_row else None
            policy_version = source_row.get("policy_version") if source_row else self.policy.policy_version
            if policy_version and policy_version != self.policy.policy_version:
                continue
            key = NodeKey(
                atom_name="read.override",
                atom_version=h.produced_by_atom_version,
                args_fingerprint=f"override_evidence={h.evidence_id}",
            )
            leaf = AvailableLeaf(node_key=key, evidence_ids=(h.evidence_id,), evidence=(wrapper,))
            kwargs = dict(
                concept=wrapper.concept, entity_ref=entity_ref, period_ref=wrapper.period_ref,
                basis=h.basis, document_version=h.document_version, policy_version=policy_version,
            )
            index.put("UserProvidedMetric", leaf, **kwargs)
            index.put("MetricEvidence", leaf, **kwargs)

        # Subject history is exposed as two typed leaves.  They are scoped by
        # owner in the SQL query and by stable SubjectKey in the graph key.
        if run_passport is not None:
            try:
                subject_key = subject_key_from_identity(run_passport.entity)
            except ValueError:
                subject_key = None
            if subject_key is not None:
                pair = comparable_snapshot_pair(self.store.list_subject_profiles(
                    owner_id, subject_key.value, limit=MONITOR_HISTORY_WINDOW,
                ))
                if pair is not None:
                    for role, profile in zip(("previous", "current"), pair):
                        key = NodeKey(
                            atom_name=f"history.subject_profile.{role}",
                            atom_version="1.0.0",
                            args_fingerprint=f"subject={subject_key.value};snapshot={profile.snapshot_id}",
                        )
                        leaf = AvailableLeaf(
                            node_key=key, evidence_ids=tuple(profile.evidence_ids), evidence=(profile,),
                        )
                        index.put(
                            f"SubjectProfile:{role}", leaf,
                            entity_ref=subject_key.value,
                            policy_version=self.policy.policy_version,
                        )
        return index

    def _append_subject_profile(
        self, *, owner_id: object, case_id: str, record: PlanRunRecord,
        passport: RunPassport, result: ExecutionResult,
    ) -> SubjectProfile | None:
        try:
            subject_key = subject_key_from_identity(passport.entity)
        except ValueError:
            return None
        values = [value for group in result.evidence_by_key.values() for value in group]
        metrics = [v for v in values if isinstance(v, (ExtractedMetric, CalculatedMetric, UserProvidedMetric))]
        structures = [v for v in values if isinstance(v, ExtractedStructure)]
        thresholds = [v for v in values if isinstance(v, RuleComparison)]
        if not metrics and not structures and not thresholds:
            return None
        evidence_ids: set[str] = set()
        periods: set[str] = set()
        document_versions: set[str] = set()
        for metric in metrics:
            payload = getattr(metric, "scalar_fact", None) or getattr(metric, "derived_fact", None) or metric.human_override
            evidence_ids.add(payload.evidence_id)
            periods.add(metric.period_ref)
            if payload.document_version:
                document_versions.add(payload.document_version)
        for structure in structures:
            evidence_ids.add(structure.structured_fact.evidence_id)
            if structure.structured_fact.period_ref:
                periods.add(structure.structured_fact.period_ref)
            if structure.structured_fact.document_version:
                document_versions.add(structure.structured_fact.document_version)
        for rule in thresholds:
            evidence_ids.add(rule.actual_evidence_id)
            evidence_ids.add(rule.threshold_reference_id)
        case = self.store.load_case(owner_id, case_id)
        if case is None:
            raise KeyError(case_id)
        profile = SubjectProfile(
            subject_key=subject_key,
            access=SubjectAccess(
                owner_id=owner_key(owner_id), tenant_id=case.identity.tenant_id,
                scope_id=record.target.scope_id,
            ),
            version=1, plan_run_id=record.plan_run_id, graph_hash=record.graph_hash,
            case_id=case_id, registry_version=passport.registry_version,
            methodology_version=passport.methodology_version,
            period_refs=sorted(periods), document_versions=sorted(document_versions),
            metrics=metrics, structures=structures, thresholds=thresholds,
            evidence_ids=sorted(evidence_ids),
        )
        return self.store.append_subject_profile(profile)

    # ------------------------------------------------------------------
    # Planning / execution / resume
    @_planned_one_by_one
    def plan(
        self,
        *,
        owner_id: object,
        case_id: str,
        goal_ir: GoalIR,
        target: Target,
        run_passport: RunPassport | None = None,
        root_extra: Mapping[str, str] | None = None,
        plan_run_id: str | None = None,
        expected_revision: int | None = None,
    ) -> PlannedCaseRun:
        snap = self.ensure_case(owner_id=owner_id, case_id=case_id)
        if expected_revision is not None and snap.revision != expected_revision:
            from finengine.store.cases import CaseConflictError  # local: avoid widening public facade imports
            raise CaseConflictError(expected=expected_revision, actual=snap.revision)
        bound = self._bind_target_to_case(owner_id, case_id, target)
        if (
            run_passport is None
            and snap.run_passport is not None
            and snap.run_passport_document_version == bound.document_version
        ):
            run_passport = snap.run_passport
        available = self.available_index(owner_id=owner_id, case_id=case_id, run_passport=run_passport)
        try:
            build, node_args = self.planner.plan_ex(
                bound, available=available, run_passport=run_passport,
                plan_id=plan_run_id or new_id(), root_extra=root_extra,
            )
        except ValueError as exc:
            # Ошибка кода планировщика (например, нарушен контракт реестра) — такой же сбой планирования,
            # как «неисполним»: его подхватывает цикл, как и у `plan_many` и `plan_steps`.
            raise PlanInfeasible(f"Case delta-plan is not feasible: {exc}") from exc
        if not build.feasibility.feasible:
            raise PlanInfeasible(f"Case delta-plan is not feasible: {build.feasibility.reason}")
        return self._persist_plan(
            owner_id=owner_id, case_id=case_id, goal_ir=goal_ir, bound=bound, build=build,
            node_args=node_args, available=available, run_passport=run_passport, snap=snap,
        )

    @_planned_one_by_one
    def plan_many(
        self, *, owner_id: object, case_id: str, goal_ir: GoalIR,
        requests: Sequence[tuple[Target, Mapping[str, str], str, str, Sequence[str]]],
        run_passport: RunPassport | None = None, plan_run_id: str | None = None,
    ) -> PlannedCaseRun:
        """Persist one compound PlanRun for all independently parsed goals."""
        snap = self.ensure_case(owner_id=owner_id, case_id=case_id)
        bound_requests = [
            (self._bind_target_to_case(owner_id, case_id, target), extra, ref, kind, deps)
            for target, extra, ref, kind, deps in requests
        ]
        if run_passport is None and snap.run_passport is not None:
            versions = {target.document_version for target, *_ in bound_requests}
            if versions == {snap.run_passport_document_version}:
                run_passport = snap.run_passport
        available = self.available_index(owner_id=owner_id, case_id=case_id, run_passport=run_passport)
        try:
            build, node_args = self.planner.plan_many(
                bound_requests, available=available, run_passport=run_passport,
                plan_id=plan_run_id or new_id(),
            )
        except ValueError as exc:
            # Цикл зависимостей, конфликт узлов — такой же отказ планирования, как «неисполним».
            raise PlanInfeasible(f"Case compound plan is not feasible: {exc}") from exc
        if not build.feasibility.feasible:
            raise PlanInfeasible(f"Case compound plan is not feasible: {build.feasibility.reason}")
        first = bound_requests[0][0]
        compound = Target(
            output_type="CompoundResult", entity_ref=first.entity_ref,
            document_version=first.document_version, policy_version=first.policy_version,
            scope_id=first.scope_id, params={"goal_refs": ",".join(item[2] for item in bound_requests)},
        )
        return self._persist_plan(
            owner_id=owner_id, case_id=case_id, goal_ir=goal_ir, bound=compound,
            build=build, node_args=node_args, available=available,
            run_passport=run_passport, snap=snap,
        )

    @_planned_one_by_one
    def plan_steps(
        self, *, owner_id: object, case_id: str, goal_ir: GoalIR, target: Target,
        steps: Sequence[AtomStep], run_passport: RunPassport | None = None,
        root_extra: Mapping[str, str] | None = None, plan_run_id: str | None = None,
    ) -> PlannedCaseRun:
        """Сохранить один ход цикла из явно названных, в том числе независимых, шагов."""
        snap, bound, run_passport, available = self._prepare_dynamic_plan(
            owner_id=owner_id, case_id=case_id, target=target, run_passport=run_passport,
        )
        try:
            build, node_args = self.planner.plan_with_atoms(
                steps, root_index=len(steps) - 1, available=available, run_passport=run_passport,
                plan_id=plan_run_id or new_id(), root_extra=root_extra, entity_ref=bound.entity_ref,
                document_version=bound.document_version, planner_version=DYNAMIC_PLANNER_VERSION,
            )
        except ValueError as exc:
            raise PlanInfeasible(f"Case dynamic step plan is not feasible: {exc}") from exc
        if not build.feasibility.feasible:
            raise PlanInfeasible(f"Case dynamic step plan is not feasible: {build.feasibility.reason}")
        root = steps[-1]
        root_bound = Target(
            output_type=root.output_type, concept_id=root.concept, entity_ref=bound.entity_ref,
            period_ref=root.period_ref or bound.period_ref, basis=bound.basis,
            document_version=bound.document_version, policy_version=bound.policy_version,
            scope_id=bound.scope_id, params=dict(bound.params),
        )
        return self._persist_plan(
            owner_id=owner_id, case_id=case_id, goal_ir=goal_ir, bound=root_bound, build=build,
            node_args=node_args, available=available, run_passport=run_passport, snap=snap,
        )

    def _prepare_dynamic_plan(
        self, *, owner_id: object, case_id: str, target: Target,
        run_passport: RunPassport | None,
    ) -> tuple[object, Target, RunPassport | None, AvailableIndex]:
        snap = self.ensure_case(owner_id=owner_id, case_id=case_id)
        bound = self._bind_target_to_case(owner_id, case_id, target)
        if (
            run_passport is None
            and snap.run_passport is not None
            and snap.run_passport_document_version == bound.document_version
        ):
            run_passport = snap.run_passport
        available = self.available_index(owner_id=owner_id, case_id=case_id, run_passport=run_passport)
        return snap, bound, run_passport, available

    def _persist_plan(
        self, *, owner_id: object, case_id: str, goal_ir: GoalIR, bound: Target, build,
        node_args: dict, available: AvailableIndex, run_passport: RunPassport | None, snap,
    ) -> PlannedCaseRun:
        # Persist only the semantic binding that the planner actually used.
        # The same physical fact is deliberately indexed both as its concrete
        # type (e.g. ExtractedMetric) and as MetricEvidence.  Persisting every
        # alias that happens to share its NodeKey would make an unrelated
        # override invalidate a plan that requested the concrete document fact.
        node_produces = {n.node_key: set(n.produces) for n in build.graph.nodes}
        persisted_leaves = []
        for key, leaf in available.entries():
            output_type, concept, *_ = key
            if leaf.node_key not in node_produces:
                continue
            label = output_type if concept is None else f"{output_type}:{concept}"
            if label in node_produces[leaf.node_key]:
                persisted_leaves.append(_leaf_descriptor(key, leaf))
        unique: dict[str, dict] = {}
        for desc in persisted_leaves:
            unique[json.dumps(desc, sort_keys=True, ensure_ascii=False)] = desc

        new_snap = self.store.update_case_head(
            owner_id, case_id, goal_ir=goal_ir, target=_case_target(bound),
            graph_hash=build.graph.graph_hash, run_passport=run_passport,
            expected_revision=snap.revision,
        )
        record = PlanRunRecord(
            plan_run_id=build.graph.plan_id,
            case_id=case_id,
            owner_id=owner_key(owner_id),
            goal_ir=goal_ir,
            target=_case_target(bound),
            graph=build.graph,
            graph_hash=build.graph.graph_hash,
            registry_fingerprint=self.registry.fingerprint(),
            policy_version=self.policy.policy_version,
            run_passport=run_passport,
            run_passport_ref=(run_passport.run_id if run_passport is not None else None),
            node_args={_node_token(k): _args_to_dict(v) for k, v in node_args.items()},
            available_leaf_keys=list(unique.values()),
            pending_run_passport=build.pending_run_passport,
            status="planned",
            case_revision=new_snap.revision,
        )
        self.store.create_plan_run(record)
        return PlannedCaseRun(record=record, available=available)

    def _validate_plan_record(self, record: PlanRunRecord, available: AvailableIndex) -> None:
        if record.registry_fingerprint != self.registry.fingerprint():
            raise StalePlanRunError("PlanRun registry fingerprint no longer matches current AtomRegistry")
        if record.policy_version != self.policy.policy_version:
            raise StalePlanRunError("PlanRun policy version no longer matches current PlanPolicy")
        if record.graph_hash != record.graph.graph_hash:
            raise StalePlanRunError("PlanRun graph hash does not match persisted PlanGraph")
        for desc in record.available_leaf_keys:
            leaf = available.lookup(
                desc["output_type"], desc.get("concept"),
                entity_ref=desc.get("entity_ref"), period_ref=desc.get("period_ref"),
                basis=desc.get("basis"), document_version=desc.get("document_version"),
                policy_version=desc.get("policy_version"),
            )
            expected = NodeKey.model_validate(desc["node_key"])
            if leaf is None or leaf.node_key != expected:
                raise StalePlanRunError(
                    f"persisted Case leaf became stale: {desc['output_type']}:{desc.get('concept') or ''}"
                )

    def _node_args_for(self, record: PlanRunRecord) -> dict[NodeKey, NodeArgs]:
        result: dict[NodeKey, NodeArgs] = {}
        for node in record.graph.nodes:
            raw = record.node_args.get(_node_token(node.node_key))
            if raw is None:
                raise StalePlanRunError(f"PlanRun misses NodeArgs for {node.node_key}")
            result[node.node_key] = _args_from_dict(raw)
        return result

    def execute(
        self,
        *,
        owner_id: object,
        case_id: str,
        plan_run_id: str,
        inputs: RunInputs | None = None,
        event_sink: Callable[[dict], None] | None = None,
        parallel_ready: bool = False,
        commit_guard: Callable[[], None] | None = None,
    ) -> ExecutionResult:
        record = self.store.load_plan_run(owner_id, case_id, plan_run_id)
        if record is None:
            raise KeyError(plan_run_id)
        if record.owner_id != owner_key(owner_id) or record.case_id != case_id:
            raise KeyError(plan_run_id)

        available = self.available_index(
            owner_id=owner_id, case_id=case_id, run_passport=record.run_passport,
        )
        try:
            self._validate_plan_record(record, available)
        except StalePlanRunError:
            self.store.update_plan_run_status(owner_id, case_id, plan_run_id, "stale")
            raise

        node_args = self._node_args_for(record)
        prior_runs, prior_evidence = self.store.load_node_runs(owner_id, case_id, plan_run_id)
        self.store.update_plan_run_status(owner_id, case_id, plan_run_id, "running")

        if event_sink is not None:
            # Срез 29, Этап 7: топология фиксирована — эмитим её ОДИН раз,
            # ДО первого node_run (см. docstring `result_events.plan_graph_event`).
            from finengine.skills.result_events import plan_graph_event  # noqa: PLC0415
            try:
                event_sink(plan_graph_event(record.graph))
            except Exception:  # noqa: BLE001 — наблюдаемость не смеет ронять исполнение
                import logging as _logging
                _logging.getLogger(__name__).error(
                    "plan_graph event emission failed run=%s", plan_run_id, exc_info=True,
                )

        def on_update(run) -> None:
            if commit_guard is not None and run.status == "succeeded":
                commit_guard()
            # Status/attempt survives an abrupt service-instance switch.  The
            # terminal callback below additionally persists the payload.
            self.store.save_node_run(owner_id, case_id, plan_run_id, run, ())
            if event_sink is not None:
                from finengine.skills.result_events import node_run_event  # noqa: PLC0415
                event_sink(node_run_event(run, plan_graph_hash=record.graph_hash))

        def on_result(run, evidence: tuple[BaseModel, ...]) -> None:
            if commit_guard is not None:
                commit_guard()
            self.store.save_node_run(owner_id, case_id, plan_run_id, run, evidence)

        leaf_keys = {leaf.node_key for leaf in available.all_leaves()}
        try:
            result = self.executor.run(
                record.graph, node_args, run_id=plan_run_id, available=available,
                run_passport=record.run_passport, inputs=inputs,
                prior_node_runs=prior_runs, prior_evidence=prior_evidence,
                on_node_update=on_update, on_node_result=on_result, parallel_ready=parallel_ready,
            )
        except Exception as exc:
            # Исполнитель прервался исключением (бюджет времени или вызовов, сбой хранилища): узлы,
            # которым не дали дойти до терминала, остались бы «выполняется» навсегда — и в Case, и в
            # рабочей области. Закрываем их фактом (часть 14, Р14.4) и отдаём исключение вызывающему.
            self._close_unfinished_nodes(
                owner_id, case_id, plan_run_id, on_update,
                reason=f"исполнение прервано: {type(exc).__name__}: {exc}",
            )
            if commit_guard is None:
                self._keep_finished_results(owner_id, case_id, record, node_args, leaf_keys)
            raise

        if commit_guard is not None:
            commit_guard()
        self._persist_artifacts(
            owner_id, case_id, record, node_args, leaf_keys, result.node_runs, result.evidence_by_key,
        )

        # The first pass of passport-dependent graphs may finish with the
        # business root failed/partial while `identify.classify` has already
        # produced the immutable RunPassport needed for the second pass. Keep
        # it in Case, tied to the exact document revision, so another service
        # instance can continue the same conversation without reclassification.
        discovered_passport = next((
            value for values in result.evidence_by_key.values() for value in values
            if isinstance(value, RunPassport)
        ), None)
        if discovered_passport is not None and record.run_passport is None:
            latest = self.store.load_case(owner_id, case_id)
            if latest is not None:
                try:
                    with case_lock(owner_id, case_id):      # ревизия растёт: не посреди плана соседней части
                        self.store.update_case_head(
                            owner_id, case_id, run_passport=discovered_passport,
                            run_passport_document_version=record.target.document_version,
                            expected_revision=latest.revision,
                        )
                except Exception as exc:
                    # Concurrent Case mutation is explicit, never silently
                    # overwritten. A retry may reuse the passport artifact
                    # already persisted by this PlanRun.
                    from finengine.store.cases import CaseConflictError
                    if not isinstance(exc, CaseConflictError):
                        raise

        self.store.update_plan_run_status(
            owner_id, case_id, plan_run_id, "succeeded" if result.succeeded else "failed",
        )
        if event_sink is not None:
            from finengine.skills.result_events import (  # noqa: PLC0415
                typed_error_event,
                typed_result_event,
                typed_results,
            )
            root_results = typed_results(result.evidence_by_key.get(record.graph.root, ()))
            for value in root_results:
                event_sink(typed_result_event(value))
            if not result.succeeded:
                partial = typed_results(
                    value
                    for values in result.evidence_by_key.values()
                    for value in values
                )
                event_sink(typed_error_event(
                    code="graph_execution_failed",
                    message="PlanGraph execution did not reach a successful terminal state",
                    partial_results=partial,
                ))
        effective_passport = record.run_passport or discovered_passport
        if result.succeeded and effective_passport is not None:
            self._append_subject_profile(
                owner_id=owner_id, case_id=case_id, record=record,
                passport=effective_passport, result=result,
            )
        return result

    def _persist_artifacts(
        self, owner_id: object, case_id: str, record: PlanRunRecord, node_args: Mapping[NodeKey, NodeArgs],
        leaf_keys: set[NodeKey], node_runs: Mapping[NodeKey, Any], evidence_by_key: Mapping[NodeKey, tuple],
    ) -> None:
        """Результаты успешных узлов — артефактами Case: из них собирается индекс, доступный следующим планам."""
        nodes = {n.node_key: n for n in record.graph.nodes}
        for key, run in node_runs.items():
            payloads = evidence_by_key.get(key, ())
            if run.status != "succeeded" or not payloads or key in leaf_keys:
                continue
            args = node_args[key]
            dependency_ids = [
                eid
                for dep in nodes[key].requires
                for value in evidence_by_key.get(dep, ())
                for eid in [evidence_id_of(value)]
                if eid
            ]
            grouped: dict[str, list[Any]] = {}
            for value in payloads:
                grouped.setdefault(_payload_output_type(value), []).append(value)
            for output_type, values in grouped.items():
                # `read.override` becomes active only when its audit event is
                # committed.  Persisting the wrapper as a normal Case artifact
                # before that commit would create a crash window where an
                # unaudited correction silently wins MetricEvidence selection.
                # Its PlanRun/NodeRun evidence is already durable; the active
                # wrapper is persisted atomically with OverrideAuditEvent.
                if key.atom_name == "read.override" and output_type == "UserProvidedMetric":
                    continue
                self.store.put_artifact(
                    owner_id, case_id, node_key=key, output_type=output_type,
                    payloads=values, concept=args.concept, entity_ref=args.entity_ref,
                    period_ref=args.period_ref, basis=args.basis,
                    document_version=args.document_version, policy_version=args.policy_version,
                    dependency_evidence_ids=dependency_ids, passport=record.run_passport,
                )

    def _keep_finished_results(
        self, owner_id: object, case_id: str, record: PlanRunRecord, node_args: Mapping[NodeKey, NodeArgs],
        leaf_keys: set[NodeKey],
    ) -> None:
        """После падения исполнителя сохранить в Case то, что узлы уже успели получить (часть 14, ревью).

        Результат узла к этому моменту уже лежит в PlanRun (`on_result`); без артефактов Case цикл, который
        подхватит задачу, не увидел бы его и извлёк бы заново. Best effort, как закрытие узлов."""
        try:
            runs, evidence = self.store.load_node_runs(owner_id, case_id, record.plan_run_id)
            self._persist_artifacts(owner_id, case_id, record, node_args, leaf_keys, runs, evidence)
        except Exception:  # noqa: BLE001 — сохранение результатов не смеет подменять причину сбоя
            logging.getLogger(__name__).error(
                "keeping finished results failed run=%s", record.plan_run_id, exc_info=True,
            )

    def _close_unfinished_nodes(
        self, owner_id: object, case_id: str, plan_run_id: str, on_update: Callable, *, reason: str,
    ) -> None:
        """Перевести недошедшие до терминала узлы PlanRun в `failed`; сам PlanRun — в `failed`.

        Best effort: сбой закрытия не должен подменять исходное исключение."""
        try:
            runs, _ = self.store.load_node_runs(owner_id, case_id, plan_run_id)
            now = datetime.now(timezone.utc)
            for run in runs.values():
                if run.status != "succeeded" and run.status not in TERMINAL_FAILURE:
                    on_update(run.model_copy(update={"status": "failed", "finished_at": now, "error": reason}))
            self.store.update_plan_run_status(owner_id, case_id, plan_run_id, "failed")
        except Exception:  # noqa: BLE001 — наблюдаемость не смеет подменять причину сбоя
            logging.getLogger(__name__).error(
                "closing unfinished nodes failed run=%s", plan_run_id, exc_info=True,
            )

    # ------------------------------------------------------------------
    # Immutable override lifecycle
    def _metric_candidates(self, owner_id: object, case_id: str) -> list[MetricLike]:
        out: list[MetricLike] = []
        seen: set[str] = set()
        for row in self.store.list_artifacts(owner_id, case_id, valid_only=True):
            for value in row["payloads"]:
                if isinstance(value, (ExtractedMetric, CalculatedMetric, UserProvidedMetric)):
                    eid = metric_evidence_id(value)
                    if eid not in seen:
                        seen.add(eid)
                        out.append(value)
        for value in self.store.active_overrides(owner_id, case_id).values():
            if isinstance(value, UserProvidedMetric):
                eid = metric_evidence_id(value)
                if eid not in seen:
                    seen.add(eid)
                    out.append(value)
        return out

    def resolve_correction(
        self, *, owner_id: object, case_id: str, correction: CorrectionIntent,
    ) -> MetricLike | OverrideChoicePoint:
        candidates = self._metric_candidates(owner_id, case_id)
        if correction.evidence_id:
            candidates = [m for m in candidates if metric_evidence_id(m) == correction.evidence_id]
            if len(candidates) == 1:
                validate_correction(candidates[0], correction) if correction.action == "apply" else None
                return candidates[0]
            if not candidates:
                raise KeyError(f"evidence {correction.evidence_id!r} not found in this owner/Case")

        if correction.concept:
            candidates = [m for m in candidates if m.concept == correction.concept]
        if not candidates:
            raise KeyError("no Case evidence matches correction")

        # Explicit dimensions may disambiguate multiple same-concept facts,
        # but a single candidate with a wrong dimension must fail validation
        # rather than disappear into a misleading 'not found'.
        if len(candidates) > 1 and correction.period_ref:
            narrowed = [m for m in candidates if metric_value_unit_period(m)[2] == correction.period_ref]
            if narrowed:
                candidates = narrowed
        if len(candidates) > 1 and correction.unit:
            narrowed = [m for m in candidates if metric_value_unit_period(m)[1] == correction.unit]
            if narrowed:
                candidates = narrowed
        if len(candidates) > 1 and correction.basis:
            narrowed = [m for m in candidates if metric_value_unit_period(m)[3] == correction.basis]
            if narrowed:
                candidates = narrowed

        if len(candidates) != 1:
            return OverrideChoicePoint(
                concept=correction.concept,
                candidate_evidence_ids=[metric_evidence_id(m) for m in candidates],
                reason="correction matches more than one existing Case evidence; exact evidence choice is required",
            )
        if correction.action == "apply":
            validate_correction(candidates[0], correction)
        return candidates[0]

    def _root_for_override_evidence(self, owner_id: object, case_id: str, evidence_id: str) -> str:
        for event, _wrapper in self.store.override_history(owner_id, case_id):
            if event.override_evidence_id == evidence_id:
                return event.root_evidence_id
        return evidence_id

    def apply_correction(
        self,
        *,
        owner_id: object,
        case_id: str,
        correction: CorrectionIntent,
        idempotency_key: str,
        expected_revision: int | None = None,
        goal_ir: GoalIR | None = None,
        run_passport: RunPassport | None = None,
    ) -> UserProvidedMetric | OverrideChoicePoint:
        """Apply a correction through the real ``read.override`` graph atom.

        The audit event, not a normal artifact row, is the activation point.
        This ordering prevents an unaudited override from becoming visible if
        the process dies after atom execution but before audit persistence.
        """
        if correction.action != "apply":
            raise ValueError("apply_correction requires CorrectionIntent(action='apply')")

        # Command idempotency is checked before planning/execution so a client
        # retry never creates a second PlanRun/override evidence.
        for old_event, old_wrapper in self.store.override_history(owner_id, case_id):
            if old_event.idempotency_key == idempotency_key:
                if isinstance(old_wrapper, UserProvidedMetric):
                    return old_wrapper
                raise ValueError(f"idempotency key {idempotency_key!r} already belongs to a non-apply event")

        chosen = self.resolve_correction(owner_id=owner_id, case_id=case_id, correction=correction)
        if isinstance(chosen, OverrideChoicePoint):
            return chosen
        replaced = metric_evidence_id(chosen)
        root = self._root_for_override_evidence(owner_id, case_id, replaced)

        # Exact persisted artifact supplies identity dimensions for the
        # override Target. Active UserProvidedMetric is stored in audit, so
        # for re-override fall back to its own evidence fields.
        source_row = next((
            row for row in self.store.list_artifacts(owner_id, case_id, valid_only=True)
            if replaced in set(row.get("evidence_ids") or [])
        ), None)
        _value, _unit, period_ref, basis, document_version = metric_value_unit_period(chosen)
        entity_ref = source_row.get("entity_ref") if source_row else None
        policy_version = (source_row.get("policy_version") if source_row else None) or self.policy.policy_version
        target = Target(
            output_type="UserProvidedMetric", concept_id=chosen.concept,
            entity_ref=entity_ref, period_ref=period_ref, basis=basis,
            document_version=document_version, policy_version=policy_version,
        )
        if goal_ir is None:
            goal_ir = GoalIR(
                raw_query=correction.original_wording, intent_kind="extract_registered_metric",
                entity_ref=entity_ref, period_ref=period_ref, correction=correction,
            )
        root_extra = {
            "owner_id": owner_key(owner_id),
            "case_id": case_id,
            "overridden_evidence_id": replaced,
            "correction_json": correction.model_dump_json(),
        }
        planned = self.plan(
            owner_id=owner_id, case_id=case_id, goal_ir=goal_ir, target=target,
            run_passport=run_passport, root_extra=root_extra, expected_revision=expected_revision,
        )
        result = self.execute(
            owner_id=owner_id, case_id=case_id, plan_run_id=planned.record.plan_run_id,
        )
        if not result.succeeded:
            raise RuntimeError("read.override PlanRun failed")
        produced = result.evidence_by_key.get(planned.record.graph.root, ())
        wrapper = next((v for v in produced if isinstance(v, UserProvidedMetric)), None)
        if wrapper is None:
            raise RuntimeError("read.override PlanRun succeeded without UserProvidedMetric")
        override = wrapper.human_override
        if override.overridden_evidence_id != replaced:
            raise StalePlanRunError("read.override returned evidence bound to a different replaced evidence id")

        event = OverrideAuditEvent(
            action="apply", owner_id=owner_key(owner_id), case_id=case_id,
            root_evidence_id=root, replaced_evidence_id=replaced,
            override_evidence_id=override.evidence_id, reason=correction.reason,
            original_wording=correction.original_wording,
            idempotency_key=idempotency_key, case_revision=planned.record.case_revision + 1,
        )
        persisted = self.store.append_override_event(
            owner_id, case_id, event=event, wrapper=wrapper,
            expected_revision=planned.record.case_revision,
        )
        if persisted.event_id != event.event_id:
            for old_event, old_wrapper in self.store.override_history(owner_id, case_id, root):
                if old_event.idempotency_key == idempotency_key and isinstance(old_wrapper, UserProvidedMetric):
                    return old_wrapper
        self.store.invalidate_dependents(
            owner_id, case_id, [replaced], reason="active_override_changed",
        )
        return wrapper

    def cancel_correction(
        self,
        *,
        owner_id: object,
        case_id: str,
        correction: CorrectionIntent,
        idempotency_key: str,
        expected_revision: int | None = None,
    ) -> OverrideAuditEvent | OverrideChoicePoint:
        if correction.action != "cancel":
            raise ValueError("cancel_correction requires CorrectionIntent(action='cancel')")
        active = self.store.active_overrides(owner_id, case_id)
        choices: list[tuple[str, UserProvidedMetric]] = []
        for root, wrapper in active.items():
            if not isinstance(wrapper, UserProvidedMetric):
                continue
            if correction.evidence_id and correction.evidence_id not in {root, wrapper.human_override.evidence_id}:
                continue
            if correction.concept and wrapper.concept != correction.concept:
                continue
            if correction.period_ref and wrapper.period_ref != correction.period_ref:
                continue
            choices.append((root, wrapper))
        if not choices:
            raise KeyError("no active override matches cancellation")
        if len(choices) > 1:
            return OverrideChoicePoint(
                concept=correction.concept,
                candidate_evidence_ids=[w.human_override.evidence_id for _, w in choices],
                reason="cancellation matches more than one active override; exact evidence choice is required",
            )
        root, wrapper = choices[0]
        event = OverrideAuditEvent(
            action="cancel", owner_id=owner_key(owner_id), case_id=case_id,
            root_evidence_id=root, replaced_evidence_id=wrapper.human_override.evidence_id,
            override_evidence_id=None, reason=correction.reason,
            original_wording=correction.original_wording,
            idempotency_key=idempotency_key, case_revision=1,
        )
        persisted = self.store.append_override_event(
            owner_id, case_id, event=event, wrapper=None, expected_revision=expected_revision,
        )
        if persisted.event_id == event.event_id:
            self.store.invalidate_dependents(
                owner_id, case_id, [wrapper.human_override.evidence_id], reason="active_override_cancelled",
            )
        return persisted


__all__ = ["CaseRuntime", "PlannedCaseRun"]
