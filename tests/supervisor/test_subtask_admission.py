from types import SimpleNamespace
from unittest.mock import patch

from ouroboros.agent_events.interfaces import SubtaskRequested
from ouroboros.storage.runtime_models import TaskRequest
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor import events
from tests.helpers.factories import make_mock_llm
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, scope_with


def test_planner_request_does_not_compare_with_its_ancestors(tmp_path):
    """An active ancestor with the same user request must not reject its own planner."""
    store = runtime_persistence.initialize().tasks_repo
    for task_id, parent in (("root", ""), ("parent", "root")):
        store.create_task(
            TaskRequest(id=task_id, parent_task_id=parent, text="Write the report", scope=TEST_SCOPE.to_dict()),
            status="running",
        )
    queued = []
    ctx = SimpleNamespace(DRIVE_ROOT=tmp_path, enqueue_task=queued.append, send_with_budget=lambda *_a, **_k: None)
    client = make_mock_llm({"content": "parent"}, usage={})
    with patch("ouroboros.llm.LLMClient", return_value=client):
        events._handle_schedule_task(
            SubtaskRequested(
                scope=TEST_SCOPE,
                task_id="planner",
                parent_task_id="parent",
                root_task_id="root",
                description="Write the report",
                source="browser",
            ),
            ctx,
        )
    assert len(queued) == 1
    client.chat.assert_not_called()


@parametrize_from_dict({"acknowledged": {"suppress_ack": False}, "quiet_admission": {"suppress_ack": True}})
def test_rejected_sibling_publishes_a_correlated_failure(tmp_path, suppress_ack):
    """Admission notices can be quiet; a waiting parent still needs its child's failure."""
    store = runtime_persistence.initialize().tasks_repo
    store.create_task(TaskRequest(id="root", text="Write the report", scope=TEST_SCOPE.to_dict()), status="running")
    store.create_task(
        TaskRequest(id="sibling", parent_task_id="root", text="Plan the report", scope=TEST_SCOPE.to_dict()),
        status="running",
    )
    sent = []
    queued = []
    ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        enqueue_task=queued.append,
        send_with_budget=lambda text, **kwargs: sent.append((text, kwargs)),
    )
    client = make_mock_llm({"content": "sibling"}, usage={})
    transport = {"source_message_id": "run-id"}
    with patch("ouroboros.llm.LLMClient", return_value=client):
        events._handle_schedule_task(
            SubtaskRequested(
                scope=TEST_SCOPE,
                task_id="planner",
                parent_task_id="root",
                root_task_id="root",
                description="Plan the report",
                source="browser",
                transport=transport,
                suppress_ack=suppress_ack,
            ),
            ctx,
        )
    assert queued == []
    assert store.get_task("planner").status == "rejected_duplicate"
    assert len(sent) == 1
    text, fields = sent[0]
    assert text.startswith("⚠️ Error during processing")
    assert fields["task_id"] == "planner"
    assert fields["scope"] == TEST_SCOPE
    assert fields["transport"] == transport
    assert fields["source"] == "browser"
    prompt = client.chat.call_args.kwargs["messages"][-1]["content"]
    assert "Task ID: root\n" not in prompt
    assert "Task ID: sibling\n" in prompt


@parametrize_from_dict(
    {
        "foreign_owner": {"other": scope_with(TEST_SCOPE, owner_id="other")},
        "foreign_agent": {"other": scope_with(TEST_SCOPE, agent_id="other")},
    }
)
def test_ancestor_traversal_stops_at_another_owner_or_agent(tmp_path, other):
    """A foreign parent link cannot suppress this owner's unrelated active task."""
    store = runtime_persistence.initialize().tasks_repo
    store.create_task(TaskRequest(id="mine", text="Plan report", scope=TEST_SCOPE.to_dict()), status="running")
    store.create_task(
        TaskRequest(id="foreign", parent_task_id="mine", text="Private report", scope=other.to_dict()),
        status="running",
    )
    client = make_mock_llm({"content": "mine"}, usage={})
    with patch("ouroboros.llm.LLMClient", return_value=client):
        duplicate = events._find_duplicate_task("Plan report", "", tmp_path, scope=TEST_SCOPE, parent_task_id="foreign")
    assert duplicate == "mine"
    assert "Private report" not in client.chat.call_args.kwargs["messages"][-1]["content"]


@parametrize_from_dict({"missing_parent": {"parent": "missing"}, "cyclic_parent": {"parent": "root"}})
def test_broken_ancestor_links_do_not_block_admission(tmp_path, parent):
    """Missing and cyclic ancestry terminates without excluding an unrelated candidate."""
    store = runtime_persistence.initialize().tasks_repo
    store.create_task(
        TaskRequest(id="root", parent_task_id=parent, text="Write report", scope=TEST_SCOPE.to_dict()),
        status="running",
    )
    store.create_task(TaskRequest(id="peer", text="Plan report", scope=TEST_SCOPE.to_dict()), status="running")
    client = make_mock_llm({"content": "peer"}, usage={})
    with patch("ouroboros.llm.LLMClient", return_value=client):
        duplicate = events._find_duplicate_task("Plan report", "", tmp_path, scope=TEST_SCOPE, parent_task_id="root")
    assert duplicate == "peer"
