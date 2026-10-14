from types import SimpleNamespace

from ouroboros.storage.runtime_models import TaskRequest
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.utils import utc_now_iso
from supervisor.commands import handle_slash_command
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.runtime_store import claim_task, park_run
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader


@parametrize_from_dict(
    {
        "named": {"command": "/stop root", "durable": True},
        "foreground": {"command": "/stop", "durable": False},
        "durable": {"command": "/stop", "durable": True},
    }
)
@parametrize_from_dict({"waiting": {"answered": False}, "answered": {"answered": True}})
def test_tree_stop_keeps_a_parked_child_claimable_for_cleanup_on_repeated_stop(
    tmp_path, monkeypatch, command, durable, answered
):
    monkeypatch.setenv("OUROBOROS_DURABLE_JOBS_ENABLED", str(durable).lower())
    store = runtime_persistence.initialize()
    for task_id, parent in [("root", ""), ("child", "root")]:
        store.tasks_repo.create_task(
            TaskRequest(id=task_id, job_id=task_id, parent_task_id=parent, text="work", scope=TEST_SCOPE.to_dict()),
            status="running",
        )
    store.tasks_repo.heartbeat_task("child", phase="model")
    opened = park_run(store, task_id="child")
    assert opened is not None
    if answered:
        assert sql_tool_confirmation_reader(store.engine).resolve_tool_confirmation(
            opened.confirmation_id, decision="approved", now_iso=utc_now_iso()
        )
        assert sql_tool_confirmation_reader(store.engine).requeue_parked_task("child")
    notices = []
    ctx = SimpleNamespace(
        tasks_repo=store.tasks_repo,
        task_stop_publisher=SimpleNamespace(publish=notices.append),
        send_with_budget=lambda *args, **kwargs: None,
    )

    for _ in range(2):
        handle_slash_command(command, ctx=ctx, scope=TEST_SCOPE, source="browser")
        child = store.tasks_repo.get_task("child")
        assert (child.status, bool(child.stop_requested_at)) == ("queued", True)
        assert "run_started_at" not in child.metadata
        assert not sql_tool_confirmation_reader(store.engine).resolve_tool_confirmation(
            opened.confirmation_id, decision="approved", now_iso=utc_now_iso()
        )

    claimed = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="cleanup-runtime")
    assert claimed is not None and claimed.task_id == "child"
    assert claimed.stop_requested_at
    assert sql_tool_confirmation_reader(store.engine).take_parked_snapshot("child") is not None


def test_foreground_stop_reaches_an_answered_root_before_it_resumes(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_DURABLE_JOBS_ENABLED", "false")
    store = runtime_persistence.initialize()
    claim_task(store)
    opened = park_run(store)
    assert sql_tool_confirmation_reader(store.engine).resolve_tool_confirmation(
        opened.confirmation_id, decision="approved", now_iso=utc_now_iso()
    )
    assert sql_tool_confirmation_reader(store.engine).requeue_parked_task("task1")
    store.tasks_repo.create_task(TaskRequest(id="unrelated", text="later", scope=TEST_SCOPE.to_dict()))
    ctx = SimpleNamespace(tasks_repo=store.tasks_repo, send_with_budget=lambda *args, **kwargs: None)

    handle_slash_command("/stop", ctx=ctx, scope=TEST_SCOPE, source="browser")

    stopped = store.tasks_repo.get_task("task1")
    assert (stopped.status, bool(stopped.stop_requested_at)) == ("queued", True)
    assert store.tasks_repo.get_task("unrelated").status == "cancelled"
    assert store.tasks_repo.get_task("unrelated").stop_requested_at is None
