"""No module of the package blocks the loop it runs on — parsed, not reviewed.

**What it catches and what it does not.** It reads the package's own syntax, so it catches a
blocking call written *at the call site* inside a coroutine: a sleep, an unawaited wait on a
threading primitive, a synchronous `with engine.connect()`, a call to one of the synchronous
doors named below. It does not follow calls: a coroutine that calls a helper of this package
which calls another that blocks several frames deep passes, and so does anything blocking that
reaches the loop through a module outside this package. Those are what the rule in
`openspec/specs/background-memory-events` states and what a reviewer reads for; this guard is
what stops the forms that actually appeared here from coming back unnoticed.
"""

from __future__ import annotations

import ast
import pathlib

_PKG_DIR = pathlib.Path(__file__).resolve().parents[2] / "background_memory"

#: Awaiting one of these is the point; calling one is the defect.
_BLOCKING_METHODS = frozenset({"wait", "acquire", "result"})

#: Synchronous doors into the database, the model and the observability store. The package
#: reaches each of them through a seam of its own instead.
_SYNC_DOORS = frozenset(
    {
        "chat_with_fallback",
        "create_engine",
        "record_event",
        "resolve_runtime_engine",
        "runtime_persistence",
        "urlopen",
    }
)

#: The settings reader's synchronous accessors, named after the keys they resolve. The package
#: holds the same object the runtime's worker threads do and must reach it through the `async_`
#: twins; calling the synchronous one reads the database on the loop.
_SYNC_SETTINGS_ACCESSOR_PREFIX = "OUROBOROS_"

#: A connection opened in a plain `with` is a synchronous connection.
_CONNECTION_OPENERS = frozenset({"begin", "connect"})

#: The asynchronous client, named by the one module that reads a broker with it. The
#: synchronous one is named by nothing here: what the publisher needs of it arrives through
#: `infra/kafka_client.py`, which is the deployment's one description of the cluster.
_ASYNCHRONOUS_BROKER_READER = {"infra/events/kafka_transport.py"}


def _module_files() -> list[pathlib.Path]:
    return sorted(path for path in _PKG_DIR.rglob("*.py") if "__pycache__" not in path.parts)


def _named(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_named(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return _named(node.func)
    return ""


def _awaited(function: ast.AST) -> set[int]:
    """Every node that sits under an ``await`` — directly or as an argument of one."""
    inside: set[int] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Await):
            inside.update(id(child) for child in ast.walk(node))
    return inside


def _offences(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    where = str(path.relative_to(_PKG_DIR))
    found: list[str] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.AsyncFunctionDef):
            continue
        awaited = _awaited(function)
        for node in ast.walk(function):
            if isinstance(node, ast.With):
                for item in node.items:
                    if isinstance(item.context_expr, ast.Call):
                        name = _named(item.context_expr)
                        if name.rsplit(".", 1)[-1] in _CONNECTION_OPENERS:
                            found.append(f"{where}:{node.lineno} {function.name} opens `with {name}()`")
                continue
            if not isinstance(node, ast.Call) or id(node) in awaited:
                continue
            name = _named(node)
            last = name.rsplit(".", 1)[-1]
            if name == "time.sleep":
                found.append(f"{where}:{node.lineno} {function.name} sleeps")
            elif last in _BLOCKING_METHODS:
                found.append(f"{where}:{node.lineno} {function.name} calls `{name}()` without awaiting it")
            elif last in _SYNC_DOORS or name.split(".", 1)[0] in _SYNC_DOORS:
                found.append(f"{where}:{node.lineno} {function.name} calls `{name}()`")
            elif last.startswith(_SYNC_SETTINGS_ACCESSOR_PREFIX):
                found.append(f"{where}:{node.lineno} {function.name} calls the synchronous accessor `{name}()`")
    return found


def test_no_coroutine_of_the_package_blocks_the_loop_it_runs_on() -> None:
    offences = [offence for path in _module_files() for offence in _offences(path)]

    assert not offences, "background memory coroutines must not block the loop:\n" + "\n".join(offences)


def _modules_importing(root: str) -> set[str]:
    naming: set[str] = set()
    for path in _module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom) and node.level == 0
                else []
            )
            if any(name == root or name.startswith(f"{root}.") for name in modules):
                naming.add(str(path.relative_to(_PKG_DIR)))
    return naming


def test_no_module_of_the_package_names_the_synchronous_kafka_client() -> None:
    """What the publisher needs of it comes through the one description of the cluster."""
    assert _modules_importing("kafka") == set()


def test_the_asynchronous_client_is_named_by_the_one_module_that_reads_a_broker() -> None:
    assert _modules_importing("aiokafka") == _ASYNCHRONOUS_BROKER_READER


def test_the_tool_body_placement_is_named_where_it_is_used() -> None:
    """Which module may own an executor is `test_import_boundary.py`'s; this is where it is."""
    placing = {
        str(path.relative_to(_PKG_DIR))
        for path in _module_files()
        if "ThreadPoolExecutor" in path.read_text(encoding="utf-8")
    }

    assert placing == {"handlers/distillation.py"}
