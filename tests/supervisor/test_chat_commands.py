"""Chat commands use the incoming scope, while explicit job ids span the agent."""

from types import SimpleNamespace

from ouroboros.storage.runtime_models import TaskRequest
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.commands import handle_slash_command
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, scope_with
from tests.helpers.supervisor import bootstrap_supervisor_for_tests
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader


@parametrize_from_dict({"disabled": {"enabled": False}, "enabled": {"enabled": True}})
@parametrize_from_dict(
    {
        "chat": {"source": "browser", "axis": "chat_id", "value": "neighbor"},
        "surface": {"source": "browser", "axis": "surface_id", "value": "dialogs"},
        "dialogs": {"source": "dialogs", "axis": "surface_id", "value": "browser"},
    }
)
def test_stop_and_status_are_chat_scoped_but_jobs_and_named_stop_span_chats(
    tmp_path, monkeypatch, enabled, source, axis, value
):
    monkeypatch.setenv("OUROBOROS_DURABLE_JOBS_ENABLED", str(enabled).lower())
    bootstrap_supervisor_for_tests(tmp_path)
    store = runtime_persistence.current
    here = scope_with(TEST_SCOPE, surface_id=source)
    other = scope_with(here, **{axis: value})
    for task_id, scope, parent in [
        ("here-running", here, ""),
        ("here-waiting", here, ""),
        ("here-child", here, "here-running"),
        ("neighbor-root", other, ""),
    ]:
        store.tasks_repo.create_task(
            TaskRequest(id=task_id, text=task_id, scope=scope.to_dict(), parent_task_id=parent)
        )
    store.tasks_repo.create_task(TaskRequest(id="neighbor-background", detached=True, scope=other.to_dict()))
    store.tasks_repo.claim_next_task(runtime_id="runtime")
    store.tasks_repo.create_task(TaskRequest(id="finished", scope=here.to_dict()), status="completed")
    for foreign_axis in ("owner_id", "agent_id"):
        foreign = scope_with(here, **{foreign_axis: "foreign"})
        store.tasks_repo.create_task(TaskRequest(id=foreign_axis, scope=foreign.to_dict()))
    sent = []
    ctx = SimpleNamespace(
        tool_confirmation_reader=sql_tool_confirmation_reader(),
        tasks_repo=store.tasks_repo,
        send_with_budget=lambda message, **kwargs: sent.append((message, kwargs)),
    )
    handle_slash_command("/status", ctx=ctx, scope=here, source=source)
    assert "here-waiting" in sent[-1][0] and "here-running" in sent[-1][0]
    assert "neighbor-root" not in sent[-1][0]
    handle_slash_command("/jobs", ctx=ctx, scope=here, source=source)
    assert {line.split(" — ")[0] for line in sent[-1][0].splitlines()} == {
        "here-running",
        "here-waiting",
        "neighbor-root",
        "neighbor-background",
    }
    assert sent[-1][1] == {"scope": here, "source": source}
    handle_slash_command("/stop", ctx=ctx, scope=here, source=source)
    assert store.tasks_repo.get_task("here-running").stop_requested_at
    assert store.tasks_repo.get_task("here-waiting").status == "cancelled"
    assert store.tasks_repo.get_task("here-child").status == "cancelled"
    assert store.tasks_repo.get_task("neighbor-root").status == "queued"
    handle_slash_command("/stop neighbor-root", ctx=ctx, scope=here, source=source)
    assert store.tasks_repo.get_task("neighbor-root").status == "cancelled"
