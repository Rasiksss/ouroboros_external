"""The split, stated as an import graph and as what each side's loop does.

While the supervisor imports the agent, "the supervisor does not know how tasks are
executed" is a sentence about intent. These are the form of the claim that can be checked.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

#: Import roots a supervisor module may not name, statically or lazily. ``ouroboros.agent``
#: covers every module that constructs an agent; ``ouroboros.agent_runtime`` covers the
#: component that runs one.
_FORBIDDEN_TO_SUPERVISOR = (("ouroboros", "agent"), ("ouroboros", "agent_runtime"))

#: What the runtime may not name back: the supervisor's loop and its event handlers. Its
#: durable store and the queue it claims from are exactly what it is allowed to name.
_FORBIDDEN_TO_RUNTIME = ("supervisor.runtime", "supervisor.events", "supervisor.commands")


def _imports(path: pathlib.Path) -> list[tuple[str, int]]:
    """Every module named by an import in ``path``, including inside a function body."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found += [(alias.name, node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.module, node.lineno))
    return found


def _modules(package: str) -> list[pathlib.Path]:
    return sorted(p for p in pathlib.Path(package).rglob("*.py") if "__pycache__" not in p.parts)


def test_the_supervisors_imports_name_no_executor_and_no_agent() -> None:
    offenders = [
        f"{path}:{line} imports {module}"
        for path in _modules("supervisor")
        for module, line in _imports(path)
        if tuple(module.split(".")[:2]) in _FORBIDDEN_TO_SUPERVISOR
    ]

    assert offenders == [], "the supervisor may not name what runs tasks, statically or lazily"


def test_the_supervisors_broker_composition_names_no_agent_runtime() -> None:
    path = pathlib.Path("ouroboros/app/broker_composition.py")
    offenders = [
        f"{path}:{line} imports {module}"
        for module, line in _imports(path)
        if tuple(module.split(".")[:2]) in _FORBIDDEN_TO_SUPERVISOR
    ]

    assert offenders == [], "the supervisor imports this shared composition module"


def test_the_runtime_does_not_import_the_supervisors_loop_or_handlers() -> None:
    named = {module for path in _modules("ouroboros/agent_runtime") for module, _line in _imports(path)}

    assert not named & set(_FORBIDDEN_TO_RUNTIME)
    # What it is allowed to name, and does: the queue it claims from.
    assert "supervisor.queue" in named


def test_the_supervisor_holds_no_reference_to_what_runs_tasks() -> None:
    from supervisor.runtime import SupervisorContext
    from supervisor.runtime_context import SupervisorRuntime

    for holder in (SupervisorRuntime, SupervisorContext):
        fields = holder.__dataclass_fields__
        assert "executor" not in fields
        # Nor the identity of whoever runs the task: the record names the runtime.
        assert "runtime_id" not in fields


def test_the_supervisor_consumes_reports_and_cannot_publish_them() -> None:
    from ouroboros.agent_events.interfaces import IAgentEventConsumer
    from supervisor.runtime_context import SupervisorRuntime

    annotation = SupervisorRuntime.__dataclass_fields__["agent_events"].type

    assert annotation in (IAgentEventConsumer, "IAgentEventConsumer")


def test_agent_runtime_claim_queue_uses_owner_running_limit(tmp_path) -> None:
    from ouroboros.storage.runtime_persistence import runtime_persistence
    from tests.helpers.supervisor import bootstrap_agent_runtime_for_tests

    runtime_persistence.initialize()
    parts = bootstrap_agent_runtime_for_tests(tmp_path, max_running_root_jobs_per_owner=7)
    queue = parts.runtime._executor.task_queue

    assert queue.max_running_root_jobs_per_owner == 7


@pytest.mark.parametrize("attribute", ["set_media_senders", "_media_senders"])
def test_the_loose_media_senders_are_gone(attribute: str) -> None:
    from ouroboros.agent_runtime.agent_factory import AgentFactory
    from ouroboros.agent_runtime.executor import TaskExecutor

    assert not hasattr(TaskExecutor, attribute)
    assert not hasattr(AgentFactory, attribute)


def _names(path: pathlib.Path) -> set[str]:
    """Every bare name and imported symbol a module mentions."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.ImportFrom):
            found |= {alias.asname or alias.name for alias in node.names}
    return found


def test_only_the_composition_root_starts_both_halves() -> None:
    """One module starts the supervisor and the runtime, and only one.

    Naming the supervisor's queue is not naming the supervisor: the runtime claims from it,
    which is exactly the coupling that survives the two becoming separate processes. What
    must stay in one place is who *starts* each half.
    """
    starts_the_supervisor = {"run_supervisor", "build_supervisor_runtime"}
    starts_the_runtime = {"build_agent_runtime", "AgentRuntime"}
    offenders: list[str] = []
    scanned = (
        *_modules("ouroboros"),
        *_modules("supervisor"),
        *_modules("new_core"),
        *_modules("transport"),
        # The per-component launch scripts are composition roots of their own: each starts
        # the one half its process hosts, so the rule they must keep is the same one.
        *_modules("multiuser_config"),
    )
    for path in scanned:
        named = _names(path)
        if named & starts_the_supervisor and named & starts_the_runtime:
            offenders.append(str(path))

    root = _names(pathlib.Path("server.py"))
    assert root & starts_the_supervisor and root & starts_the_runtime
    assert offenders == []
