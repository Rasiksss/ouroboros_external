"""No path that builds prompt content or an owner-facing report reads across owners.

The store takes a scope on every task read. A caller that omits it reads every owner's
rows, and the widening is invisible at the call site — which is exactly how one owner's
task descriptions and results reached another owner's model prompt. This pins the shape
rather than one instance of it, because the omission is a missing argument and comes back
as easily as it went away.
"""

from __future__ import annotations

import ast
import pathlib

_ROOTS = ("ouroboros", "supervisor", "transport", "background_memory", "new_core")
_SCOPED_READS = ("list_tasks", "list_task_results")

#: Readers that deliberately cross owners, each with the reason it may. None of them feeds
#: a prompt or an owner-facing report: they act on the deployment, not for anybody.
_CROSS_OWNER_READERS: dict[str, str] = {
    "supervisor/queue.py:_allocate_priority": "the queue's own ordering, over every owner's backlog",
    "supervisor/queue.py:queue_has_task_type": "asks whether the instance has such work at all",
    "supervisor/queue.py:release_own_runs": "this instance's held runs, narrowed by runtime_id",
    "ouroboros/agent_runtime/executor.py:__init__": "retires this instance's leftover rows of a type nothing runs",
    "transport/a2a/task_store.py:list": "A2A protocol rows, which carry no owner scope",
    "transport/a2a/task_store.py:cleanup_expired": "A2A protocol rows, which carry no owner scope",
    "transport/a2a/task_store.py:fail_orphaned_tasks": "A2A protocol rows, which carry no owner scope",
    "background_memory/infra/foreground.py:is_pending": "instance-wide back-pressure, a count of nobody's",
    "ouroboros/agent/startup_checks.py:check_review_continuations": "a boot diagnostic about this process's leftovers",
}


def _qualified_readers() -> list[tuple[str, bool]]:
    """Every scoped-read call site as ``file:function``, and whether it passes a scope."""
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    found: list[tuple[str, bool]] = []
    for root in _ROOTS:
        base = repo_root / root
        if not base.exists():
            continue
        for source_file in base.rglob("*.py"):
            tree = ast.parse(source_file.read_text(encoding="utf-8"))
            enclosing: dict[int, str] = {}
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                        enclosing.setdefault(line, node.name)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name not in _SCOPED_READS:
                    continue
                where = f"{source_file.relative_to(repo_root)}:{enclosing.get(node.lineno, '<module>')}"
                found.append((where, any(keyword.arg == "scope" for keyword in node.keywords)))
    return found


def test_every_task_read_carries_a_scope_or_is_a_named_exception() -> None:
    offenders = [where for where, scoped in _qualified_readers() if not scoped and where not in _CROSS_OWNER_READERS]
    assert offenders == [], (
        "these read every owner's tasks without saying so; pass the scope the caller holds, "
        f"or name the reader in _CROSS_OWNER_READERS with its reason: {offenders}"
    )


def test_the_named_exceptions_are_all_still_there() -> None:
    """An allowlist nobody prunes stops being a list of decisions."""
    unscoped = {where for where, scoped in _qualified_readers() if not scoped}
    stale = sorted(set(_CROSS_OWNER_READERS) - unscoped)

    assert stale == [], f"these readers no longer read across owners; drop them from the list: {stale}"
