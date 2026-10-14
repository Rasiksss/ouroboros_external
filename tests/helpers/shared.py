"""Shared helpers for the Ouroboros test suite.

These functions are reused across multiple ``tests/test_*.py`` modules to
avoid duplicated boilerplate (extension-loader cleanup, claude_agent_sdk
mock installation). They are intentionally plain module-level callables,
not fixtures — many callers need them at module import time.
"""

from __future__ import annotations

import json
import pathlib
import sys
import types
from unittest.mock import MagicMock

from background_memory.interfaces import IForegroundPressure
from new_core.identity import OwnerScope
from new_core.llm.types import ToolCall, ToolCallFunction
from new_core.loop import CancellationSignal
from new_core.skills.models import SkillId
from new_core.skills.permissions import ISkillPermissionChecker
from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.runtimes import THREAD_RUNTIME
from new_core.tools.schema import ToolSchema
from ouroboros.storage.execution_memory.interfaces import IExecutionMemoryRepository
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.user_settings import StubMcpServerIdsReader

#: The scope every test context runs under unless it needs another one.
TEST_SCOPE = OwnerScope(owner_id="global", agent_id="main", surface_id="browser", chat_id="1")


def skill_test_settings():
    """Return the application settings used by skill test builders.

    Tests monkeypatch this function (not ``load_settings`` directly) because
    the loader no longer holds its own ``load_settings`` import.
    """
    from ouroboros.storage.settings.store import load_settings

    return load_settings()


def make_skill_availability(*, defaults=None, agents=None, executable_skills=True):
    """Build normalized availability without a production test constructor."""
    from new_core.skills.surface import SkillAvailability

    return SkillAvailability.from_values(
        defaults=defaults,
        agents=agents,
        executable_skills=executable_skills,
    )


def make_empty_skill_snapshot():
    """Build an initialized empty snapshot for tests outside the skill subsystem."""
    from new_core.skills.catalog import SkillCatalog

    catalog = SkillCatalog()
    catalog.initialize([])
    return catalog.snapshot()


def make_empty_extension_runtime():
    """Build the explicit no-extension runtime used outside extension tests."""
    from new_core.tools.providers import EmptyExtensionRuntime

    return EmptyExtensionRuntime()


class AllowAllSkillPermissionChecker(ISkillPermissionChecker):
    def refusal(self, skill_id: SkillId | str, permission: str) -> str | None:
        del skill_id, permission
        return None


def scope_with(scope: OwnerScope, **axes: str) -> OwnerScope:
    """A scope differing on the named axes. Re-validated, unlike ``model_copy``."""
    return OwnerScope(**{**scope.to_dict(), **axes})


def make_tool_ctx(**overrides) -> ToolContext:
    """A ``ToolContext`` with a scope already minted; ``scope=`` overrides it."""
    overrides.setdefault("scope", TEST_SCOPE)
    overrides.setdefault("skill_surface", None)
    return ToolContext(**overrides)


def make_skill_catalog(
    *,
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
    settings=None,
    distribution_root: pathlib.Path | None = None,
):
    """Build the one distribution catalog used by a test runtime."""
    from new_core.skills.catalog import SkillCatalog
    from new_core.skills.providers import DistributionSkillProvider
    from ouroboros.skills.loader import DistributionSkillLoader
    from ouroboros.storage.settings.schema import AppSettings

    raw_settings = settings if settings is not None else skill_test_settings()
    provider_settings = raw_settings if isinstance(raw_settings, (AppSettings, dict)) else AppSettings()
    resolved_distribution_root = distribution_root or pathlib.Path(repo_dir) / "skills"
    catalog = SkillCatalog()
    catalog.initialize(
        [
            DistributionSkillProvider(
                distribution_root=resolved_distribution_root,
                loader=DistributionSkillLoader(provider_settings),
            )
        ]
    )
    return catalog


def make_skill_surface(
    *,
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
    settings=None,
    distribution_root: pathlib.Path | None = None,
):
    """Build a distribution catalog and its deployment projection."""
    return make_skill_surface_factory(
        repo_dir=repo_dir,
        drive_root=drive_root,
        settings=settings,
        distribution_root=distribution_root,
    )


def make_skill_surface_factory(
    *,
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
    settings=None,
    distribution_root: pathlib.Path | None = None,
):
    """Build a fixed deployment projection over a test catalog."""
    from new_core.skills.surface import SkillSurface
    from ouroboros.skills.availability import build_skill_availability
    from ouroboros.storage.settings.schema import AppSettings

    raw_settings = settings if settings is not None else skill_test_settings()
    catalog = make_skill_catalog(
        repo_dir=repo_dir,
        drive_root=drive_root,
        settings=raw_settings,
        distribution_root=distribution_root,
    )
    availability = (
        build_skill_availability(raw_settings)
        if isinstance(raw_settings, AppSettings)
        else make_skill_availability(
            defaults=raw_settings.get("agents.defaults.skills"),
            agents=raw_settings.get("agents.list"),
            executable_skills=not bool(raw_settings.get("OUROBOROS_MULTIUSER")),
        )
    )
    return SkillSurface(snapshot=catalog.snapshot(), availability=availability)


def make_empty_skill_surface_factory(settings=None):
    """Build a fixed deployment projection with no providers or filesystem reads."""
    from new_core.skills.catalog import SkillCatalog
    from new_core.skills.surface import SkillSurface
    from ouroboros.skills.availability import build_skill_availability
    from ouroboros.storage.settings.schema import AppSettings

    catalog = SkillCatalog()
    catalog.initialize([])
    raw_settings = settings or {}
    availability = (
        build_skill_availability(raw_settings)
        if isinstance(raw_settings, AppSettings)
        else make_skill_availability(
            defaults=raw_settings.get("agents.defaults.skills"),
            agents=raw_settings.get("agents.list"),
            executable_skills=not bool(raw_settings.get("OUROBOROS_MULTIUSER")),
        )
    )
    return SkillSurface(snapshot=catalog.snapshot(), availability=availability)


def make_empty_skill_surface(settings=None):
    """Build an initialized surface with no providers or filesystem reads."""
    return make_empty_skill_surface_factory(settings)


def discover_test_skills(
    drive_root: pathlib.Path,
    repo_path: str | None = None,
    *,
    workspace_root: pathlib.Path | None = None,
    agent_name: str | None = None,
):
    """Read skills through a fresh catalog while tests migrate off loader APIs."""
    distribution_root = pathlib.Path(repo_path) if repo_path is not None else None
    repo_dir = pathlib.Path(workspace_root) if workspace_root is not None else pathlib.Path.cwd()
    if distribution_root is not None and workspace_root is None:
        repo_dir = distribution_root.parent
    surface = make_skill_surface(
        repo_dir=repo_dir,
        drive_root=pathlib.Path(drive_root),
        distribution_root=distribution_root,
    )
    return list(surface.skills(agent_name=agent_name))


def find_test_skill(
    drive_root: pathlib.Path,
    name: str,
    *,
    repo_path: str | None = None,
    workspace_root: pathlib.Path | None = None,
    agent_name: str | None = None,
):
    """Resolve one test skill through the same surface as runtime consumers."""
    skills = discover_test_skills(
        drive_root,
        repo_path,
        workspace_root=workspace_root,
        agent_name=agent_name,
    )
    return next((skill for skill in skills if skill.name == str(name or "").strip()), None)


class FakeCancellation(CancellationSignal):
    """A stop signal a test drives by hand; production has no never-stopping signal."""

    def __init__(self, stopped: bool = False) -> None:
        self.stopped = stopped

    def is_stopped(self) -> bool:
        return self.stopped

    def request_stop(self) -> None:
        self.stopped = True


class FakeForegroundPressure(IForegroundPressure):
    """Foreground pressure a test sets by hand."""

    def __init__(self, pending: bool = False) -> None:
        self.pending = pending

    async def is_pending(self) -> bool:
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


def write_extension_skill(skills_root, name, plugin_body) -> pathlib.Path:
    """Write a minimal ``type: extension`` skill whose ``plugin.py`` is ``plugin_body``."""
    skill_dir = pathlib.Path(skills_root) / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            f"name: {name}\n"
            "description: test extension.\n"
            "version: 0.1.0\n"
            "type: extension\n"
            "entry: plugin.py\n"
            f"permissions: {json.dumps(['tool'])}\n"
            "env_from_settings: []\n"
            "---\n"
            "body\n"
        ),
        encoding="utf-8",
    )
    (skill_dir / "plugin.py").write_text(plugin_body, encoding="utf-8")
    return skill_dir


def load_extension_skill(tmp_path, name, plugin_body):
    """Write, enable, and load an extension under ``tmp_path/skills``.

    Returns ``(drive_root, workspace_root)``; the provider takes the workspace root and
    looks for skills in ``<workspace>/skills``. Skills under that root are
    distribution-owned, so the tools they register carry ``distribution_owned=True``.
    """
    from ouroboros.skills.extensions import loader as extension_loader

    skills_root = tmp_path / "skills"
    drive_root = tmp_path / "drive"
    drive_root.mkdir(parents=True, exist_ok=True)
    write_extension_skill(skills_root, name, plugin_body)
    loaded = find_test_skill(drive_root, name, repo_path=str(skills_root))
    assert loaded is not None
    loaded = find_test_skill(drive_root, name, repo_path=str(skills_root))
    assert loaded is not None
    assert extension_loader.load_extension(loaded, dict, drive_root=drive_root) is None
    return drive_root, tmp_path


METADATA_EXTENSION = "metadata-ext"
METADATA_EXTENSION_TOOL = "search"
METADATA_EXTENSION_ALIASES = ("fixture_search", "search", "поиск")
METADATA_EXTENSION_RESULT_LIMIT = 30_000
METADATA_EXTENSION_TIMEOUT_SEC = 120

_METADATA_EXTENSION_PLUGIN = (
    "def _search(ctx, *, query=''):\n"
    "    return query\n\n"
    "def register(api):\n"
    "    api.register_tool(\n"
    f"        {METADATA_EXTENSION_TOOL!r},\n"
    "        _search,\n"
    "        description='fixture search tool',\n"
    "        schema={'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': []},\n"
    f"        timeout_sec={METADATA_EXTENSION_TIMEOUT_SEC},\n"
    f"        result_limit={METADATA_EXTENSION_RESULT_LIMIT},\n"
    f"        retrieval_aliases={METADATA_EXTENSION_ALIASES!r},\n"
    "        safety_policy='skip',\n"
    "        parallel_safe=True,\n"
    "    )\n"
)


def load_metadata_extension(tmp_path):
    """Load an extension that declares every per-tool runtime metadata field.

    Returns ``(surface_name, drive_root, catalog)`` so a test can resolve the tool the way
    the loop does. Unload with ``extension_loader.unload_extension(METADATA_EXTENSION)``.
    """
    from new_core.tools.catalog import ToolCatalog
    from new_core.tools.providers import ExtensionToolProvider
    from ouroboros.skills.extensions import loader as extension_loader
    from ouroboros.skills.extensions.runtime import ExtensionRuntime

    drive_root, workspace_root = load_extension_skill(tmp_path, METADATA_EXTENSION, _METADATA_EXTENSION_PLUGIN)
    surface_name = extension_loader.extension_surface_name(METADATA_EXTENSION, METADATA_EXTENSION_TOOL)
    catalog = ToolCatalog()
    skill_surface = make_skill_surface_factory(repo_dir=workspace_root, drive_root=drive_root)
    catalog.initialize(
        [ExtensionToolProvider(skill_surface=skill_surface, extension_runtime=ExtensionRuntime())],
    )
    return surface_name, drive_root, catalog


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


def static_tool_provider(*entries, origin: str = "builtin"):
    """A provider over ad-hoc ``ToolEntry`` objects.

    Replaces the removed ``ToolRegistry.register()`` for tests that need one made-up
    tool on the surface: append it to a registry's ``providers`` list, or pass it to
    ``handle_tool_calls`` / ``run_tool`` directly. ``origin`` is what a policy keyed on
    descriptor origin sees.
    """
    from new_core.tools.interfaces import IToolProvider
    from new_core.tools.providers.builtin import descriptor_from_entry

    class _StaticToolProvider(IToolProvider):
        default_runtime = THREAD_RUNTIME

        def __init__(self, items):
            self._entries = {entry.name: entry for entry in items}
            self._descriptors = {name: descriptor_from_entry(entry, origin) for name, entry in self._entries.items()}

        def refresh(self):
            return

        def descriptors(self, scope):
            return list(self._descriptors.values())

        def get(self, name, scope):
            return self._descriptors.get(name)

        def refuse_dispatch(self, name, scope):
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
            self.owner_questions: list[str] = []
            self.owner_requests: list[Any] = []

        def progress(self, text):
            self.progress_calls.append(text)

        def typing(self):
            return None

        def ask_owner(self, text, *, request) -> bool:
            self.owner_questions.append(text)
            self.owner_requests.append(request)
            return True

        def task_started(self):
            return None

        def emit_tool_live_log(self, payload):
            self.log_payloads.append(payload)

        def live_logs(self, event_type: str) -> list[dict[str, Any]]:
            return [payload for payload in self.log_payloads if payload.get("type") == event_type]

        def usage(self, model, usage, kind):
            return None

        def maybe_checkpoint(self, messages, *, round_idx, max_rounds, accumulated_usage):
            return False

        def emit_result(self, *, text, usage, trace, tool_ctx):
            return None

    return _CollectingEventSink()


def make_tool_entry(name: str, handler) -> ToolEntry:
    return ToolEntry(name=name, schema=ToolSchema(name=name, description="d", parameters={}), handler=handler)


def make_tool_call(name: str, args: dict | None = None, idx: int = 0) -> ToolCall:
    return ToolCall(id=f"call-{idx}", function=ToolCallFunction(name=name, arguments=json.dumps(args or {})))


def make_static_tool_env(tmp_path, *entries):
    return make_catalog([static_tool_provider(*entries)]), make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path)


def execute_tool_round(catalog, ctx, tmp_path, calls, events, **kwargs):
    from ouroboros.agent.loop_adapters import build_tool_call_executor

    executor = build_tool_call_executor(
        catalog=catalog,
        events=events,
        repeat_guard=kwargs.pop("repeat_guard", None),
        confirmation_gate=kwargs.pop("confirmation_gate", False),
        execution_memory_repository=kwargs.pop("execution_memory_repository", None)
        or sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    kwargs.setdefault("decisions", {})
    return executor.execute(calls, ctx=ctx, task_id="task1", **kwargs)


def decision_for(name: str, args: dict | None = None, *, idx: int = 0, verdict: str = "approved") -> dict:
    from ouroboros.agent.task_ledger import safe_args_hash

    return {(idx, name, safe_args_hash(args or {})): verdict}


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
    execution_memory_repository: IExecutionMemoryRepository | None = None,
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
        events=events if events is not None else collecting_event_sink(),
        confirmation_gate=False,
        task_ledger=task_ledger,
        repeat_guard=repeat_guard,
        execution_memory_repository=execution_memory_repository
        if execution_memory_repository is not None
        else sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    results = executor.execute(
        tool_calls,
        ctx=ctx,
        task_id=task_id,
        active_tool_names=active_tool_names,
        decisions={},
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

    from new_core.tools.interfaces import ToolResult
    from ouroboros.tools.dispatch import execute_tool

    descriptor = catalog.find(name, ctx.scope)
    origin = descriptor.origin if descriptor is not None else ""
    result = asyncio.run(execute_tool(catalog, ctx, name, dict(args or {}), origin=origin))
    return result.text if isinstance(result, ToolResult) else result


def make_tool_env(repo_dir, drive_root):
    """The catalog and the context a tool test needs, as production builds them.

    The provider list mirrors the one the server lifespan installs, over the test's own
    roots. Returns the pair rather than an object that owns both: bundling them behind one
    ``execute`` is what ``ToolRegistry`` did, and keeping a per-task context on a
    long-lived object is exactly why it was retired. Tests call
    ``run_tool(catalog, ctx, ...)``, which bridges to the coroutine production awaits.
    """
    import pathlib

    from new_core.skills.surface import SkillSurface
    from new_core.tools.providers import (
        A2aToolProvider,
        BuiltinToolProvider,
        ExtensionToolProvider,
        McpToolProvider,
        SkillToolProvider,
    )
    from ouroboros.app.config import get_a2a_tool_timeout_sec
    from ouroboros.egress.a2a.manager import a2a_manager
    from ouroboros.skills.availability import build_skill_availability
    from ouroboros.skills.extensions.runtime import ExtensionRuntime
    from ouroboros.storage.settings.schema import AppSettings
    from tests.helpers.egress_auth import a2a_egress_dependencies, null_egress_auth_dependencies

    repo_dir, drive_root = pathlib.Path(repo_dir), pathlib.Path(drive_root)
    skill_catalog = make_skill_catalog(repo_dir=repo_dir, drive_root=drive_root)
    raw_settings = skill_test_settings()
    availability = (
        build_skill_availability(raw_settings)
        if isinstance(raw_settings, AppSettings)
        else make_skill_availability(
            defaults=raw_settings.get("agents.defaults.skills"),
            agents=raw_settings.get("agents.list"),
            executable_skills=not bool(raw_settings.get("OUROBOROS_MULTIUSER")),
        )
    )
    skill_surface = SkillSurface(snapshot=skill_catalog.snapshot(), availability=availability)
    catalog = make_catalog(
        [
            BuiltinToolProvider(),
            SkillToolProvider(skill_surface=skill_surface),
            ExtensionToolProvider(
                skill_surface=skill_surface,
                extension_runtime=ExtensionRuntime(),
            ),
            McpToolProvider(
                servers=(),
                server_ids_reader=StubMcpServerIdsReader(),
                auth_dependencies=null_egress_auth_dependencies(),
            ),
            A2aToolProvider(
                a2a_manager,
                get_a2a_tool_timeout_sec(),
                agents=None,
                loop=None,
                dependencies=a2a_egress_dependencies(null_egress_auth_dependencies()),
            ),
        ]
    )
    return catalog, make_tool_ctx(repo_dir=repo_dir, drive_root=drive_root, skill_surface=skill_surface)
