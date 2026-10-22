"""Discover ready artifacts without repeating their execution (ADR-0062)."""

import json

import pytest

from loop_support import open_cycle
from test_ephemeral_in_graph import CASE, OWNER

from finengine.assistant import ouro_view
from finengine.contracts import DynamicArtifact, NarrativeOutput, NodeKey


@pytest.mark.parametrize("args", [{"view": {}}, {"view": []}, {"artifact_index": {"limit": True}}, {"artifact_index": {"offset": -1}}, {"artifact_index": {}, "artifact_ids": ["x"]}])
async def test_bad_index_or_view_arguments_are_refused_without_crashing(base, args):
    cycle = await open_cycle(base)
    reply = await cycle.call("aifa_state", args)
    assert reply.startswith("ОТКАЗ")
    await cycle.close()


def put_artifact(base, name, output_type):
    artifact = DynamicArtifact(output_type=output_type, payload={"name": name}, origin=["generated"], producer="eph.test@1", input_evidence_ids=[])
    base.ensure_case(owner_id=OWNER, case_id=CASE)
    base.store.put_artifact(OWNER, CASE, node_key=NodeKey(atom_name="eph.test", atom_version="1", args_fingerprint=name), output_type=output_type, payloads=(artifact,), concept=None, entity_ref=None, period_ref=None, basis=None, document_version=None, policy_version="test")
    return artifact.evidence_id


async def test_filtered_index_finds_a_ready_result_beyond_the_first_page(base):
    for index in range(30):
        put_artifact(base, f"noise{index}", "dyn:noise")
    wanted = put_artifact(base, "ready", "dyn:ready")
    cycle = await open_cycle(base)
    reply = await cycle.call("aifa_state", {"artifact_index": {"output_type": "dyn:ready", "limit": 5}})
    body = json.loads(reply.split("\n", 1)[1])
    assert [item["id"] for item in body["artifacts"]] == [wanted]
    assert body["next_offset"] is None
    assert base.store.list_artifacts(OWNER, CASE)
    await cycle.close()


async def test_artifact_index_pages_name_the_next_offset(base):
    ids = {put_artifact(base, str(index), "dyn:row") for index in range(8)}
    cycle = await open_cycle(base)
    first = json.loads((await cycle.call("aifa_state", {"artifact_index": {"limit": 5}})).split("\n", 1)[1])
    second = json.loads((await cycle.call("aifa_state", {"artifact_index": {"limit": 5, "offset": first["next_offset"]}})).split("\n", 1)[1])
    assert first["next_offset"] == 5
    assert second["next_offset"] is None
    assert {item["id"] for page in [first, second] for item in page["artifacts"]} == ids
    await cycle.close()


async def test_narrative_outputs_can_be_discovered_and_read_by_their_output_id(base):
    narrative = NarrativeOutput(run_id="narrative-run", text="Проверенный вывод")
    base.ensure_case(owner_id=OWNER, case_id=CASE)
    base.store.put_artifact(OWNER, CASE, node_key=NodeKey(atom_name="out.narrative", atom_version="1", args_fingerprint="ready"), output_type="NarrativeOutput", payloads=(narrative,), concept=None, entity_ref=None, period_ref=None, basis=None, document_version=None, policy_version="test")
    cycle = await open_cycle(base)
    index = json.loads((await cycle.call("aifa_state", {"artifact_index": {"output_type": "NarrativeOutput"}})).split("\n", 1)[1])
    assert [item["id"] for item in index["artifacts"]] == [narrative.output_id]
    reply = await cycle.call("aifa_state", {"artifact_ids": [narrative.output_id]})
    assert narrative.text in reply
    await cycle.close()


def test_compact_values_keep_code_input_shape_without_provenance():
    payload = {"output_type": "CalculatedMetric", "concept": "MARGIN", "period_ref": "2025", "status": "supported", "derived_fact": {"evidence_id": "m1", "value": 25, "unit": "%", "expression": "INCOME / SALES * 100", "inputs": ["i1", "s1"], "document_version": "doc", "produced_by_atom": "calc.formula"}, "unification": {"long": "provenance"}}
    result = ouro_view.artifacts([("m1", json.dumps(payload))], [], limit=12000, view="compact")
    assert '"expression"' in result and '"inputs"' in result and '"derived_fact"' in result
    assert "provenance" not in result
    assert "produced_by_atom" not in result
