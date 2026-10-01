"""Shared helpers for the Ouroboros test suite.

These functions are reused across multiple ``tests/test_*.py`` modules to
avoid duplicated boilerplate (extension-loader cleanup, claude_agent_sdk
mock installation). They are intentionally plain module-level callables,
not fixtures — many callers need them at module import time.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

from new_core.loop import CancellationSignal
from new_core.tools.runtimes import THREAD_RUNTIME
from ouroboros.agent.interfaces import ForegroundPressure


class FakeCancellation(CancellationSignal):
    """A stop signal a test drives by hand; production has no never-stopping signal."""

    def __init__(self, stopped: bool = False) -> None:
        self.stopped = stopped

    def is_stopped(self) -> bool:
        return self.stopped

    def request_stop(self) -> None:
        self.stopped = True


class FakeForegroundPressure(ForegroundPressure):
    """Foreground pressure a test sets by hand."""

    def __init__(self, pending: bool = False) -> None:
        self.pending = pending

    def is_pending(self) -> bool:
        return self.pending


def clean_extension_runtime_state() -> None:
    """Reset every extension_loader namespace to a pristine state.

    Superset of cleanup logic that previously lived (with minor variations)
    in ``test_skill_exec.py``, ``test_extensions_api.py`` and
    ``test_extension_loader.py``. Extra clears are inert when the namespace
    is already empty, so the superset is safe for every caller.
    """
    from ouroboros.skills.extensions import loader as extension_loader

    with extension_loader._lock:
        loaded_names = list(extension_loader._extensions.keys())
    for name in loaded_names:
        extension_loader.unload_extension(name)
    with extension_loader._lock:
        extension_loader._extension_modules.clear()
        extension_loader._load_failures.clear()
        extension_loader._unloading.clear()
        extension_loader._lifecycle_locks.clear()
        extension_loader._tools.clear()


def ensure_claude_agent_sdk_mock() -> None:
    """Install a lightweight ``claude_agent_sdk`` mock when truly absent.

    Uses ``importlib.util.find_spec`` so an installed-but-not-yet-imported
    SDK is never masked. Idempotent — safe to call from multiple modules at
    import time.
    """
    import importlib.util as _ilu

    try:
        spec = _ilu.find_spec("claude_agent_sdk")
        sdk_available = spec is not None
    except (ValueError, ModuleNotFoundError):
        sdk_available = "claude_agent_sdk" in sys.modules
    if sdk_available:
        return
    mock_sdk = types.ModuleType("claude_agent_sdk")
    mock_sdk.ClaudeAgentOptions = type("ClaudeAgentOptions", (), {})
    mock_sdk.ClaudeSDKClient = type("ClaudeSDKClient", (), {})
    mock_sdk.HookMatcher = type("HookMatcher", (), {"__init__": lambda self, **kw: None})
    mock_sdk.AssistantMessage = type("AssistantMessage", (), {})
    mock_sdk.ResultMessage = type("ResultMessage", (), {})
    mock_sdk.query = lambda **kw: None
    sys.modules["claude_agent_sdk"] = mock_sdk


def seed_skill_execution_state(drive_root, loaded) -> None:
    """Enable a discovered skill and persist grants for execution tests."""
    from ouroboros.skills.loader import save_enabled, save_skill_grants

    save_enabled(drive_root, loaded.name, True)
    save_skill_grants(
        drive_root,
        loaded.name,
        [],
        content_hash=str(loaded.content_hash or ""),
        requested_keys=[],
        granted_permissions=[],
        requested_permissions=[],
    )


def make_safe_mock_ctx(tmp_path, *, repo_dir=None):
    """Return a MagicMock ToolContext whose drive paths resolve to real dirs.

    Several observability paths still need concrete drive log directories.
    A bare MagicMock would stringify into a filename in the repo root.
    """
    ctx = MagicMock()
    ctx.repo_dir = repo_dir if repo_dir is not None else tmp_path
    ctx.drive_root = tmp_path
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    ctx.drive_logs.return_value = logs
    ctx.emit_progress_fn = lambda *a, **kw: None
    ctx.task_id = "test-task"
    return ctx


def static_tool_provider(*entries):
    """A provider over ad-hoc ``ToolEntry`` objects.

    Replaces the removed ``ToolRegistry.register()`` for tests that need one made-up
    tool on the surface: append it to a registry's ``providers`` list, or pass it to
    ``handle_tool_calls`` / ``run_tool`` directly.
    """
    from new_core.tools.interfaces import IToolProvider
    from new_core.tools.providers.builtin import descriptor_from_entry

    class _StaticToolProvider(IToolProvider):
        default_runtime = THREAD_RUNTIME

        def __init__(self, items):
            self._entries = {entry.name: entry for entry in items}
            self._descriptors = {name: descriptor_from_entry(entry, "builtin") for name, entry in self._entries.items()}

        def refresh(self):
            return

        def descriptors(self):
            return list(self._descriptors.values())

        def get(self, name):
            return self._descriptors.get(name)

        def refuse_dispatch(self, name):
            return None

        def entries(self):
            return dict(self._entries)

        def execute(self, ctx, name, args):
            entry = self._entries.get(name)
            if entry is None:
                return f"⚠️ Unknown tool: {name}"
            try:
                return entry.handler(ctx, **args)
            except TypeError as exc:
                return f"⚠️ TOOL_ARG_ERROR ({name}): {exc}"
            except Exception as exc:
                return f"⚠️ TOOL_ERROR ({name}): {exc}"

    return _StaticToolProvider(entries)


def make_catalog(providers=()):
    """A catalog of its own over ``providers``, wired the way the lifespan wires the shared one.

    Production has exactly one ``ToolCatalog`` — the process-wide ``tool_catalog``, wired
    by the server lifespan. A test wants a catalog it can scope to its own fakes without
    disturbing that one, so it constructs its own and calls the same ``initialize``.
    """
    from new_core.tools.catalog import ToolCatalog

    catalog = ToolCatalog()
    catalog.initialize(list(providers))
    return catalog


def collecting_event_sink():
    """An ``EventSink`` that keeps what it was told, for tests that do not assert on events.

    ``ToolCallExecutor`` is built with a sink — every path out of a tool call reports one
    live log line — so a test that only wants the results still has to supply one. Collecting
    rather than dropping means a test that *does* care can read ``.log_payloads``.
    """
    from typing import Any

    from new_core.loop.interfaces import EventSink

    class _CollectingEventSink(EventSink):
        def __init__(self) -> None:
            self.log_payloads: list[dict[str, Any]] = []
            self.progress_calls: list[str] = []

        def progress(self, text):
            self.progress_calls.append(text)

        def typing(self):
            return None

        def emit_tool_live_log(self, payload):
            self.log_payloads.append(payload)

        def usage(self, model, usage, kind):
            return None

        def maybe_checkpoint(self, messages, *, round_idx, max_rounds, accumulated_usage):
            return False

        def emit_result(self, *, text, usage, trace, tool_ctx):
            return None

    return _CollectingEventSink()


def run_tool_calls(
    tool_calls,
    catalog,
    drive_logs,
    task_id,
    messages,
    llm_trace,
    emit_progress=lambda _text: None,
    *,
    ctx,
    events=None,
    active_tool_names=None,
    task_ledger=None,
    repeat_guard=None,
):
    """Execute tool calls and record them, returning the error count.

    ``ToolCallExecutor.execute`` runs the calls and returns raw results;
    ``process_results`` is what appends them to ``messages`` / ``llm_trace``. The
    ``exec_tools`` node makes both calls, and so does every test that asserts on the
    transcript a round produced.

    The first of the two is the synchronous bridge — the same entry point the node reaches,
    which runs ``aexecute`` on a loop of its own for the round. Tests therefore stay
    synchronous; only a test that already has a loop needs the coroutine.

    ``events`` defaults to a collecting sink: production wires the run's real one into the
    executor, and a test that does not care about live logs should not have to build one.
    """
    del emit_progress  # kept for signature compatibility; recording no longer reports progress
    from ouroboros.agent.loop_adapters import build_tool_call_executor

    executor = build_tool_call_executor(
        catalog=catalog,
        drive_logs=drive_logs,
        events=events if events is not None else collecting_event_sink(),
        task_ledger=task_ledger,
        repeat_guard=repeat_guard,
    )
    results = executor.execute(
        tool_calls,
        ctx=ctx,
        task_id=task_id,
        active_tool_names=active_tool_names,
    )
    return executor.process_results(
        results,
        messages=messages,
        trace=llm_trace,
        ctx=ctx,
    )


def run_tool(catalog, ctx, name, args=None):
    """Run one tool call through the gates, from a synchronous test.

    ``dispatch.execute_tool`` is a coroutine and has no synchronous twin: production reaches
    it through ``handle_tool_calls``, which bridges once per round, and a second production
    -shaped synchronous entry point would be one nothing in production uses. The bridge for
    tests lives here instead, in one place, so the next change has one call site to update.

    Not a substitute for :func:`run_tool_calls` — that one drives a whole round, including
    timeouts and live logs. This is the single-call path, for tests asserting on what a gate
    or a provider returned.
    """
    import asyncio

    from ouroboros.tools.dispatch import execute_tool

    return asyncio.run(execute_tool(catalog, ctx, name, dict(args or {})))


def make_tool_env(repo_dir, drive_root):
    """The catalog and the context a tool test needs, as production builds them.

    The provider list mirrors the one the server lifespan installs, over the test's own
    roots. Returns the pair rather than an object that owns both: bundling them behind one
    ``execute`` is what ``ToolRegistry`` did, and keeping a per-task context on a
    long-lived object is exactly why it was retired. Tests call
    ``run_tool(catalog, ctx, ...)``, which bridges to the coroutine production awaits.
    """
    import pathlib

    from new_core.tools.context import ToolContext
    from new_core.tools.providers import (
        A2aToolProvider,
        BuiltinToolProvider,
        ExtensionToolProvider,
        McpToolProvider,
        SkillToolProvider,
    )
    from ouroboros.app.config import get_a2a_tool_timeout_sec
    from ouroboros.egress.a2a.manager import a2a_manager

    repo_dir, drive_root = pathlib.Path(repo_dir), pathlib.Path(drive_root)
    catalog = make_catalog(
        [
            BuiltinToolProvider(),
            SkillToolProvider(drive_root=drive_root, repo_dir=repo_dir),
            ExtensionToolProvider(drive_root, repo_dir),
            McpToolProvider(),
            A2aToolProvider(a2a_manager, get_a2a_tool_timeout_sec()),
        ]
    )
    return catalog, ToolContext(repo_dir=repo_dir, drive_root=drive_root)
