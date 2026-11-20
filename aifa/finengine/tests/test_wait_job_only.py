"""AIFA only waits for its own background jobs."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from finengine.assistant import goal_tools
from finengine.assistant.goal_tools import WaitTool, _problem_in


def test_task_wait_is_rejected_even_with_job_id():
    for args in ({"task_id": "child", "wait_sec": 240},
                 {"job_id": "job", "task_id": "child", "wait_sec": 240}):
        assert "wait_for_task" in _problem_in(args)


def test_job_wait_is_accepted():
    assert _problem_in({"job_id": "job", "wait_sec": 240}) == ""


@pytest.mark.asyncio
async def test_wait_dispatches_to_its_own_job_board(monkeypatch):
    job = object()
    board = SimpleNamespace(find=lambda run_id, job_id: job if (run_id, job_id) == ("run", "job") else None)
    reply = AsyncMock(return_value="job result")
    monkeypatch.setattr(goal_tools, "reply_of", reply)
    call = SimpleNamespace(ctx=SimpleNamespace(run_id="run"), args={"job_id": "job", "wait_sec": 240})
    assert await WaitTool(board).run(call) == "job result"
    reply.assert_awaited_once_with(board, job, 240.0)
