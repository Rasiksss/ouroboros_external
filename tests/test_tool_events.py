"""The ``ToolEvents`` adapter: one case per row of the queue-present/queue-absent table.

Plus the boundary the change exists for: outside the adapter itself, nothing in the tool or
policy layers may name the transport.
"""

from __future__ import annotations

import ast
import pathlib
import queue as queue_mod
from typing import Any

import pytest

from new_core.tools.interfaces import NullToolEvents, ToolEvents
from ouroboros.tools.events import QueueToolEvents

#: The layers a tool author writes in. ``ouroboros/agent/`` is deliberately absent: the agent
#: owns the queue and hands it to the adapter, which is the whole point.
_TOOL_LAYER_ROOTS = (
    pathlib.Path("ouroboros/tools"),
    pathlib.Path("ouroboros/policy"),
    pathlib.Path("new_core/tools"),
)

#: The one module allowed to know what carries an event.
_ADAPTER = pathlib.Path("ouroboros/tools/events.py")


class _BrokenQueue:
    """A queue whose put always fails, to exercise the fall-through."""

    def put_nowait(self, item: Any) -> None:
        raise RuntimeError("queue is closed")


@pytest.fixture
def event_q() -> queue_mod.Queue:
    return queue_mod.Queue()


def _drain(q: queue_mod.Queue) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    while True:
        try:
            events.append(q.get_nowait())
        except queue_mod.Empty:
            return events


# --- usage -------------------------------------------------------------------------------


def test_usage_reaches_the_queue_with_the_bound_task_id(event_q: queue_mod.Queue, tmp_path: pathlib.Path) -> None:
    events = QueueToolEvents(event_q, tmp_path, task_id="t-1", task_type="task")

    events.usage("gpt-x", {"prompt_tokens": 10, "completion_tokens": 4}, category="safety", source="safety_check")

    (evt,) = _drain(event_q)
    assert evt["type"] == "llm_usage"
    assert evt["task_id"] == "t-1"
    assert evt["model"] == "gpt-x"
    assert evt["category"] == "safety"
    assert evt["source"] == "safety_check"
    # The fields a hand-rolled producer used to omit.
    assert {"provider", "api_key_type", "model_category", "ts"} <= set(evt)


def test_usage_without_a_queue_is_counted_locally(monkeypatch, tmp_path: pathlib.Path) -> None:
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr("supervisor.state.update_usage_from_llm", lambda usage: recorded.append(usage))

    QueueToolEvents(None, tmp_path, task_id="t-1").usage("gpt-x", {"prompt_tokens": 3})

    assert recorded == [{"prompt_tokens": 3}]


def test_task_usage_accumulates_a_tools_out_of_loop_model_call(
    event_q: queue_mod.Queue, tmp_path: pathlib.Path
) -> None:
    # vlm_query calls a model outside the loop; its tokens are the task's spend, so they
    # belong in the same total the loop accumulates and `emit_task_results` publishes.
    totals: dict[str, Any] = {}
    events = QueueToolEvents(event_q, tmp_path, task_id="t-1", task_usage=totals)

    events.usage("vlm/model", {"prompt_tokens": 8000, "completion_tokens": 500}, source="vlm_query")

    assert totals["prompt_tokens"] == 8000
    assert totals["completion_tokens"] == 500
    # Still reported to the supervisor: the two accountings are independent.
    assert _drain(event_q)[0]["type"] == "llm_usage"


def test_task_usage_excludes_safety_supervisor_calls(tmp_path: pathlib.Path) -> None:
    # Safety checks are infrastructure billed to the process, not work the task asked for;
    # counting them would inflate the usage the task reports.
    totals: dict[str, Any] = {}
    events = QueueToolEvents(None, tmp_path, task_id="t-1", task_usage=totals)

    events.usage("gpt-x", {"prompt_tokens": 40}, category="safety", source="safety_check")

    assert totals == {}


def test_task_usage_is_optional_for_a_sink_built_outside_a_task(
    event_q: queue_mod.Queue, tmp_path: pathlib.Path
) -> None:
    # `ouroboros/policy/safety.py` builds a context-free sink; it has no task total.
    QueueToolEvents(event_q, tmp_path).usage("gpt-x", {"prompt_tokens": 1})  # must not raise

    assert _drain(event_q)[0]["type"] == "llm_usage"


# --- lifecycle ---------------------------------------------------------------------------


def test_lifecycle_reaches_the_queue_stamped(event_q: queue_mod.Queue, tmp_path: pathlib.Path) -> None:
    events = QueueToolEvents(event_q, tmp_path, task_id="t-1")

    events.lifecycle("skill_exec_finished", {"skill": "alpha", "script": "run.py", "exit_code": 0})

    (evt,) = _drain(event_q)
    assert evt["type"] == "skill_exec_finished"
    assert evt["task_id"] == "t-1"
    assert evt["skill"] == "alpha"
    assert evt["script"] == "run.py"
    assert evt["exit_code"] == 0
    assert evt["ts"]


def test_lifecycle_without_a_queue_records_and_republishes(monkeypatch, tmp_path: pathlib.Path) -> None:
    recorded: list[tuple[str, dict[str, Any]]] = []
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "ouroboros.tools.events.record_event",
        lambda root, stream, event_type, payload, **kw: recorded.append((event_type, payload)),
    )
    monkeypatch.setattr(
        "ouroboros.observability.event_bus.publish_event",
        lambda topic, data: published.append((topic, data)),
    )

    QueueToolEvents(None, tmp_path, task_id="t-1").lifecycle("skill_exec_failed", {"skill": "alpha"})

    assert [name for name, _ in recorded] == ["skill_exec_failed"]
    assert [topic for topic, _ in published] == ["skill.lifecycle"]


def test_lifecycle_falls_back_when_the_queue_rejects_it(monkeypatch, tmp_path: pathlib.Path) -> None:
    recorded: list[str] = []
    monkeypatch.setattr(
        "ouroboros.tools.events.record_event",
        lambda root, stream, event_type, payload, **kw: recorded.append(event_type),
    )

    QueueToolEvents(_BrokenQueue(), tmp_path).lifecycle("skill_exec_finished", {"skill": "alpha"})

    assert recorded == ["skill_exec_finished"]


def test_unknown_lifecycle_type_is_recorded_but_not_republished(monkeypatch, tmp_path: pathlib.Path) -> None:
    published: list[str] = []
    monkeypatch.setattr(
        "ouroboros.tools.events.record_event",
        lambda root, stream, event_type, payload, **kw: None,
    )
    monkeypatch.setattr(
        "ouroboros.observability.event_bus.publish_event",
        lambda topic, data: published.append(topic),
    )

    QueueToolEvents(None, tmp_path).lifecycle("something_new", {})

    assert published == []


# --- the no-op default -------------------------------------------------------------------


def test_no_tool_layer_module_names_the_transport() -> None:
    """The point of the change: a tool emits events, it does not reach a queue.

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
