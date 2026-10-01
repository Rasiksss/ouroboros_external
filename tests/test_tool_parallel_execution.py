"""Read-only tools in one round still run concurrently, now that the round is one coroutine.

``ToolCallExecutor.execute`` bridges once per round and ``aexecute`` gathers a batch of
``READ_ONLY_PARALLEL_TOOLS`` on that single loop. Nothing about ``asyncio.gather`` makes a
batch concurrent by itself: the bodies are plain blocking functions, so the parallelism is
entirely the thread placement underneath them. An inline default runtime would serialize the
batch — every call awaited to completion before the next one starts — while every other test
in the suite still passed, and nothing but a profile would notice.

This is the test that fails if it does. It asserts both halves: that all the bodies are
inside a barrier at the same time, and that each ran on a distinct thread.
"""

from __future__ import annotations

import json
import threading

from new_core.llm.types import ToolCall, ToolCallFunction
from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.agent.loop_adapters import build_tool_call_executor
from ouroboros.policy import safety
from ouroboros.policy.capabilities import READ_ONLY_PARALLEL_TOOLS
from tests._shared import collecting_event_sink, make_catalog, static_tool_provider

# Parallel-eligible *and* on the headless surface: a name the policy excludes never reaches
# a provider at all, and would leave the barrier one waiter short.
_PARALLEL_NAMES = ["calculator", "data_list", "data_read"]


def test_read_only_tools_in_one_round_are_not_serialized(tmp_path, monkeypatch):
    assert set(_PARALLEL_NAMES) <= READ_ONLY_PARALLEL_TOOLS

    monkeypatch.setattr(safety, "check_safety", lambda *args, **kwargs: (True, ""))

    # Every handler must be inside the barrier at the same time for any of them to leave it.
    # Serialized, the first call would hold it until the timeout and come back broken.
    barrier = threading.Barrier(len(_PARALLEL_NAMES))
    threads: list[int] = []
    lock = threading.Lock()

    def _handler(name: str):
        def _run(ctx, **kwargs):
            with lock:
                threads.append(threading.get_ident())
            barrier.wait(timeout=10)
            return f"{name}-done"

        return _run

    provider = static_tool_provider(
        *(
            ToolEntry(
                name=name,
                schema=ToolSchema(name=name, description="d", parameters={}),
                handler=_handler(name),
            )
            for name in _PARALLEL_NAMES
        )
    )
    catalog = make_catalog([provider])
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    calls = [
        ToolCall(id=f"call-{i}", function=ToolCallFunction(name=name, arguments=json.dumps({})))
        for i, name in enumerate(_PARALLEL_NAMES)
    ]

    executor = build_tool_call_executor(catalog=catalog, drive_logs=tmp_path / "logs", events=collecting_event_sink())
    results = executor.execute(
        calls,
        ctx=ctx,
        task_id="task-parallel",
    )

    assert [r["result"] for r in results] == [f"{name}-done" for name in _PARALLEL_NAMES]
    assert len(set(threads)) == len(_PARALLEL_NAMES)
