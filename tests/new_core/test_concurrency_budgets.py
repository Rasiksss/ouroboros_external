"""What concurrent runs consume is bounded through the budget seam, not a module semaphore.

A process-local primitive answers "may one more start" wrongly the moment there are two
processes. What is pinned here is that the model client and the tool round both ask the
same seam, that the numbers relate to the slot count, and that a component which is not a
run cannot be starved by the runs.
"""

from __future__ import annotations

import threading
import time

import pytest

from new_core.coordination.inprocess import InProcessBudget, NullWorkSignal
from new_core.coordination.registry import (
    MODEL_CALLS,
    TOOL_BODIES,
    concurrency_budgets,
    default_budget_limits,
)
from new_core.llm.types import ToolCall
from ouroboros.llm import client as llm_client
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.shared import TEST_SCOPE


@pytest.fixture(autouse=True)
def _restore_budgets():
    yield
    concurrency_budgets.reset_for_tests()


# --- the numbers ------------------------------------------------------------


def test_the_model_budget_sits_above_the_slot_count() -> None:
    """Every run may have a call in flight, and the supervisor's own is not the odd one out."""
    limits = default_budget_limits(worker_slots=4, llm_max_concurrency=2)

    assert limits[MODEL_CALLS] == 5
    assert limits[TOOL_BODIES] == 32


def test_a_configured_model_concurrency_above_the_slots_still_wins() -> None:
    limits = default_budget_limits(worker_slots=2, llm_max_concurrency=10)

    assert limits[MODEL_CALLS] == 10


# --- the model client -------------------------------------------------------


def test_a_caller_with_a_deadline_gives_up_rather_than_waiting() -> None:
    """The supervisor tick is such a caller: blocked, the whole instance stops."""
    concurrency_budgets.initialize(InProcessBudget({MODEL_CALLS: 1, TOOL_BODIES: 1}))
    budgets = concurrency_budgets.budgets
    held = budgets.acquire(MODEL_CALLS, holder="a-run", ttl_sec=60)
    assert held is not None

    started = time.monotonic()
    with pytest.raises(llm_client.LocalLLMDeadlineError):
        with llm_client._llm_concurrency_slot(deadline=time.monotonic() + 0.2):
            pass

    assert time.monotonic() - started < 5
    budgets.release(held)


@pytest.mark.parametrize("end_wait", ["grant", "stop", "lease_loss", "unavailable"])
def test_first_model_budget_wait_excludes_execution_time_and_remains_fenced(tmp_path, monkeypatch, end_wait) -> None:
    import contextvars

    from ouroboros.storage.runtime_backend import ExecutionClaimUnavailable
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from ouroboros.storage.task_results import execution_claim
    from supervisor.queue import TaskQueue
    from tests.helpers.shared import TEST_SCOPE

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(TaskRequest(id="job", detached=True, text="work", scope=TEST_SCOPE.to_dict()))
    claimed = store.tasks_repo.claim_next_task(runtime_id="runtime")
    queue = TaskQueue(
        store.tasks_repo,
        tmp_path,
        work_signal=NullWorkSignal(),
        hard_timeout=0,
        audit_event_repository=sql_audit_event_repository(),
    )
    concurrency_budgets.initialize(InProcessBudget({MODEL_CALLS: 1, TOOL_BODIES: 1}))
    budgets = concurrency_budgets.budgets
    held = budgets.acquire(MODEL_CALLS, holder="a-run", ttl_sec=60)
    assert held is not None
    entered, stopped = threading.Event(), threading.Event()
    failures = []

    def _wait() -> None:
        try:
            with llm_client._llm_concurrency_slot():
                entered.set()
        except (RuntimeError, ExecutionClaimUnavailable) as exc:
            failures.append(exc)

    token = execution_claim.set(("job", claimed.metadata["execution_claim"], stopped.is_set))
    context = contextvars.copy_context()
    execution_claim.reset(token)
    thread = threading.Thread(target=context.run, args=(_wait,), daemon=True)
    thread.start()
    assert not entered.wait(timeout=0.1)
    queue.enforce_task_timeouts()
    assert store.tasks_repo.get_task("job").status == "running"
    assert not store.tasks_repo.get_task("job").metadata.get("run_started_at")
    if end_wait == "stop":
        stopped.set()
    elif end_wait == "lease_loss":
        monkeypatch.setattr(store.tasks_repo, "is_task_attempt_active", lambda *a, **kw: False)
    elif end_wait == "unavailable":
        monkeypatch.setattr(
            store.tasks_repo, "is_task_attempt_active", lambda *a, **kw: (_ for _ in ()).throw(OSError("DB down"))
        )
    budgets.release(held)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert entered.is_set() is (end_wait == "grant")
    assert bool(failures) is (end_wait != "grant")
    assert budgets.in_use(MODEL_CALLS) == 0
    if end_wait == "grant":
        assert store.tasks_repo.get_task("job").metadata["run_started_at"]
        queue.enforce_task_timeouts()
        assert store.tasks_repo.get_task("job").error_code == "hard_timeout"


# --- tool bodies ------------------------------------------------------------


def test_the_tool_round_allowance_is_a_share_of_the_process_budget() -> None:
    from ouroboros.agent import loop_tool_execution

    assert loop_tool_execution._ROUND_TOOL_BODY_ALLOWANCE == 8
    assert default_budget_limits(worker_slots=3, llm_max_concurrency=2)[TOOL_BODIES] == 24


def test_tool_bodies_are_bounded_across_runs() -> None:
    """Two rounds' worth of calls, and the budget never admits more than its ceiling."""
    concurrency_budgets.initialize(InProcessBudget({MODEL_CALLS: 4, TOOL_BODIES: 3}))
    budgets = concurrency_budgets.budgets
    peak = 0
    lock = threading.Lock()
    holding = 0
    done = threading.Event()

    def _body(index: int) -> None:
        nonlocal peak, holding
        lease = budgets.acquire(TOOL_BODIES, holder=f"tool-{index}", ttl_sec=30)
        assert lease is not None
        with lock:
            holding += 1
            peak = max(peak, holding)
        time.sleep(0.02)
        with lock:
            holding -= 1
        budgets.release(lease)

    threads = [threading.Thread(target=_body, args=(index,)) for index in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    done.set()

    assert peak <= 3


def _round_with(monkeypatch, step, *, calls=2):
    """Drive the real parallel round, with only the per-call body replaced.

    Everything under test — the semaphore, the budget acquire, the release and the gather —
    is the production code; ``step`` stands in for what `_execute_with_timeout` returns or
    raises, which is the one thing these cases vary.
    """
    import asyncio
    from types import SimpleNamespace

    from ouroboros.agent import loop_tool_execution
    from ouroboros.agent.loop_tool_execution import ToolCallExecutor

    monkeypatch.setattr(
        loop_tool_execution,
        "resolve_tool_runtime_metadata",
        lambda _name, _catalog, *, scope: SimpleNamespace(parallel_safe=True, timeout_sec=None),
    )
    executor = object.__new__(ToolCallExecutor)
    executor._catalog = SimpleNamespace(code_tool_names=lambda: frozenset())
    executor._get_tool_timeout = lambda _metadata: 5
    executor._execute_with_timeout = lambda tc, **_kwargs: step(tc.id)

    tool_calls = [
        ToolCall(id=f"call_{index}", function=SimpleNamespace(name="noop", arguments="{}")) for index in range(calls)
    ]
    return asyncio.run(executor.aexecute(tool_calls, ctx=SimpleNamespace(scope=TEST_SCOPE), task_id="t1", decisions={}))


def test_a_tool_call_that_raises_does_not_leak_its_grant(monkeypatch) -> None:
    """The release is in a `finally` because `_call` raises for everything the round handles.

    Authorization, the trace span and the journal all sit outside `_execute_single_tool`'s
    own guard, and the gather already turns their exceptions into a result. Before the
    `finally` each such failure held a share of the process budget for its whole 45-minute
    TTL — and the next round's acquire has no deadline to give up on.
    """
    concurrency_budgets.initialize(InProcessBudget({MODEL_CALLS: 4, TOOL_BODIES: 4}))

    async def _raise(_call_id):
        raise RuntimeError("authorize_tool said no")

    results = _round_with(monkeypatch, _raise)

    assert all(str(result).startswith("⚠️ TOOL_ERROR") for result in (r["result"] for r in results))
    assert concurrency_budgets.budgets.in_use(TOOL_BODIES) == 0


def test_a_completed_round_credits_every_grant_back(monkeypatch) -> None:
    concurrency_budgets.initialize(InProcessBudget({MODEL_CALLS: 4, TOOL_BODIES: 4}))

    async def _ok(call_id):
        return {"tool_call_id": call_id, "fn_name": "noop", "result": "done"}

    _round_with(monkeypatch, _ok)

    assert concurrency_budgets.budgets.in_use(TOOL_BODIES) == 0


def test_an_abandoned_body_keeps_its_grant_until_the_lease_expires(monkeypatch) -> None:
    """Its thread is still running, so crediting the budget would describe a bound that fails."""
    from ouroboros.agent.loop_tool_execution import _BODY_ABANDONED_KEY

    concurrency_budgets.initialize(InProcessBudget({MODEL_CALLS: 4, TOOL_BODIES: 4}))

    async def _abandoned(call_id):
        return {"tool_call_id": call_id, "fn_name": "noop", "result": "timeout", _BODY_ABANDONED_KEY: True}

    _round_with(monkeypatch, _abandoned)

    assert concurrency_budgets.budgets.in_use(TOOL_BODIES) == 2


# --- runtime is measured from when the run ran ------------------------------


def test_waiting_for_a_slot_is_not_counted_as_runtime(tmp_path) -> None:
    """A task killed for another's occupancy names the wrong task and repeats until it clears."""
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.queue import TaskQueue
    from tests.helpers.shared import TEST_SCOPE

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(
        TaskRequest(id="waited", type="task", text="go", chat_id=42, scope=TEST_SCOPE.to_dict())
    )
    claimed = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a")
    assert claimed is not None
    # Claimed an hour ago; the body only just started.
    store.tasks_repo.update_task_status("waited", "running", started_at="2020-01-01T00:00:00+00:00")
    store.tasks_repo.heartbeat_task("waited", phase="start")
    queue = TaskQueue(
        store.tasks_repo,
        tmp_path,
        work_signal=NullWorkSignal(),
        soft_timeout=600,
        hard_timeout=1800,
        audit_event_repository=sql_audit_event_repository(),
    )

    meta = queue.list_running()["waited"]

    assert store.tasks_repo.get_task("waited").metadata["run_started_at"]
    assert time.time() - float(meta.started_at) < 60


def test_a_run_that_has_not_beaten_yet_falls_back_to_the_claim(tmp_path) -> None:
    from ouroboros.storage.runtime_models import TaskRequest
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from supervisor.queue import TaskQueue
    from tests.helpers.shared import TEST_SCOPE

    store = runtime_persistence.initialize()
    store.tasks_repo.create_task(
        TaskRequest(id="fresh", type="task", text="go", chat_id=42, scope=TEST_SCOPE.to_dict())
    )
    store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a")
    queue = TaskQueue(
        store.tasks_repo,
        tmp_path,
        work_signal=NullWorkSignal(),
        soft_timeout=600,
        hard_timeout=1800,
        audit_event_repository=sql_audit_event_repository(),
    )

    meta = queue.list_running()["fresh"]

    assert meta.started_at > 0
