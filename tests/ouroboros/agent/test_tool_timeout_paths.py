"""The timeout path of a round: abandonment, the late result, and the returns before it.

These are the behaviours that survived the move from ``ThreadPoolExecutor`` +
``future.result(timeout=...)`` to ``asyncio.wait`` over an explicit task, and every one of
them is invisible to a test whose tools return promptly:

- a body that outlives its deadline is *abandoned*, so the round returns without it. Nothing
  but elapsed time notices; a rewrite onto ``asyncio.to_thread`` would pass every other test
  and quietly hold the round for as long as the hung body runs.
- a reviewed-mutative tool keeps running past its soft timeout, up to a hard ceiling, and a
  result arriving in between is returned for real and marked late. ``asyncio.wait_for``
  cancels what it awaits, which is why the two-phase wait is built on ``asyncio.wait``
  instead; a rewrite onto ``wait_for`` would lose the commit gate's late result silently.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import types

import pytest

from new_core.llm.types import ToolCall, ToolCallFunction
from ouroboros.agent import loop_tool_execution as mod
from ouroboros.policy import safety
from tests.helpers.factories import permissive_safety
from tests.helpers.shared import (
    collecting_event_sink,
    execute_tool_round,
    make_static_tool_env,
    make_tool_call,
    make_tool_entry,
)


@pytest.fixture(autouse=True)
def _safety_backend_is_not_what_these_test(monkeypatch):
    monkeypatch.setattr(safety, "check_safety", permissive_safety)


def _await_late_outcome(caplog, timeout: float = 10.0) -> None:
    """Block until the abandoned body has reported, so the report lands inside this test.

    Nothing joins an abandoned body — that is the behaviour under test — so its worker
    thread logs whenever it wakes. A test that returns first leaves that line to be written
    against a log stream the harness has already torn down.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any("Abandoned tool body" in record.getMessage() for record in caplog.records):
            return
        time.sleep(0.01)
    raise AssertionError("the abandoned body never reported its late outcome")


# --- abandonment ---------------------------------------------------------------------


def test_a_tool_outliving_its_timeout_does_not_hold_the_round(tmp_path, monkeypatch):
    """5.1 — the timeout result *and* a round that returns while the body still runs.

    Asserting only on the result string would pass even if the round waited out the body;
    elapsed time is the whole point.
    """
    release = threading.Event()
    started = threading.Event()

    def _hangs(ctx, **kwargs):
        started.set()
        release.wait(timeout=30)
        return "too late"

    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_read", _hangs))
    monkeypatch.setattr(mod.ToolCallExecutor, "_get_tool_timeout", lambda *args: 1)
    events = collecting_event_sink()

    began = time.perf_counter()
    try:
        results = execute_tool_round(catalog, ctx, tmp_path, [make_tool_call("data_read")], events)
        elapsed = time.perf_counter() - began

        assert results[0]["result"].startswith("⚠️ TOOL_TIMEOUT (data_read)")
        assert results[0]["is_error"] is True
        assert results[0]["result_meta"]["status"] == "timeout"
        assert started.is_set(), "the body never ran"
        assert not release.is_set(), "the body is still blocked — it must have been abandoned"
        assert elapsed < 10, f"the round waited for the abandoned body ({elapsed:.2f}s)"

        assert [e["timeout_sec"] for e in events.live_logs("tool_call_timeout")] == [1]
        assert events.live_logs("tool_call_finished") == []
    finally:
        release.set()


def test_an_abandoned_body_that_settles_afterwards_is_logged_not_silently_dropped(tmp_path, monkeypatch, caplog):
    """3.6 — an abandoned body's late outcome is attributed, without unhandled-error noise.

    This is the real production shape, and it is not what ``_abandon`` sees. ``task.cancel()``
    ends the round's task ``CancelledError`` at once. The body keeps running on its thread, and
    when it raises, the *provider* catches it (``except Exception`` → ``⚠️ TOOL_ERROR``), so its
    worker future settles with a *result*, not an exception — a result nobody retrieves, since
    the round already returned a timeout. ``ThreadToolRuntime`` owns that future, so that is
    where the discarded outcome is logged. Three things are pinned together: the late outcome
    *is* reported (one WARNING naming the tool), it does *not* read like a new defect (no
    "exception never retrieved" surfacing at shutdown), and it does *not* carry the discarded
    result — which here is a provider-built string quoting whatever the body raised, the shape
    that puts arbitrary tool output into a central log line.
    """
    import gc
    import logging

    release = threading.Event()
    raised = threading.Event()

    def _fails_late(ctx, **kwargs):
        release.wait(timeout=30)
        raised.set()
        raise RuntimeError("failed after the round moved on")

    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_read", _fails_late))
    monkeypatch.setattr(mod.ToolCallExecutor, "_get_tool_timeout", lambda *args: 1)

    with caplog.at_level(logging.DEBUG):
        results = execute_tool_round(catalog, ctx, tmp_path, [make_tool_call("data_read")], collecting_event_sink())
        release.set()
        assert raised.wait(timeout=10)
        # The failure logs from the worker thread's future callback, which fires just after the
        # body raises — a moment after ``raised`` is set. Wait for it rather than racing it.
        deadline = time.time() + 5
        while time.time() < deadline and not any("Abandoned tool body" in r.getMessage() for r in caplog.records):
            time.sleep(0.02)
        gc.collect()

    assert results[0]["result"].startswith("⚠️ TOOL_TIMEOUT")
    assert "never retrieved" not in caplog.text
    late = [r for r in caplog.records if "Abandoned tool body" in r.getMessage()]
    assert len(late) == 1
    assert late[0].levelno == logging.WARNING
    # The provider turned the body's RuntimeError into a ⚠️ TOOL_ERROR result, so the future
    # settled successfully — a discarded *result*, which the line names but never carries.
    assert "finished after its deadline" in late[0].getMessage()
    assert "tool=data_read" in late[0].getMessage()
    assert "failed after the round moved on" not in caplog.text


# --- the reviewed-mutative two-phase wait ---------------------------------------------


@pytest.fixture
def commit_gate(monkeypatch):
    """A stand-in for ``ouroboros.tools.commit_gate``, which this profile does not ship.

    The timeout path imports it lazily and swallows the ``ImportError``, so without this the
    two calls the commit gate depends on would be unobservable — the tests below would pass
    against code that never made them.
    """
    calls: dict[str, list[dict]] = {"late": [], "attempt": []}
    fake = types.ModuleType("ouroboros.tools.commit_gate")
    fake._mark_review_attempt_late = lambda ctx, **kw: calls["late"].append(kw)
    fake._record_commit_attempt = lambda ctx, **kw: calls["attempt"].append(kw)
    monkeypatch.setitem(sys.modules, "ouroboros.tools.commit_gate", fake)
    return calls


def test_a_reviewed_mutative_tool_finishing_after_the_soft_timeout_returns_its_real_result(
    tmp_path, monkeypatch, commit_gate
):
    """5.2 — the path ``asyncio.wait_for`` cannot express, because it cancels what it awaits."""
    monkeypatch.setattr(mod, "REVIEWED_MUTATIVE_TOOLS", frozenset({"data_write"}))
    monkeypatch.setattr(mod.ToolCallExecutor, "_get_tool_timeout", lambda *args: 1)

    def _slow_commit(ctx, **kwargs):
        time.sleep(2)
        return "committed"

    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_write", _slow_commit))
    events = collecting_event_sink()

    results = execute_tool_round(catalog, ctx, tmp_path, [make_tool_call("data_write")], events)

    assert results[0]["result"] == "committed"
    assert results[0]["is_error"] is False

    late = events.live_logs("tool_call_late")
    assert [e["soft_timeout_sec"] for e in late] == [1]
    assert "hard ceiling: 1800s" in late[0]["message"]
    assert [e.get("late") for e in events.live_logs("tool_call_finished")] == [True]
    assert events.live_logs("tool_call_timeout") == []

    assert [c["soft_timeout_sec"] for c in commit_gate["late"]] == [1]
    assert commit_gate["attempt"] == [], "the ceiling was never reached"


def test_reaching_the_hard_ceiling_records_a_late_result_pending_terminal_state(
    tmp_path, monkeypatch, commit_gate, caplog
):
    """5.3 — the ceiling still says the operation may land later, so the gate can react.

    The waits are compressed rather than the policy changed: ``remaining`` is
    ``ceiling - soft`` and the ceiling is at least ``soft + 60``, so a faithful run of this
    path costs a minute of wall clock. The reported values are asserted to be the real ones.
    """
    real_wait = asyncio.wait

    async def _compressed_wait(aws, *, timeout=None, **kwargs):
        return await real_wait(aws, timeout=None if timeout is None else min(timeout, 0.5), **kwargs)

    monkeypatch.setattr(asyncio, "wait", _compressed_wait)
    monkeypatch.setattr(mod, "REVIEWED_MUTATIVE_TOOLS", frozenset({"data_write"}))
    monkeypatch.setattr(mod.ToolCallExecutor, "_get_tool_timeout", lambda *args: 1)

    release = threading.Event()

    def _never_commits(ctx, **kwargs):
        release.wait(timeout=30)
        return "too late"

    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_write", _never_commits))
    events = collecting_event_sink()
    caplog.set_level(logging.WARNING, logger="new_core.tools.runtimes")

    try:
        results = execute_tool_round(catalog, ctx, tmp_path, [make_tool_call("data_write")], events)

        assert results[0]["result"].startswith("⚠️ TOOL_TIMEOUT (data_write)")
        assert "hard ceiling" in results[0]["result"]
        assert results[0]["result_meta"]["status"] == "timeout"

        ceiling_events = events.live_logs("tool_call_timeout")
        assert [e["hard_ceiling"] for e in ceiling_events] == [True]
        assert [e["timeout_sec"] for e in ceiling_events] == [1800]

        assert len(commit_gate["late"]) == 1, "the soft timeout still marks the attempt late"
        assert len(commit_gate["attempt"]) == 1
        attempt = commit_gate["attempt"][0]
        assert attempt["late_result_pending"] is True
        assert attempt["status"] == "failed"
        assert attempt["block_reason"] == "infra_failure"
        assert attempt["phase"] == "late_hard_ceiling"
        assert not release.is_set(), "the body is still running — it must have been abandoned"
    finally:
        release.set()
        _await_late_outcome(caplog)


# --- the returns before execution -------------------------------------------------------


def test_a_tool_that_is_not_active_returns_before_any_body_runs(tmp_path):
    """5.4 — the not-enabled gate: a started and a finished line, no body, the unchanged refusal."""
    ran: list[int] = []
    catalog, ctx = make_static_tool_env(
        tmp_path, make_tool_entry("calculator", lambda ctx, **kw: ran.append(1) or "ok")
    )
    events = collecting_event_sink()

    results = execute_tool_round(
        catalog, ctx, tmp_path, [make_tool_call("calculator")], events, active_tool_names=frozenset()
    )

    assert results[0]["result"].startswith("⚠️ TOOL_NOT_ENABLED")
    assert results[0]["is_error"] is True
    assert ran == []
    assert [e["tool"] for e in events.live_logs("tool_call_started")] == ["calculator"]
    assert [e["is_error"] for e in events.live_logs("tool_call_finished")] == [True]


def test_unparseable_arguments_return_before_any_body_runs(tmp_path):
    """5.4 — the argument gate reports both live-log lines and never reaches the provider."""
    ran: list[int] = []
    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_read", lambda ctx, **kw: ran.append(1) or "ok"))
    events = collecting_event_sink()
    broken = ToolCall(id="call-0", function=ToolCallFunction(name="data_read", arguments="{not json"))

    results = execute_tool_round(catalog, ctx, tmp_path, [broken], events)

    assert results[0]["result"].startswith("⚠️ TOOL_ARG_ERROR")
    assert results[0]["is_error"] is True
    assert ran == []
    assert len(events.live_logs("tool_call_started")) == 1
    assert [e["is_error"] for e in events.live_logs("tool_call_finished")] == [True]


def test_a_repeat_blocked_call_returns_before_any_body_runs(tmp_path):
    """5.4 — the repeat guard's preflight, reported as its own status rather than an error class."""
    ran: list[int] = []
    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_read", lambda ctx, **kw: ran.append(1) or "ok"))
    events = collecting_event_sink()

    class _AlwaysBlocks:
        def preflight(self, fn_name, args):
            return "⚠️ REPEAT_BLOCKED: you already tried this"

    results = execute_tool_round(
        catalog, ctx, tmp_path, [make_tool_call("data_read")], events, repeat_guard=_AlwaysBlocks()
    )

    assert results[0]["result"] == "⚠️ REPEAT_BLOCKED: you already tried this"
    assert results[0]["result_meta"]["status"] == "repeat_blocked"
    assert ran == []
    assert [e["status"] for e in events.live_logs("tool_call_finished")] == ["repeat_blocked"]
