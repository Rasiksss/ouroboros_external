"""Runtimes: where a tool body runs, and how the catalog decides which placement runs it.

No pytest-asyncio in this tree, so every asynchronous scenario is driven by ``asyncio.run``
from a synchronous test — which is also what the production bridge does.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import gc
import logging
import threading
import time
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import pytest

from new_core.tools import runtimes
from new_core.tools.context import ToolContext
from new_core.tools.interfaces import IToolProvider, IToolRuntime, ToolDescriptor
from new_core.tools.runtimes import THREAD_RUNTIME, InlineToolRuntime, ThreadToolRuntime
from new_core.tools.schema import ToolSchema
from tests._shared import make_catalog


@pytest.fixture
def ctx(tmp_path) -> ToolContext:
    return ToolContext(repo_dir=tmp_path, drive_root=tmp_path)


def _descriptor(name: str, *, runtime: IToolRuntime | None = None) -> ToolDescriptor:
    return ToolDescriptor(
        schema=ToolSchema(name=name, description="d", parameters={}),
        timeout_sec=7,
        origin="builtin",
        surface="discoverable",
        capability_class="read_only",
        safety_policy="check",
        runtime=runtime,
    )


class _Provider(IToolProvider):
    """Records what reached ``execute`` and the thread it arrived on."""

    default_runtime = THREAD_RUNTIME

    def __init__(
        self,
        descriptors: Sequence[ToolDescriptor],
        *,
        result: str = "ok",
        raises: BaseException | None = None,
        aliases: dict[str, str] | None = None,
    ) -> None:
        self._descs = {desc.name: desc for desc in descriptors}
        self._aliases = aliases or {}
        self._result = result
        self._raises = raises
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.threads: list[int] = []

    def refresh(self) -> None:
        return None

    def descriptors(self) -> list[ToolDescriptor]:
        return list(self._descs.values())

    def get(self, name: str) -> ToolDescriptor | None:
        return self._descs.get(self._aliases.get(name, name))

    def refuse_dispatch(self, name: str) -> str | None:
        return None

    def execute(self, ctx: Any, name: str, args: dict[str, Any]) -> str:
        self.calls.append((name, args))
        self.threads.append(threading.get_ident())
        if self._raises is not None:
            raise self._raises
        return self._result


class _RecordingRuntime(IToolRuntime):
    """Records the resolution it was handed, then behaves inline."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[tuple[IToolProvider, str, dict[str, Any]]] = []

    async def run(
        self,
        provider: IToolProvider,
        ctx: ToolContext,
        name: str,
        args: dict[str, Any],
    ) -> str:
        self.calls.append((provider, name, args))
        return f"{self.label}:{provider.execute(ctx, name, args)}"


# --- the runtimes themselves -----------------------------------------------------------


def test_inline_runtime_runs_the_body_on_the_calling_thread(ctx):
    """4.1 — inline creates no thread and no loop of its own; the result is untouched."""
    provider = _Provider([_descriptor("t")], result="from-provider")
    calling_thread = threading.get_ident()

    result = asyncio.run(InlineToolRuntime().run(provider, ctx, "t", {"a": 1}))

    assert result == "from-provider"
    assert provider.calls == [("t", {"a": 1})]
    assert provider.threads == [calling_thread]


def test_inline_runtime_awaits_an_awaitable_result(ctx):
    """4.2 — the path that lets an async source drop its own thread bridge later (D6)."""

    class _AwaitableProvider(_Provider):
        def execute(self, ctx: Any, name: str, args: dict[str, Any]) -> Any:
            super().execute(ctx, name, args)

            async def _body() -> str:
                await asyncio.sleep(0)
                return "awaited"

            return _body()

    provider = _AwaitableProvider([_descriptor("t")])

    assert asyncio.run(InlineToolRuntime().run(provider, ctx, "t", {})) == "awaited"
    assert provider.calls == [("t", {})]


def test_thread_runtime_runs_off_the_loop_without_blocking_the_caller(ctx):
    """4.3 — the body is on another thread, and the awaiting coroutine keeps running."""
    started = threading.Event()
    release = threading.Event()

    class _BlockingProvider(_Provider):
        def execute(self, ctx: Any, name: str, args: dict[str, Any]) -> str:
            started.set()
            release.wait(timeout=5)
            return super().execute(ctx, name, args)

    provider = _BlockingProvider([_descriptor("t")])
    calling_thread = threading.get_ident()

    async def scenario() -> tuple[str, int]:
        task = asyncio.create_task(ThreadToolRuntime().run(provider, ctx, "t", {}))
        # The body blocks until ``release`` is set. Reaching that line at all is the
        # assertion: were the loop blocked, this coroutine would never resume to set it.
        loop_turns = 0
        while not started.is_set() and loop_turns < 500:
            await asyncio.sleep(0.01)
            loop_turns += 1
        assert started.is_set(), "the body never started — the calling loop was blocked"
        release.set()
        return await asyncio.wait_for(task, timeout=5), loop_turns

    result, loop_turns = asyncio.run(scenario())

    assert result == "ok"
    assert loop_turns >= 1
    assert len(provider.threads) == 1
    assert provider.threads[0] != calling_thread


def test_thread_runtime_awaits_an_awaitable_result(ctx):
    """1.2 — a provider may return an awaitable from a thread; the placement awaits it (D8)."""

    class _AwaitableProvider(_Provider):
        def execute(self, ctx: Any, name: str, args: dict[str, Any]) -> Any:
            super().execute(ctx, name, args)

            async def _body() -> str:
                await asyncio.sleep(0)
                return "awaited"

            return _body()

    provider = _AwaitableProvider([_descriptor("t")])

    assert asyncio.run(ThreadToolRuntime().run(provider, ctx, "t", {})) == "awaited"


def test_an_abandoned_body_does_not_delay_the_loop_shutting_down(ctx):
    """1.5 — the regression the per-call executor exists to prevent (D3).

    ``asyncio.to_thread`` would submit to the loop's default executor, which ``asyncio.run``
    joins without a deadline on 3.10 — so a body that ignores its deadline would hold the
    round open for as long as it runs. Nothing but elapsed time notices.
    """
    release = threading.Event()
    finished = threading.Event()

    class _SlowProvider(_Provider):
        def execute(self, ctx: Any, name: str, args: dict[str, Any]) -> str:
            result = super().execute(ctx, name, args)
            release.wait(timeout=10)
            finished.set()
            return result

    provider = _SlowProvider([_descriptor("t")])

    async def scenario() -> None:
        task = asyncio.create_task(ThreadToolRuntime().run(provider, ctx, "t", {}))
        while not provider.threads:  # noqa: ASYNC110 - a threading.Event cannot be awaited
            await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    started = time.perf_counter()
    try:
        asyncio.run(scenario())
        elapsed = time.perf_counter() - started
        assert elapsed < 1.0, f"asyncio.run waited for the abandoned body ({elapsed:.2f}s)"
        assert not finished.is_set(), "the body finished — it was joined, not abandoned"
    finally:
        release.set()


def test_the_callers_context_reaches_the_body(ctx):
    """1.6 — ``run_in_executor`` propagates no contextvars, so the runtime copies them."""
    probe: contextvars.ContextVar[str] = contextvars.ContextVar("probe", default="unset")
    seen: list[str] = []

    class _ReadingProvider(_Provider):
        def execute(self, ctx: Any, name: str, args: dict[str, Any]) -> str:
            seen.append(probe.get())
            return super().execute(ctx, name, args)

    provider = _ReadingProvider([_descriptor("t")])

    async def scenario() -> str:
        probe.set("set-by-caller")
        return await ThreadToolRuntime().run(provider, ctx, "t", {})

    assert asyncio.run(scenario()) == "ok"
    assert seen == ["set-by-caller"]


def test_two_thread_placements_under_gather_overlap_on_distinct_threads(ctx):
    """1.7 — what makes ``asyncio.gather`` over a read-only batch real parallelism.

    The barrier is the gate: serialized, the first body waits alone and raises
    ``BrokenBarrierError`` rather than hanging.
    """
    barrier = threading.Barrier(2, timeout=10)
    threads: list[int] = []
    lock = threading.Lock()

    class _BarrierProvider(_Provider):
        def execute(self, ctx: Any, name: str, args: dict[str, Any]) -> str:
            with lock:
                threads.append(threading.get_ident())
            barrier.wait()
            return super().execute(ctx, name, args)

    provider = _BarrierProvider([_descriptor("a"), _descriptor("b")])
    runtime = ThreadToolRuntime()

    async def scenario() -> list[str]:
        return await asyncio.gather(
            runtime.run(provider, ctx, "a", {}),
            runtime.run(provider, ctx, "b", {}),
        )

    assert asyncio.run(scenario()) == ["ok", "ok"]
    assert len(set(threads)) == 2


def test_an_abandoned_body_that_raises_later_is_logged_once_with_its_call(ctx, caplog):
    """1.8 — a body failing after the round moved on is attributed, not left as mystery noise.

    Two requirements held together. The abandoned failure is *reported* — one WARNING naming
    the tool, so a late mutation that fails is not dropped on the floor. It must not read like
    a *new defect*: no "exception never retrieved" surfacing and no trip through the loop's
    default exception handler, both of which arrive with no call site attached.
    """
    release = threading.Event()
    raised = threading.Event()

    class _FailingProvider(_Provider):
        def execute(self, ctx: Any, name: str, args: dict[str, Any]) -> str:
            super().execute(ctx, name, args)
            release.wait(timeout=10)
            raised.set()
            raise RuntimeError("failed after abandonment")

    provider = _FailingProvider([_descriptor("t")])
    handled: list[dict[str, Any]] = []

    async def scenario() -> None:
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: handled.append(context))
        task = asyncio.create_task(ThreadToolRuntime().run(provider, ctx, "t", {}))
        while not provider.threads:  # noqa: ASYNC110 - a threading.Event cannot be awaited
            await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        release.set()
        while not raised.is_set():  # noqa: ASYNC110 - let it fail while the loop is still up
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(scenario())
        gc.collect()

    assert raised.is_set()
    assert handled == []
    assert "never retrieved" not in caplog.text
    late = [r for r in caplog.records if "Abandoned tool body" in r.getMessage()]
    assert len(late) == 1
    assert late[0].levelno == logging.WARNING
    # This provider lets the body's exception propagate, so the future settles with it.
    assert "raised after its deadline" in late[0].getMessage()
    assert "tool=t" in late[0].getMessage()
    # A late failure goes out whole: this line is the only account of why the call died, and
    # unlike a result (see the payload test below) the exception is the body's own.
    assert "failed after abandonment" in late[0].getMessage()
    assert "never retrieved" not in caplog.text


def test_a_body_settling_as_the_round_gives_up_is_reported_exactly_once(ctx, caplog, monkeypatch):
    """1.8 — the other half of the window: settling *between* the cancellation and the unwind.

    Abandonment used to be flagged in a ``finally``, and only for a body found unfinished. A
    body that settled in between had already run its done callback while the flag was clear,
    and no second callback was coming — so the late outcome vanished with no warning at all.
    Holding the loop thread until the future is genuinely settled pins that ordering; whichever
    of the two reporters gets there first, the outcome is reported once and not twice.
    """
    submitted: list[Future] = []

    class _CapturingExecutor(ThreadPoolExecutor):
        def submit(self, fn, /, *args, **kwargs):  # type: ignore[override]
            future = super().submit(fn, *args, **kwargs)
            submitted.append(future)
            return future

    monkeypatch.setattr(runtimes, "ThreadPoolExecutor", _CapturingExecutor)
    release = threading.Event()

    class _JustInTimeProvider(_Provider):
        def execute(self, ctx: Any, name: str, args: dict[str, Any]) -> str:
            result = super().execute(ctx, name, args)
            release.wait(timeout=10)
            return result

    provider = _JustInTimeProvider([_descriptor("t")], result="late-result")

    async def scenario() -> None:
        task = asyncio.create_task(ThreadToolRuntime().run(provider, ctx, "t", {}))
        while not provider.threads:  # noqa: ASYNC110 - a threading.Event cannot be awaited
            await asyncio.sleep(0.01)
        task.cancel()
        release.set()
        # Synchronous on purpose, and the whole point: never yielding keeps the loop from
        # resuming the cancelled task, so the body settles before the runtime unwinds.
        deadline = time.perf_counter() + 10
        while not submitted[0].done() and time.perf_counter() < deadline:
            time.sleep(0.001)  # noqa: ASYNC251 - blocking the loop here is the scenario
        assert submitted[0].done(), "the body never settled — the ordering under test never happened"
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.05)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(scenario())
        gc.collect()

    late = [r for r in caplog.records if "Abandoned tool body" in r.getMessage()]
    assert len(late) == 1
    assert late[0].levelno == logging.WARNING
    assert "finished after its deadline" in late[0].getMessage()
    assert "tool=t" in late[0].getMessage()


def test_an_abandoned_result_is_reported_without_carrying_it(ctx, caplog):
    """The late line names the call; the discarded *result* never rides along.

    A result is arbitrary tool output — file contents, a token the body was handed — and this
    line goes to the central log, where a WARNING about placement has no business carrying any
    of it. (A late *exception* is the opposite case and is logged whole: see 1.8.)
    """
    secret = "api_key=TOP-SECRET-123"
    release = threading.Event()
    settled = threading.Event()

    class _SecretProvider(_Provider):
        def execute(self, ctx: Any, name: str, args: dict[str, Any]) -> str:
            super().execute(ctx, name, args)
            release.wait(timeout=10)
            settled.set()
            return f"contents of secrets.env: {secret}"

    provider = _SecretProvider([_descriptor("t")])

    async def scenario() -> None:
        task = asyncio.create_task(ThreadToolRuntime().run(provider, ctx, "t", {}))
        while not provider.threads:  # noqa: ASYNC110 - a threading.Event cannot be awaited
            await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        release.set()
        while not settled.is_set():  # noqa: ASYNC110 - let it settle while the loop is still up
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(scenario())
        gc.collect()

    late = [r for r in caplog.records if "Abandoned tool body" in r.getMessage()]
    assert len(late) == 1
    assert "finished after its deadline" in late[0].getMessage()
    assert "tool=t" in late[0].getMessage()
    assert "TOP-SECRET-123" not in caplog.text
    assert "api_key" not in caplog.text
    assert "secrets.env" not in caplog.text


@pytest.mark.parametrize("runtime_cls", [InlineToolRuntime, ThreadToolRuntime])
def test_runtime_propagates_a_provider_exception_unchanged(ctx, runtime_cls):
    """4.4 — a runtime adds no error text of its own; error handling lives above it (D5)."""
    boom = RuntimeError("provider blew up")
    provider = _Provider([_descriptor("t")], raises=boom)

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(runtime_cls().run(provider, ctx, "t", {}))

    assert excinfo.value is boom


# --- resolution -------------------------------------------------------------------------


def test_descriptor_runtime_wins_over_the_provider_default(ctx):
    """4.5 — the descriptor's own runtime is the override."""
    from_descriptor = _RecordingRuntime("descriptor")
    from_provider = _RecordingRuntime("provider")
    provider = _Provider([_descriptor("t", runtime=from_descriptor)])
    provider.default_runtime = from_provider
    catalog = make_catalog([provider])

    assert asyncio.run(catalog.aexecute(ctx, "t", {})) == "descriptor:ok"
    assert from_provider.calls == []


def test_provider_default_is_used_when_the_descriptor_names_none(ctx):
    """4.5 — with no override, the owning provider's default decides."""
    from_provider = _RecordingRuntime("provider")
    provider = _Provider([_descriptor("t")])
    provider.default_runtime = from_provider
    catalog = make_catalog([provider])

    assert asyncio.run(catalog.aexecute(ctx, "t", {})) == "provider:ok"
    assert [name for _, name, _ in from_provider.calls] == ["t"]


def test_an_alias_reaches_the_runtime_as_the_canonical_name(ctx):
    """4.6 — placement resolves against the same owner dispatch resolves against."""
    runtime = _RecordingRuntime("second")
    first = _Provider([_descriptor("other")])
    second = _Provider([_descriptor("canonical")], aliases={"alias": "canonical"})
    second.default_runtime = runtime
    catalog = make_catalog([first, second])

    assert asyncio.run(catalog.aexecute(ctx, "alias", {"k": "v"})) == "second:ok"
    assert runtime.calls == [(second, "canonical", {"k": "v"})]
    assert first.calls == []


def test_a_provider_that_names_no_runtime_resolves_to_the_thread_default(ctx):
    """4.1/4.5 — the interface supplies the default, and it places the body off the loop.

    Every provider's ``execute`` is blocking synchronous code, so an inline default would
    make ``asyncio.gather`` over a read-only batch run one call after another — silently,
    with nothing but a profile to notice.
    """
    provider = _Provider([_descriptor("t")])
    calling_thread = threading.get_ident()

    assert provider.default_runtime is THREAD_RUNTIME
    assert asyncio.run(make_catalog([provider]).aexecute(ctx, "t", {})) == "ok"
    assert provider.threads == [provider.threads[0]]
    assert provider.threads[0] != calling_thread


# ``aexecute`` on an unresolvable name is the catalog's contract, owned by
# ``test_tool_catalog.py::test_executing_an_unknown_name_reports_it`` — not re-asserted here.


# --- the synchronous bridge, one level up -------------------------------------------------
#
# The catalog no longer carries one: ``ToolCallExecutor.execute`` crosses over once per round,
# and a second entry point below it would be a second answer to the same question. The two
# behaviours that used to be asserted here — what a synchronous caller gets back, and the
# refusal inside a running loop — are asserted against that bridge instead.


def _executor(catalog, tmp_path):
    from ouroboros.agent.loop_adapters import build_tool_call_executor
    from tests._shared import collecting_event_sink

    return build_tool_call_executor(catalog=catalog, drive_logs=tmp_path / "logs", events=collecting_event_sink())


def _tool_call(name: str, args: dict[str, Any] | None = None):
    import json

    from new_core.llm.types import ToolCall, ToolCallFunction

    return ToolCall(id=f"call-{name}", function=ToolCallFunction(name=name, arguments=json.dumps(args or {})))


def test_the_round_bridge_returns_what_the_provider_returned(ctx, tmp_path, monkeypatch):
    """4.7 — a synchronous caller sees exactly what it saw before any of this existed."""
    from ouroboros.policy import safety

    monkeypatch.setattr(safety, "check_safety", lambda *a, **kw: (True, ""))
    provider = _Provider([_descriptor("calculator")], result="from-provider")

    results = _executor(make_catalog([provider]), tmp_path).execute(
        [_tool_call("calculator", {"x": 1})],
        ctx=ctx,
        task_id="task-bridge",
    )

    assert [r["result"] for r in results] == ["from-provider"]
    assert provider.calls == [("calculator", {"x": 1})]


def test_the_round_bridge_refuses_to_run_inside_a_running_loop(ctx, tmp_path):
    """4.8 — the caller is asynchronous and should await ``aexecute``; say so."""
    provider = _Provider([_descriptor("calculator")])

    async def scenario() -> list[dict[str, Any]]:
        return _executor(make_catalog([provider]), tmp_path).execute(
            [_tool_call("calculator")],
            ctx=ctx,
            task_id="task-bridge",
        )

    with pytest.raises(RuntimeError, match="aexecute"):
        asyncio.run(scenario())

    assert provider.calls == []


def test_one_loop_serves_a_whole_round(ctx, tmp_path, monkeypatch):
    """4.7 — crossing over happens once per round, not once per call.

    The catalog's per-call bridge would have created one loop per tool; the round's bridge
    creates one and every call below it runs on that same loop.
    """
    from ouroboros.policy import safety

    monkeypatch.setattr(safety, "check_safety", lambda *a, **kw: (True, ""))
    loops: list[int] = []

    class _LoopReadingRuntime(IToolRuntime):
        async def run(self, provider, ctx, name, args):
            loops.append(id(asyncio.get_running_loop()))
            return provider.execute(ctx, name, args)

    runtime = _LoopReadingRuntime()
    provider = _Provider([_descriptor("calculator", runtime=runtime), _descriptor("data_read", runtime=runtime)])

    _executor(make_catalog([provider]), tmp_path).execute(
        [_tool_call("calculator"), _tool_call("data_read")],
        ctx=ctx,
        task_id="task-one-loop",
    )

    assert len(loops) == 2
    assert len(set(loops)) == 1
