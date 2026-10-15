"""Срез 10: SQL persistence для `Case`/`PlanRun`/override audit.

Это намеренно НЕ обёртка над `grounding_bus`: межсообщенческое состояние
должно переживать другой process/service instance. Production deployment
использует общую server-side Postgres из `FINENGINE_STATE_DATABASE_URL`.
SQLite поддерживается только как локальная/test подложка: она позволяет
проверить recovery/reuse/audit без поднятого Postgres, но не заменяет
production-проверку Postgres locking/concurrency semantics.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel
from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Integer, MetaData, String, Table, UniqueConstraint,
    and_, select, update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from finengine import config
from finengine.contracts import (
    CaseDocumentRef, CaseIdentity, CaseSnapshot, CaseTarget, GoalIR, NodeKey, NodeRun,
    MonitoringCapacity, OverrideAuditEvent, PlanRunRecord, RunPassport, SubjectProfile,
)
from finengine.contracts.base import evidence_id_of, result_id_of, utc_now
from finengine.store.engine import движок, схема

_metadata = MetaData()

CASE = Table(
    "analysis_case", _metadata,
    Column("owner_key", String(64), primary_key=True),
    Column("case_id", String(128), primary_key=True),
    Column("tenant_key", String(128), nullable=True),
    Column("revision", Integer, nullable=False, default=0),
    Column("snapshot", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

CASE_DOCUMENT = Table(
    "analysis_case_document", _metadata,
    Column("owner_key", String(64), primary_key=True),
    Column("case_id", String(128), primary_key=True),
    Column("logical_name", String(512), primary_key=True),
    Column("document_version", String(128), primary_key=True),
    Column("document_id", String(128), nullable=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("added_at", DateTime(timezone=True), nullable=False),
)

CASE_ARTIFACT = Table(
    "analysis_case_artifact", _metadata,
    Column("owner_key", String(64), primary_key=True),
    Column("case_id", String(128), primary_key=True),
    Column("artifact_id", String(128), primary_key=True),
    Column("output_type", String(128), nullable=False, index=True),
    Column("node_key", JSON, nullable=False),
    Column("concept", String(256), nullable=True),
    Column("entity_ref", String(512), nullable=True),
    Column("period_ref", String(256), nullable=True),
    Column("basis", String(512), nullable=True),
    Column("document_version", String(128), nullable=True),
    Column("policy_version", String(128), nullable=True),
    Column("passport_fingerprint", String(128), nullable=True),
    Column("evidence_ids", JSON, nullable=False),
    Column("dependency_evidence_ids", JSON, nullable=False),
    Column("payloads", JSON, nullable=False),
    Column("valid", Boolean, nullable=False, default=True),
    Column("invalid_reason", String(512), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

PLAN_RUN = Table(
    "analysis_plan_run", _metadata,
    Column("owner_key", String(64), primary_key=True),
    Column("case_id", String(128), primary_key=True),
    Column("plan_run_id", String(128), primary_key=True),
    Column("record", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

PLAN_NODE_RUN = Table(
    "analysis_plan_node_run", _metadata,
    Column("owner_key", String(64), primary_key=True),
    Column("case_id", String(128), primary_key=True),
    Column("plan_run_id", String(128), primary_key=True),
    Column("node_key_hash", String(64), primary_key=True),
    Column("node_key", JSON, nullable=False),
    Column("node_run", JSON, nullable=False),
    Column("evidence", JSON, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

OVERRIDE_EVENT = Table(
    "analysis_override_event", _metadata,
    Column("owner_key", String(64), primary_key=True),
    Column("case_id", String(128), primary_key=True),
    Column("event_id", String(64), primary_key=True),
    Column("root_evidence_id", String(128), nullable=False, index=True),
    Column("case_revision", Integer, nullable=False),
    Column("action", String(16), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("event", JSON, nullable=False),
    Column("wrapper", JSON, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("owner_key", "case_id", "idempotency_key", name="uq_case_override_idempotency"),
)

SUBJECT_PROFILE_HISTORY = Table(
    "subject_profile_history", _metadata,
    Column("owner_key", String(64), primary_key=True),
    Column("subject_key", String(128), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("snapshot_id", String(128), nullable=False, unique=True),
    Column("plan_run_id", String(128), nullable=False),
    Column("snapshot", JSON, nullable=False),
    Column("serialized_bytes", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "owner_key", "subject_key", "plan_run_id",
        name="uq_subject_profile_plan_run",
    ),
)


class CasePersistenceError(RuntimeError):
    pass


class CaseConflictError(CasePersistenceError):
    def __init__(self, *, expected: int, actual: int):
        self.expected = expected
        self.actual = actual
        super().__init__(f"stale Case revision: expected={expected}, actual={actual}")


class StalePlanRunError(CasePersistenceError):
    pass


class CaseOwnerError(CasePersistenceError):
    pass


def owner_key(owner_id: object) -> str:
    value = str(owner_id).strip()
    if not value:
        raise ValueError("Case requires non-empty owner_id")
    return value[:64]


def _json_model(model: BaseModel | None) -> dict | None:
    return None if model is None else model.model_dump(mode="json")


def _pack_value(value: Any) -> dict:
    if isinstance(value, BaseModel):
        return {"kind": "contract", "type": type(value).__name__, "payload": value.model_dump(mode="json")}
    return {"kind": "raw", "payload": value}


def _unpack_value(doc: dict) -> Any:
    if doc.get("kind") != "contract":
        return doc.get("payload")
    import finengine.contracts as contracts  # noqa: PLC0415

    cls = getattr(contracts, str(doc.get("type") or ""), None)
    if cls is None:
        raise StalePlanRunError(f"unknown persisted contract type {doc.get('type')!r}")
    return cls.model_validate(doc.get("payload") or {})


def _node_hash(key: NodeKey) -> str:
    raw = json.dumps(key.model_dump(mode="json"), sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def artifact_id_for(node_key: NodeKey, output_type: str, evidence_ids: Sequence[str]) -> str:
    raw = json.dumps({
        "node": node_key.model_dump(mode="json"), "output_type": output_type,
        "evidence_ids": sorted(str(x) for x in evidence_ids),
    }, sort_keys=True, ensure_ascii=False).encode()
    return "artifact:" + hashlib.sha256(raw).hexdigest()


def _dialect_insert(conn, table):
    """INSERT with ON CONFLICT support for the two supported SQL test/runtime dialects."""
    name = conn.dialect.name
    if name == "postgresql":
        return pg_insert(table)
    if name == "sqlite":
        return sqlite_insert(table)
    raise RuntimeError(f"Case persistence does not support SQL dialect {name!r}")


def passport_fingerprint(passport: RunPassport | None) -> str | None:
    if passport is None:
        return None
    payload = {
        "profile_id": passport.classification.profile_id,
        "standard": passport.classification.standard,
        "company_type": passport.classification.company_type,
        "language": passport.classification.language,
        "entity_id": passport.entity.entity_id,
        "registry_version": passport.registry_version,
        "methodology_version": passport.methodology_version,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class SqlCaseStore:
    """Owner-scoped Case store. Postgres is production; SQLite is test-only."""

    def __init__(self, url: str):
        if not (url or "").strip():
            raise RuntimeError("Case persistence requires FINENGINE_STATE_DATABASE_URL")
        self._engine = движок(url, pool_pre_ping=True, future=True, echo=False)
        if self._engine.dialect.name not in {"postgresql", "sqlite"}:
            raise RuntimeError(
                f"Case persistence supports PostgreSQL and test SQLite, got {self._engine.dialect.name!r}"
            )
        _metadata.create_all(self._engine)

    def ensure_case(self, identity: CaseIdentity) -> CaseSnapshot:
        owner = owner_key(identity.owner_id)
        now = utc_now()
        initial = CaseSnapshot(identity=identity, created_at=now, updated_at=now)
        with self._engine.begin() as conn:
            stmt = _dialect_insert(conn, CASE).values(
                owner_key=owner, case_id=identity.case_id, tenant_key=identity.tenant_id,
                revision=0, snapshot=initial.model_dump(mode="json"), created_at=now, updated_at=now,
            ).on_conflict_do_nothing(index_elements=[CASE.c.owner_key, CASE.c.case_id])
            conn.execute(stmt)
        loaded = self.load_case(identity.owner_id, identity.case_id)
        assert loaded is not None
        if loaded.identity.tenant_id != identity.tenant_id:
            raise CaseOwnerError("Case tenant mismatch")
        return loaded

    def load_case(self, owner_id: object, case_id: str) -> CaseSnapshot | None:
        owner = owner_key(owner_id)
        with self._engine.begin() as conn:
            row = conn.execute(select(CASE.c.snapshot).where(
                CASE.c.owner_key == owner, CASE.c.case_id == case_id,
            )).first()
        return CaseSnapshot.model_validate(row[0]) if row else None

    def _lock_case(self, conn, owner: str, case_id: str):
        row = conn.execute(select(CASE).where(
            CASE.c.owner_key == owner, CASE.c.case_id == case_id,
        ).with_for_update()).mappings().first()
        if row is None:
            raise KeyError(f"Case {case_id!r} not found for owner")
        return row

    @staticmethod
    def _check_revision(row, expected_revision: int | None) -> None:
        if expected_revision is not None and int(row["revision"]) != expected_revision:
            raise CaseConflictError(expected=expected_revision, actual=int(row["revision"]))

    @staticmethod
    def _write_snapshot_cas(conn, owner: str, case_id: str, *, old_revision: int, snap: CaseSnapshot) -> None:
        """Persist one Case revision atomically on PostgreSQL and SQLite.

        PostgreSQL additionally protects the row with ``FOR UPDATE``. SQLite
        ignores that clause, so the compare-and-swap predicate is what keeps
        two test/service instances from silently overwriting the same revision.
        """
        result = conn.execute(update(CASE).where(
            CASE.c.owner_key == owner, CASE.c.case_id == case_id, CASE.c.revision == old_revision,
        ).values(
            revision=snap.revision, snapshot=snap.model_dump(mode="json"), updated_at=snap.updated_at,
        ))
        if result.rowcount != 1:
            actual = conn.execute(select(CASE.c.revision).where(
                CASE.c.owner_key == owner, CASE.c.case_id == case_id,
            )).scalar_one_or_none()
            raise CaseConflictError(expected=old_revision, actual=int(actual if actual is not None else -1))

    def update_case_head(
        self, owner_id: object, case_id: str, *, goal_ir: GoalIR | None = None,
        target: CaseTarget | None = None, graph_hash: str | None = None,
        run_passport: RunPassport | None = None, run_passport_document_version: str | None = None,
        expected_revision: int | None = None,
    ) -> CaseSnapshot:
        owner = owner_key(owner_id)
        with self._engine.begin() as conn:
            row = self._lock_case(conn, owner, case_id)
            self._check_revision(row, expected_revision)
            snap = CaseSnapshot.model_validate(row["snapshot"])
            now = utc_now()
            snap = snap.model_copy(update={
                "revision": snap.revision + 1,
                "latest_goal_ir": goal_ir if goal_ir is not None else snap.latest_goal_ir,
                "latest_target": target if target is not None else snap.latest_target,
                "latest_graph_hash": graph_hash if graph_hash is not None else snap.latest_graph_hash,
                "run_passport": run_passport if run_passport is not None else snap.run_passport,
                "run_passport_document_version": (
                    run_passport_document_version
                    if run_passport is not None
                    else snap.run_passport_document_version
                ),
                "updated_at": now,
            })
            self._write_snapshot_cas(
                conn, owner, case_id, old_revision=int(row["revision"]), snap=snap,
            )
        return snap

    def put_ad_hoc_spec(
        self,
        owner_id: object,
        case_id: str,
        spec,
        *,
        expected_revision: int | None = None,
    ) -> CaseSnapshot:
        """Persist one temporary definition inside its owner-scoped Case."""
        from finengine.contracts import AdHocConceptSpec, AdHocFormulaSpec

        if not isinstance(spec, (AdHocConceptSpec, AdHocFormulaSpec)):
            raise TypeError(f"unsupported ad-hoc spec: {type(spec).__name__}")
        owner = owner_key(owner_id)
        with self._engine.begin() as conn:
            row = self._lock_case(conn, owner, case_id)
            self._check_revision(row, expected_revision)
            snap = CaseSnapshot.model_validate(row["snapshot"])
            concepts = dict(snap.ad_hoc_concepts)
            formulas = dict(snap.ad_hoc_formulas)
            if isinstance(spec, AdHocConceptSpec):
                concepts[spec.spec_id] = spec
            else:
                formulas[spec.spec_id] = spec
            now = utc_now()
            snap = snap.model_copy(update={
                "revision": snap.revision + 1,
                "ad_hoc_concepts": concepts,
                "ad_hoc_formulas": formulas,
                "updated_at": now,
            })
            self._write_snapshot_cas(
                conn, owner, case_id, old_revision=int(row["revision"]), snap=snap,
            )
        return snap

    def register_document(
        self, owner_id: object, case_id: str, *, logical_name: str, document_id: str,
        document_version: str, expected_revision: int | None = None,
    ) -> tuple[CaseSnapshot, list[str]]:
        """Activate exact content version and invalidate only its dependants."""
        if not document_version:
            raise ValueError("document_version/content hash is required for Case")
        owner = owner_key(owner_id)
        invalidated: list[str] = []
        with self._engine.begin() as conn:
            row = self._lock_case(conn, owner, case_id)
            self._check_revision(row, expected_revision)
            current = conn.execute(select(CASE_DOCUMENT).where(
                CASE_DOCUMENT.c.owner_key == owner, CASE_DOCUMENT.c.case_id == case_id,
                CASE_DOCUMENT.c.logical_name == logical_name, CASE_DOCUMENT.c.active.is_(True),
            )).mappings().all()
            if any(r["document_version"] == document_version and r["document_id"] == document_id for r in current):
                return CaseSnapshot.model_validate(row["snapshot"]), []
            old_versions = {str(r["document_version"]) for r in current}
            if current:
                conn.execute(update(CASE_DOCUMENT).where(
                    CASE_DOCUMENT.c.owner_key == owner, CASE_DOCUMENT.c.case_id == case_id,
                    CASE_DOCUMENT.c.logical_name == logical_name, CASE_DOCUMENT.c.active.is_(True),
                ).values(active=False))
            now = utc_now()
            stmt = _dialect_insert(conn, CASE_DOCUMENT).values(
                owner_key=owner, case_id=case_id, logical_name=logical_name,
                document_version=document_version, document_id=document_id, active=True, added_at=now,
            ).on_conflict_do_update(
                index_elements=[CASE_DOCUMENT.c.owner_key, CASE_DOCUMENT.c.case_id,
                                CASE_DOCUMENT.c.logical_name, CASE_DOCUMENT.c.document_version],
                set_={"document_id": document_id, "active": True, "added_at": now},
            )
            conn.execute(stmt)
            if old_versions:
                invalidated = self._invalidate_versions_locked(conn, owner, case_id, old_versions)
            snap = CaseSnapshot.model_validate(row["snapshot"])
            active_docs = [CaseDocumentRef(
                logical_name=r["logical_name"], document_id=r["document_id"],
                document_version=r["document_version"], active=True, added_at=r["added_at"],
            ) for r in conn.execute(select(CASE_DOCUMENT).where(
                CASE_DOCUMENT.c.owner_key == owner, CASE_DOCUMENT.c.case_id == case_id,
                CASE_DOCUMENT.c.active.is_(True),
            )).mappings()]
            passport_replaced = bool(
                snap.run_passport_document_version
                and snap.run_passport_document_version in old_versions
            )
            snap = snap.model_copy(update={
                "revision": snap.revision + 1,
                "active_documents": active_docs,
                "run_passport": None if passport_replaced else snap.run_passport,
                "run_passport_document_version": None if passport_replaced else snap.run_passport_document_version,
                "updated_at": now,
            })
            self._write_snapshot_cas(
                conn, owner, case_id, old_revision=int(row["revision"]), snap=snap,
            )
        return snap, invalidated

    def _invalidate_versions_locked(self, conn, owner: str, case_id: str, old_versions: set[str]) -> list[str]:
        rows = conn.execute(select(CASE_ARTIFACT).where(
            CASE_ARTIFACT.c.owner_key == owner, CASE_ARTIFACT.c.case_id == case_id,
            CASE_ARTIFACT.c.valid.is_(True),
        )).mappings().all()
        invalid_ids: set[str] = set()
        invalid_evidence: set[str] = set()
        changed = True
        while changed:
            changed = False
            for r in rows:
                aid = str(r["artifact_id"])
                if aid in invalid_ids:
                    continue
                direct = r["document_version"] in old_versions
                deps = set(r["dependency_evidence_ids"] or [])
                if direct or bool(deps & invalid_evidence):
                    invalid_ids.add(aid)
                    invalid_evidence.update(str(x) for x in (r["evidence_ids"] or []))
                    changed = True
        if invalid_ids:
            conn.execute(update(CASE_ARTIFACT).where(and_(
                CASE_ARTIFACT.c.owner_key == owner, CASE_ARTIFACT.c.case_id == case_id,
                CASE_ARTIFACT.c.artifact_id.in_(sorted(invalid_ids)),
            )).values(valid=False, invalid_reason="document_version_replaced"))
        return sorted(invalid_ids)

    def invalidate_dependents(
        self, owner_id: object, case_id: str, evidence_ids: Sequence[str], *, reason: str,
    ) -> list[str]:
        """Invalidate only artifacts transitively derived from selected evidence.

        The source evidence itself remains immutable/valid.  This is used when
        an override changes the active semantic selection: calculations that
        consumed the previous selection must be rebuilt, while the document
        fact stays available for audit and direct access.
        """
        owner = owner_key(owner_id)
        seeds = {str(x) for x in evidence_ids if str(x)}
        if not seeds:
            return []
        with self._engine.begin() as conn:
            self._lock_case(conn, owner, case_id)
            rows = conn.execute(select(CASE_ARTIFACT).where(
                CASE_ARTIFACT.c.owner_key == owner, CASE_ARTIFACT.c.case_id == case_id,
                CASE_ARTIFACT.c.valid.is_(True),
            )).mappings().all()
            invalid_ids: set[str] = set()
            invalid_evidence = set(seeds)
            changed = True
            while changed:
                changed = False
                for row in rows:
                    aid = str(row["artifact_id"])
                    if aid in invalid_ids:
                        continue
                    deps = {str(x) for x in (row["dependency_evidence_ids"] or [])}
                    if deps & invalid_evidence:
                        invalid_ids.add(aid)
                        invalid_evidence.update(str(x) for x in (row["evidence_ids"] or []))
                        changed = True
            if invalid_ids:
                conn.execute(update(CASE_ARTIFACT).where(and_(
                    CASE_ARTIFACT.c.owner_key == owner, CASE_ARTIFACT.c.case_id == case_id,
                    CASE_ARTIFACT.c.artifact_id.in_(sorted(invalid_ids)),
                )).values(valid=False, invalid_reason=reason))
        return sorted(invalid_ids)

    def active_documents(self, owner_id: object, case_id: str) -> list[CaseDocumentRef]:
        owner = owner_key(owner_id)
        with self._engine.begin() as conn:
            rows = conn.execute(select(CASE_DOCUMENT).where(
                CASE_DOCUMENT.c.owner_key == owner, CASE_DOCUMENT.c.case_id == case_id,
                CASE_DOCUMENT.c.active.is_(True),
            )).mappings().all()
        return [CaseDocumentRef(
            logical_name=r["logical_name"], document_id=r["document_id"],
            document_version=r["document_version"], active=True, added_at=r["added_at"],
        ) for r in rows]

    def put_artifact(
        self, owner_id: object, case_id: str, *, node_key: NodeKey, output_type: str,
        payloads: Sequence[Any], concept: str | None, entity_ref: str | None, period_ref: str | None,
        basis: str | None, document_version: str | None, policy_version: str | None,
        dependency_evidence_ids: Sequence[str] = (), passport: RunPassport | None = None,
    ) -> str:
        owner = owner_key(owner_id)
        evidence_ids = tuple(x for x in (result_id_of(v) for v in payloads) if x)
        aid = artifact_id_for(node_key, output_type, evidence_ids)
        now = utc_now()
        with self._engine.begin() as conn:
            self._lock_case(conn, owner, case_id)
            stmt = _dialect_insert(conn, CASE_ARTIFACT).values(
                owner_key=owner, case_id=case_id, artifact_id=aid, output_type=output_type,
                node_key=node_key.model_dump(mode="json"), concept=concept, entity_ref=entity_ref,
                period_ref=period_ref, basis=basis, document_version=document_version,
                policy_version=policy_version, passport_fingerprint=passport_fingerprint(passport),
                evidence_ids=list(evidence_ids), dependency_evidence_ids=list(dict.fromkeys(dependency_evidence_ids)),
                payloads=[_pack_value(v) for v in payloads], valid=True, invalid_reason=None, created_at=now,
            ).on_conflict_do_nothing(index_elements=[
                CASE_ARTIFACT.c.owner_key, CASE_ARTIFACT.c.case_id, CASE_ARTIFACT.c.artifact_id,
            ])
            conn.execute(stmt)
        return aid

    def list_artifacts(self, owner_id: object, case_id: str, *, valid_only: bool = True) -> list[dict]:
        owner = owner_key(owner_id)
        q = select(CASE_ARTIFACT).where(
            CASE_ARTIFACT.c.owner_key == owner, CASE_ARTIFACT.c.case_id == case_id,
        )
        if valid_only:
            q = q.where(CASE_ARTIFACT.c.valid.is_(True))
        with self._engine.begin() as conn:
            rows = conn.execute(q.order_by(CASE_ARTIFACT.c.created_at)).mappings().all()
        result = []
        for row in rows:
            d = dict(row)
            d["node_key"] = NodeKey.model_validate(d["node_key"])
            d["payloads"] = tuple(_unpack_value(x) for x in (d["payloads"] or []))
            # Rows written before result ids became total may have an empty
            # list for service contracts. Derive the same ids on read so reuse
            # and `find_evidence` remain correct without a data migration.
            d["evidence_ids"] = list(dict.fromkeys((
                *(str(x) for x in (d.get("evidence_ids") or [])),
                *(eid for value in d["payloads"] if (eid := result_id_of(value))),
            )))
            result.append(d)
        return result

    def find_evidence(self, owner_id: object, case_id: str, evidence_id: str, *, valid_only: bool = False) -> list[Any]:
        found: list[Any] = []
        for artifact in self.list_artifacts(owner_id, case_id, valid_only=valid_only):
            if evidence_id in set(artifact["evidence_ids"] or []):
                found.extend(v for v in artifact["payloads"] if result_id_of(v) == evidence_id)
        override_values = (
            self.active_overrides(owner_id, case_id).values()
            if valid_only
            else (wrapper for _event, wrapper in self.override_history(owner_id, case_id) if wrapper is not None)
        )
        found.extend(value for value in override_values if result_id_of(value) == evidence_id)
        return found

    def create_plan_run(self, record: PlanRunRecord) -> PlanRunRecord:
        owner = owner_key(record.owner_id)
        now = utc_now()
        with self._engine.begin() as conn:
            self._lock_case(conn, owner, record.case_id)
            existing = conn.execute(select(PLAN_RUN.c.record).where(
                PLAN_RUN.c.owner_key == owner, PLAN_RUN.c.case_id == record.case_id,
                PLAN_RUN.c.plan_run_id == record.plan_run_id,
            ).with_for_update()).first()
            if existing:
                old = PlanRunRecord.model_validate(existing[0])
                if (old.graph_hash, old.registry_fingerprint, old.policy_version) != (
                    record.graph_hash, record.registry_fingerprint, record.policy_version,
                ):
                    raise StalePlanRunError("PlanRun id already exists with incompatible graph/registry/policy")
                return old
            conn.execute(PLAN_RUN.insert().values(
                owner_key=owner, case_id=record.case_id, plan_run_id=record.plan_run_id,
                record=record.model_dump(mode="json"), created_at=now, updated_at=now,
            ))
        return record

    def load_plan_run(self, owner_id: object, case_id: str, plan_run_id: str) -> PlanRunRecord | None:
        owner = owner_key(owner_id)
        with self._engine.begin() as conn:
            row = conn.execute(select(PLAN_RUN.c.record).where(
                PLAN_RUN.c.owner_key == owner, PLAN_RUN.c.case_id == case_id,
                PLAN_RUN.c.plan_run_id == plan_run_id,
            )).first()
        return PlanRunRecord.model_validate(row[0]) if row else None

    def update_plan_run_status(self, owner_id: object, case_id: str, plan_run_id: str, status: str) -> PlanRunRecord:
        owner = owner_key(owner_id)
        with self._engine.begin() as conn:
            row = conn.execute(select(PLAN_RUN.c.record).where(
                PLAN_RUN.c.owner_key == owner, PLAN_RUN.c.case_id == case_id,
                PLAN_RUN.c.plan_run_id == plan_run_id,
            ).with_for_update()).first()
            if not row:
                raise KeyError(plan_run_id)
            record = PlanRunRecord.model_validate(row[0]).model_copy(update={"status": status, "updated_at": utc_now()})
            conn.execute(update(PLAN_RUN).where(
                PLAN_RUN.c.owner_key == owner, PLAN_RUN.c.case_id == case_id,
                PLAN_RUN.c.plan_run_id == plan_run_id,
            ).values(record=record.model_dump(mode="json"), updated_at=record.updated_at))
        return record

    def save_node_run(
        self, owner_id: object, case_id: str, plan_run_id: str, node_run: NodeRun,
        evidence: Sequence[Any] = (),
    ) -> None:
        owner = owner_key(owner_id)
        key_hash = _node_hash(node_run.node_key)
        now = utc_now()
        with self._engine.begin() as conn:
            self._lock_case(conn, owner, case_id)
            row = conn.execute(select(PLAN_NODE_RUN.c.node_run).where(
                PLAN_NODE_RUN.c.owner_key == owner, PLAN_NODE_RUN.c.case_id == case_id,
                PLAN_NODE_RUN.c.plan_run_id == plan_run_id, PLAN_NODE_RUN.c.node_key_hash == key_hash,
            ).with_for_update()).first()
            if row:
                old = NodeRun.model_validate(row[0])
                rank = {"planned": 0, "ready": 1, "running": 2, "succeeded": 3, "failed": 3, "blocked": 3, "skipped": 3}
                if old.attempt > node_run.attempt or (old.attempt == node_run.attempt and rank[old.status] > rank[node_run.status]):
                    return
                conn.execute(update(PLAN_NODE_RUN).where(
                    PLAN_NODE_RUN.c.owner_key == owner, PLAN_NODE_RUN.c.case_id == case_id,
                    PLAN_NODE_RUN.c.plan_run_id == plan_run_id, PLAN_NODE_RUN.c.node_key_hash == key_hash,
                ).values(node_run=node_run.model_dump(mode="json"), evidence=[_pack_value(v) for v in evidence], updated_at=now))
            else:
                conn.execute(PLAN_NODE_RUN.insert().values(
                    owner_key=owner, case_id=case_id, plan_run_id=plan_run_id, node_key_hash=key_hash,
                    node_key=node_run.node_key.model_dump(mode="json"), node_run=node_run.model_dump(mode="json"),
                    evidence=[_pack_value(v) for v in evidence], updated_at=now,
                ))

    def load_node_runs(self, owner_id: object, case_id: str, plan_run_id: str) -> tuple[dict[NodeKey, NodeRun], dict[NodeKey, tuple[Any, ...]]]:
        owner = owner_key(owner_id)
        with self._engine.begin() as conn:
            rows = conn.execute(select(PLAN_NODE_RUN).where(
                PLAN_NODE_RUN.c.owner_key == owner, PLAN_NODE_RUN.c.case_id == case_id,
                PLAN_NODE_RUN.c.plan_run_id == plan_run_id,
            )).mappings().all()
        runs: dict[NodeKey, NodeRun] = {}
        evidence: dict[NodeKey, tuple[Any, ...]] = {}
        for row in rows:
            key = NodeKey.model_validate(row["node_key"])
            runs[key] = NodeRun.model_validate(row["node_run"])
            evidence[key] = tuple(_unpack_value(v) for v in (row["evidence"] or []))
        return runs, evidence

    def append_override_event(
        self, owner_id: object, case_id: str, *, event: OverrideAuditEvent,
        wrapper: BaseModel | None, expected_revision: int | None = None,
    ) -> OverrideAuditEvent:
        owner = owner_key(owner_id)
        if event.owner_id != owner or event.case_id != case_id:
            raise CaseOwnerError("override event owner/case mismatch")
        with self._engine.begin() as conn:
            row = self._lock_case(conn, owner, case_id)
            # Idempotent replay wins over a stale expected revision: the same
            # command must return the event it already created, not fail just
            # because another Case mutation happened afterwards.
            existing = conn.execute(select(OVERRIDE_EVENT.c.event).where(
                OVERRIDE_EVENT.c.owner_key == owner, OVERRIDE_EVENT.c.case_id == case_id,
                OVERRIDE_EVENT.c.idempotency_key == event.idempotency_key,
            )).first()
            if existing:
                return OverrideAuditEvent.model_validate(existing[0])
            self._check_revision(row, expected_revision)
            new_rev = int(row["revision"]) + 1
            event = event.model_copy(update={"case_revision": new_rev})
            conn.execute(OVERRIDE_EVENT.insert().values(
                owner_key=owner, case_id=case_id, event_id=event.event_id,
                root_evidence_id=event.root_evidence_id, case_revision=new_rev, action=event.action,
                idempotency_key=event.idempotency_key, event=event.model_dump(mode="json"),
                wrapper=_pack_value(wrapper) if wrapper is not None else None, created_at=event.created_at,
            ))
            snap = CaseSnapshot.model_validate(row["snapshot"]).model_copy(update={
                "revision": new_rev, "updated_at": utc_now(),
            })
            self._write_snapshot_cas(
                conn, owner, case_id, old_revision=int(row["revision"]), snap=snap,
            )
        return event

    def override_history(self, owner_id: object, case_id: str, root_evidence_id: str | None = None) -> list[tuple[OverrideAuditEvent, Any | None]]:
        owner = owner_key(owner_id)
        q = select(OVERRIDE_EVENT).where(
            OVERRIDE_EVENT.c.owner_key == owner, OVERRIDE_EVENT.c.case_id == case_id,
        )
        if root_evidence_id is not None:
            q = q.where(OVERRIDE_EVENT.c.root_evidence_id == root_evidence_id)
        with self._engine.begin() as conn:
            rows = conn.execute(q.order_by(OVERRIDE_EVENT.c.case_revision, OVERRIDE_EVENT.c.event_id)).mappings().all()
        return [(
            OverrideAuditEvent.model_validate(r["event"]),
            _unpack_value(r["wrapper"]) if r["wrapper"] else None,
        ) for r in rows]

    def active_overrides(self, owner_id: object, case_id: str) -> dict[str, Any]:
        active: dict[str, Any] = {}
        for event, wrapper in self.override_history(owner_id, case_id):
            if event.action == "cancel":
                active.pop(event.root_evidence_id, None)
            elif wrapper is not None:
                active[event.root_evidence_id] = wrapper
        return active

    # ------------------------------------------------------------------
    # Slice 11: append-only subject history.  No UPDATE/DELETE method is
    # exposed for snapshots: corrections create a later version.
    def append_subject_profile(self, profile: SubjectProfile) -> SubjectProfile:
        owner = owner_key(profile.access.owner_id)
        with self._engine.begin() as conn:
            existing = conn.execute(select(SUBJECT_PROFILE_HISTORY.c.snapshot).where(
                SUBJECT_PROFILE_HISTORY.c.owner_key == owner,
                SUBJECT_PROFILE_HISTORY.c.subject_key == profile.subject_key.value,
                SUBJECT_PROFILE_HISTORY.c.plan_run_id == profile.plan_run_id,
            )).scalar_one_or_none()
            if existing is not None:
                return SubjectProfile.model_validate(existing)
            latest = conn.execute(select(SUBJECT_PROFILE_HISTORY).where(
                SUBJECT_PROFILE_HISTORY.c.owner_key == owner,
                SUBJECT_PROFILE_HISTORY.c.subject_key == profile.subject_key.value,
            ).order_by(SUBJECT_PROFILE_HISTORY.c.version.desc()).limit(1)).mappings().first()
            version = int(latest["version"]) + 1 if latest else 1
            previous_id = str(latest["snapshot_id"]) if latest else None
            stored = profile.model_copy(update={
                "version": version,
                "previous_snapshot_id": previous_id,
            })
            payload = stored.model_dump(mode="json")
            size = len(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8"))
            conn.execute(SUBJECT_PROFILE_HISTORY.insert().values(
                owner_key=owner, subject_key=stored.subject_key.value, version=version,
                snapshot_id=stored.snapshot_id, plan_run_id=stored.plan_run_id,
                snapshot=payload, serialized_bytes=size, created_at=stored.created_at,
            ))
        return stored

    def list_subject_profiles(
        self, owner_id: object, subject_key: str, *, limit: int | None = None,
    ) -> list[SubjectProfile]:
        owner = owner_key(owner_id)
        stmt = select(SUBJECT_PROFILE_HISTORY.c.snapshot).where(
            SUBJECT_PROFILE_HISTORY.c.owner_key == owner,
            SUBJECT_PROFILE_HISTORY.c.subject_key == subject_key,
        ).order_by(SUBJECT_PROFILE_HISTORY.c.version.asc())
        if limit is not None:
            # Limit to latest N while returning chronological order.
            stmt = select(SUBJECT_PROFILE_HISTORY.c.snapshot).where(
                SUBJECT_PROFILE_HISTORY.c.owner_key == owner,
                SUBJECT_PROFILE_HISTORY.c.subject_key == subject_key,
            ).order_by(SUBJECT_PROFILE_HISTORY.c.version.desc()).limit(limit)
        with self._engine.begin() as conn:
            rows = [SubjectProfile.model_validate(row[0]) for row in conn.execute(stmt).all()]
        return list(reversed(rows)) if limit is not None else rows

    def measure_subject_history(
        self, owner_id: object, *, active_subscriptions: int = 0,
        estimated_runs_per_day: float = 0.0,
    ) -> MonitoringCapacity:
        owner = owner_key(owner_id)
        with self._engine.begin() as conn:
            rows = conn.execute(select(
                SUBJECT_PROFILE_HISTORY.c.subject_key,
                SUBJECT_PROFILE_HISTORY.c.serialized_bytes,
            ).where(SUBJECT_PROFILE_HISTORY.c.owner_key == owner)).all()
        return MonitoringCapacity(
            subject_count=len({str(row[0]) for row in rows}),
            snapshot_count=len(rows),
            serialized_bytes=sum(int(row[1]) for row in rows),
            active_subscriptions=active_subscriptions,
            estimated_runs_per_day=estimated_runs_per_day,
        )


def ensure_case_schema(url: str) -> None:
    """Idempotent schema migration entry-point for a fresh Slice-10 install."""
    SqlCaseStore(url)


_cached: tuple[str, SqlCaseStore] | None = None


def get_case_store() -> SqlCaseStore:
    global _cached
    url = (config.settings.state_database_url or "").strip()
    if not url:
        raise RuntimeError(
            "Case persistence is not configured: FINENGINE_STATE_DATABASE_URL is empty; "
            "Slice 10 has no local-file fallback because Case must survive instance changes"
        )
    key = f"{url}|{схема()}"
    if _cached is not None and _cached[0] == key:
        return _cached[1]
    _cached = (key, SqlCaseStore(url))
    return _cached[1]


__all__ = [
    "SqlCaseStore", "get_case_store", "ensure_case_schema", "CaseConflictError",
    "StalePlanRunError", "CaseOwnerError", "evidence_id_of", "result_id_of", "passport_fingerprint",
]
