"""Case во временной SQLite и артефакты в нём — для тестов `ouro_view` и `aifa_goal`.

Каталоги тестов не пакеты: модуль импортируют по имени, как `turn_harness.py`.
"""
from __future__ import annotations

from pathlib import Path

from finengine.contracts import DynamicArtifact, NodeKey, PlanPolicy
from finengine.skills.case_runtime import CaseRuntime
from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.dynamic_run import case_rows
from finengine.store.cases import SqlCaseStore

OWNER = 7
CASE = "project-1"


def runtime_in(tmp_path: Path) -> CaseRuntime:
    runtime = CaseRuntime(
        store=SqlCaseStore(f"sqlite:///{tmp_path / 'case.db'}"),
        registry=AtomRegistry([]),
        policy=PlanPolicy(policy_version="test"),
    )
    runtime.ensure_case(owner_id=OWNER, case_id=CASE)
    return runtime


def put_artifact(runtime: CaseRuntime, name: str, payload: dict) -> None:
    """Артефакт `dyn:<name>`: самый простой результат, у которого есть id и значение в таблице."""
    artifact = DynamicArtifact(
        output_type=f"dyn:{name}",
        payload=payload,
        origin=["generated"],
        producer=f"eph.{name}@1",
        input_evidence_ids=[],
    )
    node = NodeKey(atom_name=f"eph.{name}", atom_version="1", args_fingerprint=name)
    runtime.store.put_artifact(
        OWNER,
        CASE,
        node_key=node,
        output_type=artifact.output_type,
        payloads=[artifact],
        concept=None,
        entity_ref=None,
        period_ref=None,
        basis=None,
        document_version=None,
        policy_version="test",
    )


def rows_of(runtime: CaseRuntime) -> tuple[dict, ...]:
    return tuple(case_rows(runtime.store, OWNER, CASE))
