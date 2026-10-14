import json
from types import SimpleNamespace

from ouroboros.storage.runtime_models import TaskRecord, TaskRequest
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.task_results import STATUS_COMPLETED, write_task_result
from ouroboros.tools.recent_tasks import _handle_recent_tasks
from tests.helpers.shared import TEST_SCOPE


def _ctx(tmp_path, scope=TEST_SCOPE):
    """The tool's context, scope included: this report is read straight into a prompt."""
    return SimpleNamespace(drive_root=tmp_path, scope=scope)


def _write_task(root, name, *, result="done", ts="2026-01-01T00:00:00Z", **extra):
    runtime_persistence.initialize()
    write_task_result(
        root,
        name,
        STATUS_COMPLETED,
        scope=TEST_SCOPE,
        result=result,
        ts=ts,
        text=f"task {name}",
        total_rounds=3,
        **extra,
    )


def test_recent_tasks_empty_drive(tmp_path):
    runtime_persistence.initialize()
    data = json.loads(_handle_recent_tasks(_ctx(tmp_path)))
    assert data == {"active": [], "running": [], "tasks": [], "unreadable_tasks": []}


def test_recent_tasks_returns_preview_by_default(tmp_path):
    _write_task(tmp_path, "abc123", result="hello world")

    data = json.loads(_handle_recent_tasks(_ctx(tmp_path)))

    assert data["tasks"][0]["task_id"] == "abc123"
    assert data["tasks"][0]["description"] == "task abc123"
    assert data["tasks"][0]["result_preview"] == "hello world"
    assert "result" not in data["tasks"][0]
    assert "trace_summary" not in data["tasks"][0]


def test_recent_tasks_include_results_and_traces(tmp_path):
    _write_task(tmp_path, "abc123", result="full result", trace_summary="trace")

    data = json.loads(
        _handle_recent_tasks(
            _ctx(tmp_path),
            include_results=True,
            include_traces=True,
        )
    )

    assert data["tasks"][0]["result"] == "full result"
    assert data["tasks"][0]["trace_summary"] == "trace"


def test_recent_tasks_sorted_by_activity_timestamp(tmp_path):
    store = runtime_persistence.initialize()
    for task_id, created_at, finished_at in (
        ("created-latest", "2026-01-04T00:00:00Z", "2026-01-02T00:00:00Z"),
        ("finished-latest", "2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z"),
    ):
        store.tasks_repo.upsert_task(
            TaskRecord(
                owner_id=TEST_SCOPE.owner_id,
                agent_id=TEST_SCOPE.agent_id,
                task_id=task_id,
                status="completed",
                created_at=created_at,
                finished_at=finished_at,
            )
        )

    data = json.loads(_handle_recent_tasks(_ctx(tmp_path), limit=1))

    assert [row["task_id"] for row in data["tasks"]] == ["finished-latest"]


def test_recent_tasks_limit_is_clamped(tmp_path):
    for idx in range(25):
        _write_task(tmp_path, f"task{idx}", ts=f"2026-01-01T00:{idx:02d}:00Z")

    data = json.loads(_handle_recent_tasks(_ctx(tmp_path), limit=99))

    assert len(data["tasks"]) == 20


def test_recent_tasks_reports_active_tasks_and_keeps_running_field(tmp_path):
    store = runtime_persistence.initialize()
    for status in (
        "requested",
        "queued",
        "scheduled",
        "running",
        "interrupted",
        "waiting_confirmation",
    ):
        store.tasks_repo.create_task(
            TaskRequest(id=f"{status}1", type="task", text=f"{status} work", scope=TEST_SCOPE.to_dict()),
            status=status,
        )

    data = json.loads(_handle_recent_tasks(_ctx(tmp_path), limit=10))

    assert {(row["task_id"], row["status"]) for row in data["active"]} == {
        ("requested1", "requested"),
        ("queued1", "queued"),
        ("scheduled1", "scheduled"),
        ("running1", "running"),
        ("interrupted1", "interrupted"),
        ("waiting_confirmation1", "waiting_confirmation"),
    }
    assert data["running"] == [
        {
            "task_id": "running1",
            "status": "running",
            "description": "running work",
            "ts": data["running"][0]["ts"],
        }
    ]


def test_recent_tasks_returns_newest_active_task_beyond_store_page(tmp_path):
    store = runtime_persistence.initialize()
    for index in range(501):
        created_at = f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}Z"
        store.tasks_repo.upsert_task(
            TaskRecord(
                owner_id=TEST_SCOPE.owner_id,
                agent_id=TEST_SCOPE.agent_id,
                task_id=f"task{index:03d}",
                status="requested",
                created_at=created_at,
                metadata={"description": f"work {index}"},
            )
        )

    data = json.loads(_handle_recent_tasks(_ctx(tmp_path), limit=1))

    assert [row["task_id"] for row in data["active"]] == ["task500"]


def _other_owner():
    from new_core.identity import OwnerScope, SurfaceId

    return OwnerScope(owner_id="another-owner", agent_id="another-agent", surface_id=SurfaceId("browser"), chat_id="9")


def test_recent_tasks_shows_only_the_asking_owners_tasks(tmp_path):
    """One owner's descriptions and results must not reach another owner's prompt."""
    runtime_persistence.initialize()
    other = _other_owner()
    write_task_result(tmp_path, "mine", STATUS_COMPLETED, scope=TEST_SCOPE, result="my answer", text="my task")
    write_task_result(tmp_path, "theirs", STATUS_COMPLETED, scope=other, result="their answer", text="their task")

    data = json.loads(_handle_recent_tasks(_ctx(tmp_path), include_results=True))

    assert [task["task_id"] for task in data["tasks"]] == ["mine"]
    assert "their answer" not in json.dumps(data, ensure_ascii=False)


def test_recent_tasks_shows_only_the_asking_owners_running_and_active_tasks(tmp_path):
    store = runtime_persistence.initialize()
    other = _other_owner()
    store.tasks_repo.create_task(TaskRequest(id="mine-active", type="task", text="mine", scope=TEST_SCOPE.to_dict()))
    store.tasks_repo.create_task(TaskRequest(id="theirs-active", type="task", text="theirs", scope=other.to_dict()))
    store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a")
    store.tasks_repo.claim_next_task(worker_id=1, runtime_id="runtime-a")

    data = json.loads(_handle_recent_tasks(_ctx(tmp_path)))

    assert [task["task_id"] for task in data["running"]] == ["mine-active"]
    assert [task["task_id"] for task in data["active"]] == ["mine-active"]
