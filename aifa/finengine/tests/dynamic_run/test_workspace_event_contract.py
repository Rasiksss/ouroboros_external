"""Контракт событий рабочей области: настоящий producer backend → fixture frontend."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import update

from finengine.contracts import CaseIdentity, DocumentText, NodeKey, NodeRun, PlanGraph
from finengine.skills.result_events import (
    author_attempt_event,
    dynamic_move_event,
    node_run_event,
    plan_graph_event,
)
from finengine.skills import run_orchestrator
from finengine.store.cases import (
    CASE_ARTIFACT,
    SqlCaseStore,
    evidence_id_of,
    result_id_of,
)

from dynamic_support import assert_event_fixture


RECORDED_DEV_STREAMS = (
    "dev-case-reuse.json",
    "dev-external-source-give-up.json",
    "dev-restart-stopped.json",
    "dev-compound-one-cycle.json",
    "dev-no-data-no-cycle.json",
    "dev-cycle-file-delivery.json",
)


def _recorded_dev_stream(name: str) -> list[dict]:
    path = Path(__file__).parent / "fixtures" / "sse" / name
    return json.loads(path.read_text(encoding="utf-8"))


def test_recorded_dev_streams_are_shared_sanitized_fixtures() -> None:
    for name in RECORDED_DEV_STREAMS:
        rendered = (Path(__file__).parent / "fixtures" / "sse" / name).read_text(encoding="utf-8")
        assert _recorded_dev_stream(name)
        assert "85.198.84.50" not in rendered
        assert "finagent.homyakagent.ru" not in rendered
        assert "password" not in rendered.lower()


def test_recorded_empty_node_has_no_evidence_while_reused_target_does() -> None:
    interrupted = _recorded_dev_stream("dev-restart-stopped.json")
    reused = _recorded_dev_stream("dev-case-reuse.json")
    empty = next(
        event["node_run"] for event in interrupted
        if event.get("type") == "node_run"
        and event["node_run"]["node_key"]["atom_name"] == "read.value"
        and event["node_run"]["status"] == "succeeded"
    )
    target = next(
        event["node_run"] for event in reused
        if event.get("type") == "node_run" and event["node_run"]["status"] == "succeeded"
    )

    assert empty["produced_evidence_ids"] == []
    assert target["produced_evidence_ids"]


def _graph(graph_hash: str, atom_name: str, node_count: int = 1) -> PlanGraph:
    keys = [{
        "atom_name": atom_name if index == node_count - 1 else f"{atom_name}.stage{index}",
        "atom_version": "1.0.0",
        "args_fingerprint": "same-key" if index == node_count - 1 else f"stage-{index}",
    } for index in range(node_count)]
    return PlanGraph.model_validate({
        "plan_id": f"plan-{graph_hash}",
        "graph_hash": graph_hash,
        "planner_version": "dynamic-1.0.0",
        "root": keys[-1],
        "nodes": [{
            "node_key": key,
            "produces": ["ExtractedMetric:VALUE"],
            "requires": [] if index == 0 else [keys[index - 1]],
            "sandbox_profile": "document_read",
            "scenario_refs": [],
        } for index, key in enumerate(keys)],
        "scenarios": [],
    })


def _run(graph: PlanGraph, evidence_ids: list[str]) -> NodeRun:
    return NodeRun(
        node_run_id=f"run-{graph.graph_hash}",
        node_key=graph.root,
        run_id="workspace-contract",
        status="succeeded",
        sandbox_profile="document_read",
        attempt=1,
        produced_evidence_ids=evidence_ids,
        model_version=None,
        prompt_version=None,
    )


def _workspace_events() -> list[dict]:
    first = _graph("graph-a", "read.value", node_count=3)
    second = _graph("graph-b", "read.value")
    third = _graph("graph-c", "read.value")
    fourth = _graph("graph-d", "read.value")
    return [
        dynamic_move_event(move=1, status="running", action="execute", steps=({"atom": "read.value"},)),
        plan_graph_event(first),
        node_run_event(_run(first, []), plan_graph_hash=first.graph_hash),
        dynamic_move_event(move=1, status="done", action="execute", steps=({"atom": "read.value"},)),
        dynamic_move_event(move=2, status="running", action="execute", scenarios=("A", "B")),
        plan_graph_event(second),
        node_run_event(_run(second, []), plan_graph_hash=second.graph_hash),
        author_attempt_event(
            move=2, atom="eph.value", attempt=1, status="ok", code="def run(inputs): return inputs",
        ),
        plan_graph_event(third),
        node_run_event(_run(third, ["evidence-from-case"]), plan_graph_hash=third.graph_hash),
        dynamic_move_event(move=2, status="done", action="execute", scenarios=("A", "B")),
        dynamic_move_event(move=3, status="running", action="execute", steps=({"atom": "read.value"},)),
        plan_graph_event(fourth),
        node_run_event(_run(fourth, ["evidence-new"]), plan_graph_hash=fourth.graph_hash),
        dynamic_move_event(move=3, status="done", action="execute", steps=({"atom": "read.value"},)),
        dynamic_move_event(move=4, status="running", action="execute"),
        dynamic_move_event(move=4, status="done", action="execute", outcome="результатов нет"),
    ]


def test_workspace_events_keep_the_move_boundary_and_all_backend_fields() -> None:
    events = _workspace_events()
    assert [event["type"] for event in events] == [
        "dynamic_move", "plan_graph", "node_run", "dynamic_move", "dynamic_move",
        "plan_graph", "node_run", "author_attempt", "plan_graph", "node_run",
        "dynamic_move", "dynamic_move", "plan_graph", "node_run", "dynamic_move",
        "dynamic_move", "dynamic_move",
    ]
    assert [event["graph_hash"] for event in events if event["type"] == "plan_graph"] == [
        "graph-a", "graph-b", "graph-c", "graph-d",
    ]
    assert events[2]["node_run"]["produced_evidence_ids"] == []
    assert events[9]["node_run"]["produced_evidence_ids"] == ["evidence-from-case"]
    assert_event_fixture("workspace-cycle.json", events)


def _succeeded_fixture_runs() -> list[dict]:
    path = Path(__file__).parent / "fixtures" / "sse" / "empty-read-then-give-up.json"
    events = json.loads(path.read_text(encoding="utf-8"))
    return [
        event["node_run"] for event in events
        if event["type"] == "node_run" and event["node_run"]["status"] == "succeeded"
    ]


def test_every_succeeded_node_run_publishes_real_evidence_ids_or_an_intentional_empty_result() -> None:
    succeeded = _succeeded_fixture_runs()
    assert {run["node_key"]["atom_name"] for run in succeeded} >= {
        "ingest.document", "identify.classify", "locate", "read.value", "norm.unit", "norm.time",
        "verify.bounds",
    }
    assert all(
        "(" not in evidence_id
        for run in succeeded
        for evidence_id in run["produced_evidence_ids"]
    )


def test_reused_nodes_publish_the_existing_case_evidence_ids() -> None:
    succeeded = _succeeded_fixture_runs()
    reused = [run for run in succeeded if run["node_key"]["atom_name"] in {"ingest.document", "identify.classify"}]
    assert all(run["produced_evidence_ids"] for run in reused)


def test_legacy_case_row_recovers_a_resolvable_result_id_without_a_migration(tmp_path: Path) -> None:
    store = SqlCaseStore(f"sqlite:///{tmp_path / 'legacy-result-id.db'}")
    store.ensure_case(CaseIdentity(case_id="case-1", owner_id="owner-1"))
    key = NodeKey(atom_name="ingest.document", atom_version="1.0.0", args_fingerprint="entity=ent")
    document = DocumentText(
        document_id="ent", document_version="sha256:v1", page=1, text="report",
    )
    store.put_artifact(
        "owner-1", "case-1", node_key=key, output_type="DocumentText", payloads=(document,),
        concept=None, entity_ref="ent", period_ref=None, basis=None,
        document_version="sha256:v1", policy_version="policy-1",
    )
    with store._engine.begin() as connection:  # noqa: SLF001 — эмуляция строки до появления result_id
        connection.execute(update(CASE_ARTIFACT).values(evidence_ids=[]))

    result_id = result_id_of(document)
    assert result_id is not None and result_id.startswith("content:")
    assert evidence_id_of(document) is None
    assert store.list_artifacts("owner-1", "case-1")[0]["evidence_ids"] == [result_id]
    assert store.find_evidence("owner-1", "case-1", result_id, valid_only=True) == [document]

_T0 = datetime(2026, 10, 1, 10, 0, 0, tzinfo=timezone.utc)


def _timed_run(graph: PlanGraph, index: int, status: str, evidence_ids: list[str], error: str | None = None) -> NodeRun:
    node = graph.nodes[index]
    started = _T0 + timedelta(seconds=index * 2)
    finished = None if status == "running" else started + timedelta(milliseconds=1500)
    return NodeRun(
        node_run_id=f"wire-{graph.graph_hash}-{index}", node_key=node.node_key, run_id="wire-run",
        status=status, sandbox_profile="document_read", attempt=1, produced_evidence_ids=evidence_ids,
        error=error, started_at=started, finished_at=finished,
    )


def _wire_source_events() -> list[dict]:
    """Журнал прогона как его пишет backend: у NodeRun есть настоящие времена."""
    good, bad, open_graph = _graph("wire-a", "read.value", node_count=3), _graph("wire-b", "read.value", node_count=2), _graph("wire-c", "read.value")
    runs = [
        dynamic_move_event(move=1, status="running", action="execute"),
        plan_graph_event(good),
        *(node_run_event(_timed_run(good, i, "succeeded", [f"ev-{i}"]), plan_graph_hash="wire-a") for i in range(3)),
        dynamic_move_event(move=1, status="done", action="execute"),
        dynamic_move_event(move=2, status="running", action="execute"),
        plan_graph_event(bad),
        node_run_event(_timed_run(bad, 0, "failed", [], error="нет источника"), plan_graph_hash="wire-b"),
        node_run_event(_timed_run(bad, 1, "blocked", []), plan_graph_hash="wire-b"),
        dynamic_move_event(move=2, status="done", action="execute"),
        dynamic_move_event(move=3, status="running", action="execute"),
        plan_graph_event(open_graph),
        node_run_event(_timed_run(open_graph, 0, "running", []), plan_graph_hash="wire-c"),
        {
            "type": "typed_error", "partial_results": [],
            "error": {"code": "dynamic_run_failed", "message": "сбой цикла", "retryable": False,
                      "node_key": None, "details": {}},
        },
        {"type": "done"},
    ]
    return runs


class _JournalStream:
    def __init__(self, events: list[dict]) -> None:
        self.events = events

    def __call__(self, *args, **kwargs):  # noqa: ARG002
        return self._iterate()

    async def _iterate(self):
        for event in self.events:
            yield event


async def test_wire_journal_through_subscribe_versioned_matches_the_frontend_fixture(monkeypatch) -> None:
    """Стык «журнал → конверт A11 → unwrapRunEvents»: frontend читает ровно эти кадры."""
    monkeypatch.setattr(run_orchestrator, "subscribe", _JournalStream(_wire_source_events()))

    envelopes = [event async for event in run_orchestrator.subscribe_versioned("wire-run", kind="chat")]
    frames = [json.loads(envelope.model_dump_json(exclude={"event_id", "occurred_at"})) for envelope in envelopes]

    assert [frame["event_type"] for frame in frames].count("node_run") == 6
    assert frames[-1] == {
        "event_type": "completed", "run_id": "wire-run", "schema_version": "1.0",
        "sequence": len(frames), "status": "failed",
    }
    assert_event_fixture("workspace-wire.json", frames)


def test_recorded_compound_goal_is_one_cycle_with_one_answer_and_no_internal_error() -> None:
    stream = _recorded_dev_stream("dev-compound-one-cycle.json")
    moves = [event for event in stream if event.get("type") == "dynamic_move" and event["status"] == "done"]
    rendered = json.dumps(stream, ensure_ascii=False)

    assert [move["action"] for move in moves][-1] == "finalize"
    assert any(move["scenarios"] for move in moves), "извлечение прошло стандартным сценарием"
    assert [event["type"] for event in stream if event["type"] == "text_delta"] == ["text_delta"]
    assert not [event for event in stream if event["type"] == "typed_error"]
    assert "compound_goal_not_composable" not in rendered
    assert "graph_execution_failed" not in rendered


def test_recorded_no_data_result_is_a_refusal_without_a_cycle() -> None:
    stream = _recorded_dev_stream("dev-no-data-no-cycle.json")
    errors = [event for event in stream if event["type"] == "typed_error"]

    assert [event["error"]["code"] for event in errors] == ["metric_not_found"]
    assert errors[0]["error"]["retryable"] is False
    assert not [event for event in stream if event["type"] == "dynamic_move"]
    assert all(
        event["node_run"]["status"] != "failed" for event in stream if event["type"] == "node_run"
    ), "«данных нет» — исполнение без ошибок, а не упавший узел"


def test_recorded_file_delivery_publishes_the_file_card_and_a_download_event_for_the_same_file() -> None:
    stream = _recorded_dev_stream("dev-cycle-file-delivery.json")
    cards = [event["result"] for event in stream if event["type"] == "typed_result"
             and event["result"]["output_type"] == "FileArtifact"]
    downloads = [event for event in stream if event["type"] == "file"]

    assert len(cards) == 1 and len(downloads) == 1
    card, download = cards[0], downloads[0]
    assert download["filename"] == card["filename"] and download["kind"] == card["file_kind"] == "xlsx"
    assert card["object_store_location"].rsplit("/", 1)[1] == f"{download['file_id']}.xlsx"
    assert card["source_result"]["result_type"] == "DynamicArtifact" and "формул" in card["summary"]
    assert not [event for event in stream if event["type"] == "typed_error"]
