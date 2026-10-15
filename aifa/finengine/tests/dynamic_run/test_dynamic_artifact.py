"""`DynamicArtifact` и `EphemeralAtomSource`: контракт, хранение в Case, поиск в AvailableIndex."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from finengine.contracts import (
    DynamicArtifact,
    EphemeralAtomSource,
    ExtractedMetric,
    GoalIR,
    NodeKey,
    PlanPolicy,
    ScalarFact,
)
from finengine.skills.case_runtime import CaseRuntime, _payload_output_type
from finengine.skills.spec.atom_registry import AtomDef, AtomRegistry
from finengine.skills.spec.graph_executor import ExecutionBudget
from finengine.skills.spec.target import OutputSpec, Target
from finengine.store.cases import SqlCaseStore

OWNER = "owner-1"
CASE = "case-1"
POLICY = PlanPolicy(policy_version="p1-test")


def _runtime(tmp_path: Path) -> CaseRuntime:
    runtime = CaseRuntime(
        store=SqlCaseStore(f"sqlite:///{tmp_path / 'dynamic.db'}"),
        registry=AtomRegistry([]), policy=POLICY,
    )
    runtime.ensure_case(owner_id=OWNER, case_id=CASE)
    return runtime


def _artifact(output_type: str = "dyn:ebitda_vs_brent", **payload) -> DynamicArtifact:
    return DynamicArtifact(
        output_type=output_type, payload=payload or {"correlation": 0.82},
        origin=["generated"], producer="eph.ebitda_vs_brent@abc123",
        input_evidence_ids=["ev-1", "ev-2"],
    )


def _put(runtime: CaseRuntime, node_key: NodeKey, values: list, output_type: str) -> None:
    runtime.store.put_artifact(
        OWNER, CASE, node_key=node_key, output_type=output_type, payloads=values,
        concept=None, entity_ref=None, period_ref=None, basis=None,
        document_version=None, policy_version=POLICY.policy_version,
    )


def _node(atom: str) -> NodeKey:
    return NodeKey(atom_name=atom, atom_version="abc123", args_fingerprint="args")


def test_output_type_must_start_with_dyn() -> None:
    with pytest.raises(ValidationError, match="dyn:"):
        _artifact(output_type="ExtractedMetric")

    assert _artifact(output_type="dyn:x").output_type == "dyn:x"


def test_contract_is_frozen_and_rejects_unknown_fields() -> None:
    artifact = _artifact()

    with pytest.raises(ValidationError):
        artifact.producer = "eph.other@1"
    with pytest.raises(ValidationError):
        DynamicArtifact(output_type="dyn:x", payload={}, origin=["generated"],
                        producer="eph.x@1", лишнее=1)


def test_origin_only_known_values() -> None:
    with pytest.raises(ValidationError):
        DynamicArtifact(output_type="dyn:x", payload={}, origin=["guessed"], producer="eph.x@1")


def test_payload_output_type_is_dyn_type_for_dynamic_and_class_name_otherwise() -> None:
    assert _payload_output_type(_artifact("dyn:a")) == "dyn:a"
    assert _payload_output_type(_artifact("dyn:b")) == "dyn:b"
    assert _payload_output_type(EphemeralAtomSource(evidence_id="h", name="n", request={})) == (
        "EphemeralAtomSource"
    )


def test_roundtrip_through_case_store(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    artifact = _artifact(series=[1.0, 2.5], note="ок")
    _put(runtime, _node("eph.ebitda_vs_brent"), [artifact], _payload_output_type(artifact))

    # Свежий экземпляр хранилища: читаем то, что реально лежит в БД.
    fresh = SqlCaseStore(f"sqlite:///{tmp_path / 'dynamic.db'}")
    rows = fresh.list_artifacts(OWNER, CASE)

    assert [row["output_type"] for row in rows] == ["dyn:ebitda_vs_brent"]
    restored = rows[0]["payloads"][0]
    assert isinstance(restored, DynamicArtifact)
    assert restored == artifact
    assert rows[0]["evidence_ids"] == [artifact.evidence_id]
    assert fresh.find_evidence(OWNER, CASE, artifact.evidence_id) == [artifact]


def test_available_index_finds_artifact_by_dyn_type(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    first, second = _artifact("dyn:first"), _artifact("dyn:second")
    _put(runtime, _node("eph.first"), [first], _payload_output_type(first))
    _put(runtime, _node("eph.second"), [second], _payload_output_type(second))

    index = runtime.available_index(owner_id=OWNER, case_id=CASE)

    leaf = index.lookup("dyn:first", policy_version=POLICY.policy_version)
    assert leaf is not None and leaf.evidence == (first,)
    assert leaf.node_key.atom_name == "eph.first"
    other = index.lookup("dyn:second", policy_version=POLICY.policy_version)
    assert other is not None and other.evidence == (second,)
    # Под именем класса динамические результаты не хранятся.
    assert index.lookup("DynamicArtifact", policy_version=POLICY.policy_version) is None


def test_neighbour_artifacts_are_read_as_before(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    fact = ScalarFact(
        produced_by_atom="read.test", produced_by_atom_version="1.0.0", source_ref="document:t",
        concept="SALES", period_ref="FY2024", value=100.0, unit="RUB",
    )
    metric = ExtractedMetric(scalar_fact=fact, concept="SALES", period_ref="FY2024")
    artifact = _artifact()
    _put(runtime, _node("read.test"), [metric], _payload_output_type(metric))
    _put(runtime, _node("eph.ebitda_vs_brent"), [artifact], _payload_output_type(artifact))

    rows = runtime.store.list_artifacts(OWNER, CASE)

    assert {row["output_type"] for row in rows} == {"ExtractedMetric", "dyn:ebitda_vs_brent"}
    by_type = {row["output_type"]: row["payloads"][0] for row in rows}
    assert by_type["ExtractedMetric"] == metric
    assert by_type["dyn:ebitda_vs_brent"] == artifact
    index = runtime.available_index(owner_id=OWNER, case_id=CASE)
    assert index.lookup("ExtractedMetric", policy_version=POLICY.policy_version) is not None
    assert index.lookup("dyn:ebitda_vs_brent", policy_version=POLICY.policy_version) is not None


@pytest.mark.parametrize("code", ["def run(inputs):\n    return {}\n", ""])
def test_atom_source_roundtrip_with_and_without_code(tmp_path: Path, code: str) -> None:
    runtime = _runtime(tmp_path)
    source = EphemeralAtomSource(
        evidence_id="a1b2c3", name="ebitda_vs_brent",
        request={"name": "ebitda_vs_brent", "inputs": ["ExtractedMetric"]}, code=code,
    )
    _put(runtime, _node("eph.ebitda_vs_brent"), [source], _payload_output_type(source))

    rows = runtime.store.list_artifacts(OWNER, CASE)

    restored = rows[0]["payloads"][0]
    assert isinstance(restored, EphemeralAtomSource)
    assert restored == source
    assert restored.code == code
    assert rows[0]["evidence_ids"] == ["a1b2c3"]


def _runtime_with(tmp_path: Path, atom: AtomDef) -> CaseRuntime:
    return CaseRuntime(
        store=SqlCaseStore(f"sqlite:///{tmp_path / 'dynamic.db'}"), registry=AtomRegistry([atom]),
        policy=POLICY, budget=ExecutionBudget(max_retries=0),
    )


def _produce_dynamic(args, ctx) -> list:  # noqa: ARG001
    return [_artifact("dyn:ebitda_vs_brent")]


def test_execute_stores_atom_result_under_dyn_type_and_lookup_is_exact(tmp_path: Path) -> None:
    """Весь путь без ручного `put_artifact`: plan → execute → хранилище → новый экземпляр
    рантайма → `AvailableIndex`. Чужие сущность и версия документа результат не находят."""
    atom = AtomDef(
        name="eph.ebitda_vs_brent", version="abc123", produces=(OutputSpec("dyn:ebitda_vs_brent"),),
        sandbox_profile="compute_no_network", execution_kind="deterministic",
        cache_policy="run_scoped", cost_class="cheap", executor=_produce_dynamic,
        key_fields=("concept", "entity_ref", "period_ref", "basis", "document_version", "policy_version"),
    )

    first = _runtime_with(tmp_path, atom)
    first.ensure_case(owner_id=OWNER, case_id=CASE)
    first.register_document(
        owner_id=OWNER, case_id=CASE, logical_name="report.pdf", document_id="doc-1", document_version="v1",
    )
    planned = first.plan(
        owner_id=OWNER, case_id=CASE, goal_ir=GoalIR(raw_query="x", intent_kind="extract_registered_metric"),
        target=Target(output_type="dyn:ebitda_vs_brent", entity_ref="ACME", period_ref="2025", basis="IFRS"),
    )
    assert first.execute(owner_id=OWNER, case_id=CASE, plan_run_id=planned.record.plan_run_id).succeeded

    stored = _runtime_with(tmp_path, atom).store.list_artifacts(OWNER, CASE)
    assert [(row["output_type"], row["entity_ref"], row["document_version"]) for row in stored] == [
        ("dyn:ebitda_vs_brent", "ACME", "v1"),
    ]
    index = _runtime_with(tmp_path, atom).available_index(owner_id=OWNER, case_id=CASE)
    same = dict(period_ref="2025", basis="IFRS", policy_version=POLICY.policy_version)
    assert index.lookup("dyn:ebitda_vs_brent", entity_ref="ACME", document_version="v1", **same) is not None
    assert index.lookup("dyn:ebitda_vs_brent", entity_ref="OTHER", document_version="v1", **same) is None
    assert index.lookup("dyn:ebitda_vs_brent", entity_ref="ACME", document_version="v2", **same) is None
