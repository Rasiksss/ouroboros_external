"""The ``ToolEvents`` adapter: one case per row of the published/unpublished table.

Plus the boundary the change exists for: outside the adapter itself, nothing in the tool or
policy layers may name the transport.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

import pytest

from new_core.tools.interfaces import NullToolEvents, ToolEvents
from ouroboros.agent_events.interfaces import LlmUsageReported, SkillLifecycle, SubtaskRequested
from ouroboros.tools.events import PublishedToolEvents
from tests.helpers.agent_events import RecordingAgentEvents
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.sql_faults import fail_sql

#: The layers a tool author writes in. ``ouroboros/agent/`` is deliberately absent: the agent
#: holds the publisher and hands it to the adapter, which is the whole point.
_TOOL_LAYER_ROOTS = (
    pathlib.Path("ouroboros/tools"),
    pathlib.Path("ouroboros/policy"),
    pathlib.Path("new_core/tools"),
)

#: The one module allowed to know what carries an event.
_ADAPTER = pathlib.Path("ouroboros/tools/events.py")


class _BrokenPublisher(RecordingAgentEvents):
    """A publisher whose publish always fails, to exercise the fall-through."""

    def publish(self, event: Any) -> None:
        raise RuntimeError("the transport is closed")


@pytest.fixture
def sink() -> RecordingAgentEvents:
    return RecordingAgentEvents()


def test_audit_records_the_supplied_scope_and_stream_in_the_injected_repository(tmp_path):
    repository = sql_audit_event_repository()
    events = PublishedToolEvents(None, audit_event_repository=repository)

    events.audit("tools", "artifact_materialization_error", {"task_id": "task-7"}, scope=TEST_SCOPE)

    (written,) = repository.list_audit_events()
    assert (written.owner_id, written.agent_id, written.log_stream, written.event_type) == (
        TEST_SCOPE.owner_id,
        TEST_SCOPE.agent_id,
        "tools",
        "artifact_materialization_error",
    )
    assert written.metadata["task_id"] == "task-7"


def test_audit_failure_does_not_fail_a_tool_event(tmp_path):
    repository = sql_audit_event_repository()
    with fail_sql("audit_events", OSError("audit unavailable")):
        events = PublishedToolEvents(None, audit_event_repository=repository)

        events.audit("tools", "artifact_materialization_error", {}, scope=TEST_SCOPE)

    assert repository.list_audit_events() == []


# --- usage -------------------------------------------------------------------------------


def test_usage_is_reported_with_the_bound_task_id(sink: RecordingAgentEvents, tmp_path: pathlib.Path) -> None:
    events = PublishedToolEvents(
        sink,
        task_id="t-1",
        task_type="task",
        scope=TEST_SCOPE,
        audit_event_repository=sql_audit_event_repository(),
    )

    events.usage("gpt-x", {"prompt_tokens": 10, "completion_tokens": 4}, category="safety", source="safety_check")

    (evt,) = sink.published
    assert isinstance(evt, LlmUsageReported)
    assert evt.task_id == "t-1"
    assert evt.model == "gpt-x"
    assert evt.category == "safety"
    assert evt.source == "safety_check"
    assert evt.scope == TEST_SCOPE
    # The fields a hand-rolled producer used to omit.
    assert evt.provider and evt.api_key_type and evt.model_category and evt.ts


def test_usage_without_a_publisher_is_counted_locally(monkeypatch, tmp_path: pathlib.Path) -> None:
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr("supervisor.state.update_usage_from_llm", lambda usage, **_kwargs: recorded.append(usage))

    PublishedToolEvents(None, task_id="t-1", audit_event_repository=sql_audit_event_repository()).usage(
        "gpt-x", {"prompt_tokens": 3}
    )

    assert recorded == [{"prompt_tokens": 3}]


def test_task_usage_accumulates_a_tools_out_of_loop_model_call(
    sink: RecordingAgentEvents, tmp_path: pathlib.Path
) -> None:
    # vlm_query calls a model outside the loop; its tokens are the task's spend, so they
    # belong in the same total the loop accumulates and `emit_task_results` publishes.
    totals: dict[str, Any] = {}
    events = PublishedToolEvents(
        sink,
        task_id="t-1",
        task_usage=totals,
        scope=TEST_SCOPE,
        audit_event_repository=sql_audit_event_repository(),
    )

    events.usage("vlm/model", {"prompt_tokens": 8000, "completion_tokens": 500}, source="vlm_query")

    assert totals["prompt_tokens"] == 8000
    assert totals["completion_tokens"] == 500
    # Still reported outward: the two accountings are independent.
    assert isinstance(sink.published[0], LlmUsageReported)


def test_task_usage_excludes_safety_supervisor_calls(tmp_path: pathlib.Path) -> None:
    # Safety checks are infrastructure billed to the process, not work the task asked for;
    # counting them would inflate the usage the task reports.
    totals: dict[str, Any] = {}
    events = PublishedToolEvents(
        None, task_id="t-1", task_usage=totals, audit_event_repository=sql_audit_event_repository()
    )

    events.usage("gpt-x", {"prompt_tokens": 40}, category="safety", source="safety_check")

    assert totals == {}


def test_task_usage_is_optional_for_a_sink_built_outside_a_task(
    sink: RecordingAgentEvents, tmp_path: pathlib.Path
) -> None:
    # `ouroboros/policy/safety.py` builds a context-free sink; it has no task total.
    PublishedToolEvents(sink, audit_event_repository=sql_audit_event_repository()).usage(
        "gpt-x", {"prompt_tokens": 1}
    )  # must not raise

    assert isinstance(sink.published[0], LlmUsageReported)


# --- lifecycle ---------------------------------------------------------------------------


def test_lifecycle_is_reported_stamped(sink: RecordingAgentEvents, tmp_path: pathlib.Path) -> None:
    events = PublishedToolEvents(
        sink, task_id="t-1", scope=TEST_SCOPE, audit_event_repository=sql_audit_event_repository()
    )

    events.lifecycle("skill_exec_finished", {"skill": "alpha", "script": "run.py", "exit_code": 0})

    (evt,) = sink.published
    assert isinstance(evt, SkillLifecycle)
    assert evt.event_type == "skill_exec_finished"
    assert evt.task_id == "t-1"
    assert evt.payload["skill"] == "alpha"
    assert evt.payload["script"] == "run.py"
    assert evt.payload["exit_code"] == 0
    assert evt.ts


def test_lifecycle_without_a_publisher_records_and_republishes(monkeypatch, tmp_path: pathlib.Path) -> None:
    recorded: list[tuple[str, dict[str, Any]]] = []
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "ouroboros.tools.events.record_event",
        lambda stream, event_type, payload, **kw: recorded.append((event_type, payload)),
    )
    monkeypatch.setattr(
        "ouroboros.observability.event_bus.publish_event",
        lambda topic, data: published.append((topic, data)),
    )

    PublishedToolEvents(None, task_id="t-1", audit_event_repository=sql_audit_event_repository()).lifecycle(
        "skill_exec_failed", {"skill": "alpha"}
    )

    assert [name for name, _ in recorded] == ["skill_exec_failed"]
    assert [topic for topic, _ in published] == ["skill.lifecycle"]


def test_lifecycle_falls_back_when_the_transport_rejects_it(monkeypatch, tmp_path: pathlib.Path) -> None:
    recorded: list[str] = []
    monkeypatch.setattr(
        "ouroboros.tools.events.record_event",
        lambda stream, event_type, payload, **kw: recorded.append(event_type),
    )

    PublishedToolEvents(
        _BrokenPublisher(), scope=TEST_SCOPE, audit_event_repository=sql_audit_event_repository()
    ).lifecycle("skill_exec_finished", {"skill": "alpha"})

    assert recorded == ["skill_exec_finished"]


def test_unknown_lifecycle_type_is_recorded_but_not_republished(monkeypatch, tmp_path: pathlib.Path) -> None:
    published: list[str] = []
    monkeypatch.setattr(
        "ouroboros.tools.events.record_event",
        lambda stream, event_type, payload, **kw: None,
    )
    monkeypatch.setattr(
        "ouroboros.observability.event_bus.publish_event",
        lambda topic, data: published.append(topic),
    )

    PublishedToolEvents(None, audit_event_repository=sql_audit_event_repository()).lifecycle("something_new", {})

    assert published == []


# --- subtask requests --------------------------------------------------------------------


def _subtask_request() -> SubtaskRequested:
    return SubtaskRequested(scope=TEST_SCOPE, task_id="child-1", description="Do the thing", depth=1)


def test_a_subtask_request_is_handed_to_the_supervisor_at_once(sink: RecordingAgentEvents) -> None:
    events = PublishedToolEvents(sink, task_id="t-1", scope=TEST_SCOPE, audit_event_repository=None)
    request = _subtask_request()

    assert events.request_subtask(request) is True

    assert sink.published == [request]


def test_a_subtask_request_without_a_publisher_stays_with_the_caller() -> None:
    events = PublishedToolEvents(None, task_id="t-1", audit_event_repository=None)

    assert events.request_subtask(_subtask_request()) is False


def test_a_subtask_request_the_transport_refuses_stays_with_the_caller() -> None:
    events = PublishedToolEvents(_BrokenPublisher(), task_id="t-1", scope=TEST_SCOPE, audit_event_repository=None)

    assert events.request_subtask(_subtask_request()) is False


# --- the no-op default -------------------------------------------------------------------


def test_no_tool_layer_module_names_the_transport() -> None:
    """The point of the change: a tool reports events, it does not reach a queue.

    Checked on the AST rather than the raw text so that prose about ``put_nowait`` in an
    interface docstring does not count as reaching for one.
    """
    offenders: list[str] = []
    for root in _TOOL_LAYER_ROOTS:
        for path in root.rglob("*.py"):
            if path == _ADAPTER:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in {"event_queue", "put_nowait"}:
                    offenders.append(f"{path}:{node.lineno} .{node.attr}")
                elif isinstance(node, ast.Name) and node.id == "event_queue":
                    offenders.append(f"{path}:{node.lineno} event_queue")
                elif isinstance(node, ast.Import):
                    offenders += [f"{path}:{node.lineno} import {a.name}" for a in node.names if a.name == "queue"]
                elif isinstance(node, ast.ImportFrom) and node.module == "queue":
                    offenders.append(f"{path}:{node.lineno} from queue import ...")

    assert offenders == []


def test_null_events_emits_nothing_and_satisfies_the_interface() -> None:
    events = NullToolEvents()
    assert isinstance(events, ToolEvents)
    assert events.usage("m", {"prompt_tokens": 1}) is None
    assert events.lifecycle("x", {}) is None
    assert events.request_subtask(SubtaskRequested(scope=TEST_SCOPE)) is False
