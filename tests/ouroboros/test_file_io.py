import asyncio

import anyio
import pytest

from ouroboros.utils import finish_file_io
from tests.helpers.parametrize import parametrize_from_dict


@parametrize_from_dict(
    {
        "anyio": {"mode": "anyio"},
        "task": {"mode": "task"},
        "repeated_task": {"mode": "repeated_task"},
    }
)
def test_file_io_finishes_before_cancellation_without_spinning(monkeypatch, mode):
    shields = 0
    original = asyncio.shield

    def counted(*args):
        nonlocal shields
        shields += 1
        return original(*args)

    monkeypatch.setattr(asyncio, "shield", counted)

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        scopes, completed = [], []

        async def operation():
            started.set()
            await release.wait()
            completed.append(True)

        async def caller():
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                with pytest.raises(asyncio.CancelledError):
                    await finish_file_io(operation())
                assert completed == [True]

        task = asyncio.create_task(caller())
        await started.wait()
        if mode == "anyio":
            scopes[0].cancel()
        else:
            task.cancel()
        for index in range(30):
            await asyncio.sleep(0)
            if mode == "repeated_task" and index == 15:
                task.cancel()
        assert not task.done()
        release.set()
        await task
        assert shields <= 3

    anyio.run(scenario)
