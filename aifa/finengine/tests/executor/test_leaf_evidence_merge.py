"""A2: доказательство узла, уже лежащего в `Case`, — ОБЪЕДИНЕНИЕ всех его листьев.

Живой прогон 25.09.2026 (Срез 32): гейт `verify.bounds` пишет в `Case` артефакты двух типов
под одним `node_key` — метрику и находку. `AvailableIndex` хранит их отдельными листьями, а
`GraphExecutor` засевал `NodeRun` через `{leaf.node_key: leaf}`: побеждал последний лист, и у
84 из 92 узлов метрика пропала (`EvidenceBundleV2` собрался из 8 фактов вместо 84). Какой тип
побеждает, зависело от порядка листьев — поэтому подставные тесты «везло».
"""
from __future__ import annotations

import pytest

from finengine.contracts import NodeKey, ScalarFact, VerificationFinding
from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.graph_executor import GraphExecutor, RunInputs, _leaves_by_node_key
from finengine.skills.spec.graph_planner import AvailableIndex, AvailableLeaf

KEY = NodeKey(atom_name="verify.bounds", atom_version="1.0.0", args_fingerprint="concept=REVENUE")


def _metric() -> ScalarFact:
    return ScalarFact(
        produced_by_atom="read.value", produced_by_atom_version="1.0.0", source_ref="loc-1",
        concept="REVENUE", period_ref="FY 2025", value=1500.0, unit="млн. руб.",
    )


def _finding() -> VerificationFinding:
    return VerificationFinding(
        produced_by_atom="verify.bounds", produced_by_atom_version="1.0.0", source_ref="loc-1",
        concept="REVENUE", check_kind="bounds", status="pass",
    )


def _index(order: tuple[str, str]) -> tuple[AvailableIndex, ScalarFact, VerificationFinding]:
    metric, finding = _metric(), _finding()
    leaves = {
        "METRIC": AvailableLeaf(node_key=KEY, evidence_ids=(metric.evidence_id,), evidence=(metric,)),
        "FINDING": AvailableLeaf(node_key=KEY, evidence_ids=(finding.evidence_id,), evidence=(finding,)),
    }
    index = AvailableIndex()
    for output_type in order:
        index.put(output_type, leaves[output_type], entity_ref="e1", period_ref="p1")
    return index, metric, finding


@pytest.mark.parametrize("order", [("METRIC", "FINDING"), ("FINDING", "METRIC")])
def test_leaves_of_two_output_types_under_one_node_key_are_merged_in_any_order(order):
    index, metric, finding = _index(order)

    merged = _leaves_by_node_key(index)

    assert list(merged) == [KEY]
    assert {item.evidence_id for item in merged[KEY].evidence} == {metric.evidence_id, finding.evidence_id}
    assert set(merged[KEY].evidence_ids) == {metric.evidence_id, finding.evidence_id}


def test_the_same_leaf_registered_under_an_alias_is_not_duplicated():
    metric = _metric()
    leaf = AvailableLeaf(node_key=KEY, evidence_ids=(metric.evidence_id,), evidence=(metric,))
    index = AvailableIndex()
    index.put("ExtractedMetric", leaf, entity_ref="e1", period_ref="p1")
    index.put("MetricEvidence", leaf, entity_ref="e1", period_ref="p1")

    merged = _leaves_by_node_key(index)

    assert merged[KEY].evidence == (metric,)


def test_the_same_evidence_carried_by_two_leaves_is_kept_once():
    metric = _metric()
    index = AvailableIndex()
    index.put("A", AvailableLeaf(node_key=KEY, evidence_ids=(metric.evidence_id,), evidence=(metric,)), entity_ref="e1")
    index.put("B", AvailableLeaf(node_key=KEY, evidence_ids=(metric.evidence_id,), evidence=(metric,)), entity_ref="e2")

    merged = _leaves_by_node_key(index)

    assert len(merged[KEY].evidence) == 1 and merged[KEY].evidence_ids == (metric.evidence_id,)


@pytest.mark.parametrize("order", [("METRIC", "FINDING"), ("FINDING", "METRIC")])
def test_a_consumer_of_a_reused_node_sees_the_evidence_of_every_output_type(atom, target, build_plan, order):
    index, metric, finding = _index(order)
    seen: list = []

    def consume(args, ctx):  # noqa: ARG001
        seen.extend(ctx.dependencies())
        return ()

    registry = AtomRegistry([
        atom("read.value", ("METRIC",)),
        atom("out.consumer", ("CONSUMER",), requires=("METRIC",), executor=consume),
    ])
    result, node_args = build_plan(registry, target("CONSUMER"), available=index)
    assert result.feasibility.feasible, result.feasibility.reason

    execution = GraphExecutor(registry=registry).run(
        result.graph, node_args, run_id="run-1", available=index, inputs=RunInputs(),
    )

    assert execution.succeeded
    assert {item.evidence_id for item in seen} == {metric.evidence_id, finding.evidence_id}, (
        "потребитель переиспользованного узла обязан видеть и метрику, и находку"
    )
