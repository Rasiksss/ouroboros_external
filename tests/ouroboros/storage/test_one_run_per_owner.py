"""Atomic claims enforce one ordinary root per chat and the owner-wide running ceiling."""

from __future__ import annotations

import pathlib
import threading

import pytest

from ouroboros.storage.runtime_models import TaskRequest
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.runtime_store import make_tool_confirmation
from tests.helpers.shared import TEST_SCOPE, scope_with
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader


def _queued(
    store, task_id: str, *, scope=TEST_SCOPE, task_type: str = "task", parent: str = "", detached: bool = False
) -> None:
    store.tasks_repo.create_task(
        TaskRequest(
            id=task_id,
            type=task_type,
            text="work",
            chat_id=scope.chat_id,
            scope=scope.to_dict(),
            parent_task_id=parent,
            detached=detached,
        )
    )


def _other_owner():
    return scope_with(TEST_SCOPE, owner_id="someone-else")


def test_a_second_task_of_one_owner_waits_while_their_first_runs(tmp_path: pathlib.Path) -> None:
    store = runtime_persistence.initialize()
    _queued(store, "first")
    _queued(store, "second")

    claimed = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a")
    blocked = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a")

    assert claimed is not None and claimed.task_id == "first"
    assert blocked is None


@parametrize_from_dict(
    {"chat": {"axis": "chat_id", "value": "another-chat"}, "surface": {"axis": "surface_id", "value": "dialogs"}}
)
def test_foreground_roots_serialize_only_within_the_full_chat_key(tmp_path, axis, value):
    store = runtime_persistence.initialize()
    _queued(store, "first")
    _queued(store, "same-chat")
    _queued(store, "other-chat", scope=scope_with(TEST_SCOPE, **{axis: value}))
    claims = [store.tasks_repo.claim_next_task(runtime_id="runtime") for _ in range(3)]
    assert [row.task_id if row else None for row in claims] == ["first", "other-chat", None]
    store.tasks_repo.update_task_status("first", "completed")
    assert store.tasks_repo.claim_next_task(runtime_id="runtime").task_id == "same-chat"


@parametrize_from_dict({"disabled": {"enabled": False}, "enabled": {"enabled": True}})
def test_ordinary_jobs_queue_beyond_active_limit_but_share_running_limit(tmp_path, enabled):
    store = runtime_persistence.initialize()
    requests = [
        TaskRequest(id=f"root-{i}", scope=scope_with(TEST_SCOPE, agent_id=f"agent-{i}").to_dict()) for i in range(6)
    ]
    rows = [
        runtime_persistence.current.create_root_task.execute(request, enabled=enabled, max_active=5)
        for request in requests
    ]
    assert [row.status for row in rows] == ["queued"] * 6
    assert not any(row.error_code for row in rows)
    assert (
        runtime_persistence.current.create_root_task.execute(requests[0], enabled=enabled, max_active=5).task_id
        == "root-0"
    )
    claims = [store.tasks_repo.claim_next_task(runtime_id="runtime") for _ in range(4)]
    assert [row.task_id if row else None for row in claims] == ["root-0", "root-1", "root-2", None]
    _queued(store, "child", parent="root-0")
    assert store.tasks_repo.claim_next_task(runtime_id="runtime").task_id == "child"
    store.tasks_repo.update_task_status("root-0", "completed")
    assert store.tasks_repo.claim_next_task(runtime_id="runtime").task_id == "root-3"


def test_task_scope_controls_indexed_identity_and_survives_retry(tmp_path):
    store = runtime_persistence.initialize()
    original = TaskRequest(id="original", source="unknown", chat_id=None, scope=TEST_SCOPE.to_dict())
    created = store.tasks_repo.create_task(original)
    claimed = store.tasks_repo.claim_next_task(runtime_id="runtime")
    assert claimed is not None
    from sqlalchemy import text

    with store.engine.connect() as conn:
        axes = conn.execute(text("SELECT owner_id, agent_id, surface_id, chat_id FROM tasks")).one()
    assert tuple(axes) == tuple(TEST_SCOPE.to_dict().values())
    assert created.chat_id == claimed.chat_id == TEST_SCOPE.chat_id
    assert claimed.to_agent_input()["scope"] == TEST_SCOPE.to_dict()
    store.tasks_repo.transition_running(
        "original",
        kind="instance_released",
        result="retry",
        attempt=1,
        runtime_id="runtime",
        successor=original.model_copy(update={"id": "retry", "chat_id": "stale"}),
    )
    retry = store.tasks_repo.claim_next_task(runtime_id="other-runtime")
    assert (retry.task_id, retry.job_id, retry.chat_id, retry.surface_id) == (
        "retry",
        "original",
        TEST_SCOPE.chat_id,
        str(TEST_SCOPE.surface_id),
    )
    store.tasks_repo.update_task_status("retry", "completed", chat_id="wrong")
    assert store.tasks_repo.get_task("retry").to_agent_input()["scope"] == TEST_SCOPE.to_dict()


def test_another_owners_task_is_claimed_while_the_first_owner_runs(tmp_path: pathlib.Path) -> None:
    store = runtime_persistence.initialize()
    _queued(store, "mine")
    _queued(store, "theirs", scope=_other_owner())

    first = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a")
    second = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a")

    assert {first.task_id, second.task_id} == {"mine", "theirs"}


def test_foreground_roots_remain_independent_between_an_owners_agents(tmp_path: pathlib.Path) -> None:
    store = runtime_persistence.initialize()
    _queued(store, "agent-a")
    _queued(store, "agent-b", scope=scope_with(TEST_SCOPE, agent_id="other-agent"))

    first = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a")
    second = store.tasks_repo.claim_next_task(worker_id=1, runtime_id="runtime-a")

    assert {first.task_id, second.task_id} == {"agent-a", "agent-b"}


def test_a_subtask_of_a_running_parent_is_claimable(tmp_path: pathlib.Path) -> None:
    """A subtask inherits its parent's scope; counting records would deadlock the parent."""
    store = runtime_persistence.initialize()
    _queued(store, "parent")
    parent = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a")
    assert parent is not None
    _queued(store, "child", parent="parent")

    child = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a")

    assert child is not None and child.task_id == "child"


def test_the_owner_slot_frees_when_the_root_run_finishes(tmp_path: pathlib.Path) -> None:
    store = runtime_persistence.initialize()
    _queued(store, "first")
    _queued(store, "second")
    store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a")

    store.tasks_repo.update_task_status("first", "completed", result="done")

    resumed = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a")
    assert resumed is not None and resumed.task_id == "second"


def test_a_running_background_lane_does_not_hold_the_owners_slot(tmp_path: pathlib.Path) -> None:
    """The exempt types are named, not inferred from which statuses happen to be claimable."""
    store = runtime_persistence.initialize()
    _queued(store, "protocol", task_type="a2a")
    store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a", include_task_types=["a2a"])
    _queued(store, "real")

    claimed = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a", exclude_task_types=["a2a"])

    assert claimed is not None and claimed.task_id == "real"


def test_only_positive_user_roots_participate_in_the_running_limit(tmp_path: pathlib.Path) -> None:
    store = runtime_persistence.initialize()
    _queued(store, "unknown-lane", task_type="future-system-work")
    assert store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a") is not None
    _queued(store, "user-root")

    claimed = store.tasks_repo.claim_next_task(worker_id=1, runtime_id="runtime-a")

    assert claimed is not None and claimed.task_id == "user-root"


def test_durable_claims_share_an_owner_wide_ceiling_and_foreground_lane(tmp_path: pathlib.Path) -> None:
    store = runtime_persistence.initialize()
    _queued(store, "foreground")
    _queued(store, "job-a", detached=True)
    _queued(store, "job-b", detached=True)

    first = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a", max_running_root_jobs_per_owner=2)
    second = store.tasks_repo.claim_next_task(worker_id=1, runtime_id="runtime-a", max_running_root_jobs_per_owner=2)
    blocked = store.tasks_repo.claim_next_task(worker_id=2, runtime_id="runtime-a", max_running_root_jobs_per_owner=2)

    assert {first.task_id, second.task_id} == {"foreground", "job-a"}
    assert blocked is None


def test_accepted_detached_work_uses_all_available_owner_slots(tmp_path: pathlib.Path) -> None:
    store = runtime_persistence.initialize()
    _queued(store, "job-a", detached=True)
    _queued(store, "job-b", detached=True)

    assert store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a", max_running_root_jobs_per_owner=2)
    assert store.tasks_repo.claim_next_task(worker_id=1, runtime_id="runtime-a", max_running_root_jobs_per_owner=2)


def test_a_job_parked_on_its_owners_answer_still_holds_the_ceiling(tmp_path: pathlib.Path) -> None:
    """Parking frees the worker, not the owner's lane: the job is going to run again."""
    store = runtime_persistence.initialize()
    _queued(store, "job", detached=True)
    claimed = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a", max_running_root_jobs_per_owner=2)
    assert sql_tool_confirmation_reader(store.engine).park_and_open(
        task_id="job",
        claim_token=claimed.metadata["execution_claim"],
        confirmation=make_tool_confirmation(task_id="job"),
        snapshot={"round_idx": 1},
        workspace="",
        arguments={},
    )

    _queued(store, "their-other-agent", scope=scope_with(TEST_SCOPE, agent_id="other-agent"))
    assert store.tasks_repo.claim_next_task(worker_id=1, runtime_id="runtime-a", max_running_root_jobs_per_owner=1)
    _queued(store, "over-the-ceiling")

    blocked = store.tasks_repo.claim_next_task(worker_id=2, runtime_id="runtime-a", max_running_root_jobs_per_owner=1)

    assert blocked is None


def test_two_instances_claiming_at_once_never_both_start_a_second_task(tmp_path: pathlib.Path) -> None:
    """The predicate is in the statement, so it holds between instances as the claim does."""
    store = runtime_persistence.initialize()
    _queued(store, "running")
    assert store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a") is not None
    for index in range(6):
        _queued(store, f"queued-{index}")

    claimed: list[str] = []
    barrier = threading.Barrier(2)

    def _claim(runtime_id: str) -> None:
        barrier.wait()
        record = store.tasks_repo.claim_next_task(worker_id=0, runtime_id=runtime_id)
        if record is not None:
            claimed.append(record.task_id)

    threads = [threading.Thread(target=_claim, args=(f"runtime-{name}",)) for name in ("b", "c")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert claimed == []


def test_two_claimants_starting_from_nothing_do_not_both_take_one_owner(tmp_path: pathlib.Path) -> None:
    """The predicate reads a snapshot; the unique index is what refuses the second write."""
    from sqlalchemy import text

    store = runtime_persistence.initialize()
    _queued(store, "first")
    _queued(store, "second")
    claimed = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a")
    assert claimed is not None

    # What two concurrent transactions each believe: neither sees the other's running row.
    with store.engine.connect() as conn:
        with pytest.raises(Exception) as refusal:
            conn.execute(text("UPDATE tasks SET status='running', runtime_id='runtime-b' WHERE task_id='second'"))
            conn.commit()

    assert "unique" in str(refusal.value).lower() or "constraint" in str(refusal.value).lower()
