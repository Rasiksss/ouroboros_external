"""The scheduling package must stay importable from the supervisor tier."""

from __future__ import annotations

import ast
import inspect
import pathlib

_PACKAGE = pathlib.Path(__file__).resolve().parents[2] / "ouroboros" / "scheduling"
#: The supervisor tier's import ban, plus this package's own: no supervisor, no transport.
_FORBIDDEN = (
    "supervisor",
    "transport",
    "new_core.tools",
    "ouroboros.agent",
    "ouroboros.agent_runtime",
    "ouroboros.mcp",
)


def _imported_names(path: pathlib.Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.append(node.module or "")
    return names


def _methods(protocol: type) -> set[str]:
    return {name for name, value in vars(protocol).items() if not name.startswith("_") and callable(value)}


def test_the_two_store_surfaces_stay_apart():
    """Serving one owner's tool from a tick method would act on another owner's row."""
    from ouroboros.scheduling.store import DispatchStore, ScheduleStore

    scoped, dispatch = _methods(ScheduleStore), _methods(DispatchStore)
    assert scoped and dispatch and scoped.isdisjoint(dispatch)
    for name in scoped:
        assert "scope" in inspect.signature(getattr(ScheduleStore, name)).parameters, f"{name} takes no scope"
    for name in dispatch:
        assert "scope" not in inspect.signature(getattr(DispatchStore, name)).parameters, f"{name} takes a scope"


def test_the_service_and_the_dispatcher_each_take_only_their_own_surface():
    """The split is only worth its types if the two callers are actually typed by it."""
    from ouroboros.scheduling.dispatch import ScheduleDispatcher
    from ouroboros.scheduling.service import SchedulerService

    assert inspect.signature(SchedulerService.__init__).parameters["store"].annotation == "ScheduleStore"
    assert inspect.signature(ScheduleDispatcher.__init__).parameters["store"].annotation == "DispatchStore"


def test_scheduling_imports_stay_inside_the_boundary():
    """Static and lazy imports both walk the whole AST, so a function-level import counts."""
    assert _PACKAGE.is_dir()
    for path in sorted(_PACKAGE.glob("*.py")):
        for name in _imported_names(path):
            offside = any(name == banned or name.startswith(banned + ".") for banned in _FORBIDDEN)
            assert not offside, f"{path.name} imports {name}"


def test_the_nine_tools_are_actually_registered_not_only_declared():
    """The catalogue declares them; one line in the provider is what makes them exist at runtime."""
    from new_core.tools.providers.builtin import FROZEN_TOOL_MODULES
    from ouroboros.tools.scheduling import get_tools

    assert "scheduling" in FROZEN_TOOL_MODULES, "the scheduler tools would not be built into the catalogue"
    names = sorted(entry.name for entry in get_tools())
    assert names == [
        "cancel_schedule",
        "confirm_schedule",
        "create_reminder",
        "create_scheduled_task",
        "edit_schedule",
        "list_schedules",
        "move_schedule_here",
        "pause_schedule",
        "resume_schedule",
    ]


def test_every_tool_spells_a_refusal_the_way_the_loop_reads_failure():
    """Through the provider, because it is the provider that spells an escaping exception.

    A JSON body whose own field says "error" is recorded, logged and reflected on as a success:
    the loop classifies by prefix and never parses the payload.
    """
    from types import SimpleNamespace

    from new_core.identity import OwnerScope
    from new_core.tools.providers.builtin import BuiltinToolProvider
    from ouroboros.agent.loop_tool_execution import _FAILURE_PREFIXES
    from ouroboros.tools.scheduling import get_tools

    # A2A is a service surface: there is no interactive user chat to answer a schedule into.
    scope = OwnerScope(owner_id="owner-a", agent_id="main", surface_id="a2a", chat_id="100")
    ctx = SimpleNamespace(scope=scope, transport={})
    provider = BuiltinToolProvider(["scheduling"])
    provider.refresh()
    for entry in get_tools():
        answer = provider.execute(ctx, entry.name, {})
        assert answer.startswith("⚠️ TOOL_"), f"{entry.name} answered {answer[:80]}"
        assert _FAILURE_PREFIXES and answer.startswith(_FAILURE_PREFIXES), "the loop would read this as ok"
