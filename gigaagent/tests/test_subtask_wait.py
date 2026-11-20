"""Subtasks are awaited at their owning runtime, without AIFA notifications."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ouroboros.tools.service import orchestration as tasks


@pytest.fixture
def waiting(monkeypatch):
    ctx = SimpleNamespace(task_id="root", root_task_id="root", stop_requested=lambda: False)
    current = SimpleNamespace(status=tasks.STATUS_RUNNING, result="", detached=False, metadata={}, input={})
    monkeypatch.setattr(tasks, "_current_task", lambda ctx, task_id: current)
    monkeypatch.setattr(tasks, "_worker_slots", lambda: 2)
    return ctx, current


@pytest.mark.asyncio
async def test_wait_observes_completion_without_aifa_notification(waiting):
    ctx, current = waiting

    async def complete():
        await asyncio.sleep(0.01)
        current.status = tasks.STATUS_COMPLETED
        current.result = "full plan"

    completing = asyncio.create_task(complete())
    result = await tasks._wait_for_task(ctx, "child", wait_sec=1)
    await completing
    assert "[completed]" in result and "full plan" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [tasks.STATUS_COMPLETED, tasks.STATUS_FAILED])
async def test_terminal_task_returns_immediately(waiting, monkeypatch, status):
    ctx, current = waiting
    current.status, current.result = status, "saved result"
    sleep = Mock(side_effect=AssertionError("terminal task must not sleep"))
    monkeypatch.setattr(tasks.asyncio, "sleep", sleep)
    assert "saved result" in await tasks._wait_for_task(ctx, "child", wait_sec=1)


@pytest.mark.asyncio
async def test_timeout_returns_running_status(waiting):
    ctx, _ = waiting
    assert "[running]" in await tasks._wait_for_task(ctx, "child", wait_sec=0.01)


@pytest.mark.asyncio
async def test_wait_stops_cooperatively(waiting):
    ctx, _ = waiting
    ctx.stop_requested = lambda: True
    assert "stopped" in await tasks._wait_for_task(ctx, "child", wait_sec=1)


@pytest.mark.asyncio
async def test_unknown_or_inaccessible_task_does_not_wait(waiting, monkeypatch):
    ctx, _ = waiting
    monkeypatch.setattr(tasks, "_current_task", lambda ctx, task_id: None)
    assert "unknown" in await tasks._wait_for_task(ctx, "child", wait_sec=1)


@pytest.mark.asyncio
async def test_single_worker_child_wait_is_blocked(waiting, monkeypatch):
    ctx, current = waiting
    current.input = {"parent_task_id": "root"}
    monkeypatch.setattr(tasks, "_worker_slots", lambda: 1)
    assert "WAIT_BLOCKED_SINGLE_WORKER" in await tasks._wait_for_task(ctx, "child", wait_sec=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [-1, 241, True, float("nan"), "240"])
async def test_invalid_wait_duration_is_rejected(waiting, seconds):
    ctx, _ = waiting
    assert "ERROR" in await tasks._wait_for_task(ctx, "child", wait_sec=seconds)


def test_foreign_owner_task_is_inaccessible(monkeypatch):
    foreign = SimpleNamespace(owner_id="other", agent_id="agent")
    repo = SimpleNamespace(get_current_job=lambda *a, **kw: None, get_task=lambda _: foreign)
    monkeypatch.setattr(tasks, "runtime_persistence", SimpleNamespace(current=SimpleNamespace(tasks_repo=repo)))
    ctx = SimpleNamespace(scope=SimpleNamespace(owner_id="owner", agent_id="agent"))
    assert tasks._current_task(ctx, "foreign") is None
