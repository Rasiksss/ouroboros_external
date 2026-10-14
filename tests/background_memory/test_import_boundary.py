"""``background_memory`` must import neither the supervisor nor the agent.

The dependency runs one way: the agent and the composition root reach into this
package, never back. Distillation used to cross both borders — the agent wrote a
task row through ``supervisor.queue``, the supervisor ran a body that lived in
``ouroboros.agent`` — and this test is what keeps either from coming back.

The check parses rather than imports. The handler legitimately imports
``ouroboros.tools.memory_tools`` and ``new_core.tools.context``, which pull
``ouroboros.agent`` in transitively; a ``sys.modules`` check would go red for a
rule that is not broken.
"""

from __future__ import annotations

import ast
import pathlib

import background_memory as background_memory_pkg

_PKG_DIR = pathlib.Path(background_memory_pkg.__file__).parent

#: Import roots the package may never name, in any module, at any nesting depth — except
#: for the modules named in `_ALLOWED_STORAGE`. `ouroboros.storage` is here because this
#: package owns its data access: every row it reads or writes goes through its own
#: repositories, and a module that reached the storage layer for anything but the pure
#: shapes below would be a second way to the database with none of their rules.
_FORBIDDEN_ROOTS = ("supervisor", "ouroboros.agent", "ouroboros.storage", "infra.runtime_database")

#: What the package may still name under `ouroboros.storage`, listed module by module rather
#: than by relaxing a prefix. Every entry is either pure — no I/O, no mutable process state —
#: or an owner decision, and each says which:
_ALLOWED_STORAGE = (
    # Owner decision 6 of 19.09.2026: field encryption is imported, not duplicated. It is also
    # the one module a second copy of would be a security defect rather than a second SSOT.
    "ouroboros.storage.message_field_crypto",
    # The shapes the rows are, and nothing else: pydantic models with no I/O. A copy here
    # would be a second description of the same columns.
    "ouroboros.storage.runtime_models",
    # One function that renders a stored message as a summary line. Pure, and shared with the
    # consolidation prompt precisely so a run and the rest of the system agree on the text.
    "ouroboros.storage.conversation_format",
    # The memory domain's own policy — which kinds an automated caller may write, which
    # statuses reach a prompt, how a manifest renders. Distillation acts on it; duplicating it
    # would be a second SSOT for what a memory may be.
    "ouroboros.storage.core_memory.memory.domain.models",
    "ouroboros.storage.core_memory.memory.domain.render",
    # The instance's own settings: thresholds, timeouts and token budgets the deployment sets.
    # `load_settings` reads a process-wide object built at start-up, not a file and not a
    # database — the *owner's* settings are a different question, resolved by
    # `config_registry.user_settings`' reader through its `async_` accessors. `AppSettings` is
    # the schema itself.
    "ouroboros.storage.settings.store",
    "ouroboros.storage.settings.schema",
)

#: The two modules under ``ouroboros.contracts`` this package may name, listed one by one
#: rather than by relaxing the prefix: the type-generic half of a seam codec (the
#: unknown-key filter, the declared-type check, the JSON-safety walk) and the shapes a Redis
#: stream reply comes back in. Both are shared with the other broker backends precisely so
#: there is one copy of each; neither names a payload type, mints a scope or reads a
#: setting, which is what separates them from their neighbours there.
_ALLOWED_CONTRACTS = ("ouroboros.contracts.wire", "ouroboros.contracts.stream_replies")

#: The runtime store, by every name a module could reach it under. The package owns its
#: data access: a module that named one of these would be a second way to the database with
#: none of the repositories' rules — synchronous, unscoped, and outside this package.
_STORE_NAMES = (
    "RuntimePersistence",
    "RuntimePersistenceProvider",
    "runtime_persistence",
    "ITaskRepository",
    "SqlTaskRepository",
)
_STORE_MODULES = ("ouroboros.storage.runtime_persistence", "ouroboros.storage.runtime_backend")

#: The composition seam that resolves a broker backend from settings. A transport that
#: could reach the resolver could reach a backend, which is the dependency this seam
#: exists to prevent — and a transport that read a setting could disagree with the root
#: that built it. ``tests/ouroboros/app/test_broker_composition.py`` makes the same check for
#: ``transport/``; this one is where an editor of *this* package meets it.
_RESOLVER = "ouroboros.app.broker_composition"


#: The package owns no thread. What is banned is *making* one, not naming the type of a stop
#: signal: the loop reads `threading.Event.is_set()` from a coroutine, which is legal and is a
#: recorded decision, and the in-process queue takes a `threading.Lock` because its publishing
#: half is called from whatever thread finished a user's work.
_THREAD_MAKERS = ("Thread", "ThreadPoolExecutor", "ProcessPoolExecutor", "to_thread", "run_in_executor")

#: The one sanctioned placement: a memory tool body is synchronous by nature and runs on an
#: executor its own call owns (`new_core/tools/runtimes.py`, lines 24-46). Allowed at exactly
#: the module that invokes those bodies, by name, and generalised nowhere.
_MAY_OWN_AN_EXECUTOR = {"handlers/distillation.py"}


def _thread_offences(source: str, where: str) -> list[str]:
    """Every call in `source` that would give this package a thread of its own."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source, filename=where)):
        if not isinstance(node, ast.Call):
            continue
        called = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if called not in _THREAD_MAKERS:
            continue
        if called == "ThreadPoolExecutor" and where in _MAY_OWN_AN_EXECUTOR:
            continue
        found.append(f"{where}:{node.lineno} {called}()")
    return found


def _module_files() -> list[pathlib.Path]:
    return sorted(p for p in _PKG_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _is_forbidden(name: str) -> bool:
    if name in _ALLOWED_STORAGE:
        return False
    return any(name == root or name.startswith(f"{root}.") for root in _FORBIDDEN_ROOTS)


def _forbidden_contracts(source: str, *, filename: str = "<test>") -> list[str]:
    """Every ``ouroboros.contracts`` module ``source`` names that is not on the allowlist."""
    named: list[str] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.Import):
            named.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            named.append(node.module or "")
    return [name for name in named if name.startswith("ouroboros.contracts") and name not in _ALLOWED_CONTRACTS]


def test_the_package_has_modules_to_check() -> None:
    assert _module_files(), "the boundary test found no modules — it would pass vacuously"


def test_every_handler_is_in_the_parsed_set() -> None:
    """The rule is package-wide, so a new handler is only covered once it is here."""
    checked = {str(path.relative_to(_PKG_DIR)) for path in _module_files()}

    assert {"handlers/distillation.py", "handlers/consolidation.py"} <= checked


def test_no_module_reaches_outside_the_boundary() -> None:
    """The supervisor, the agent and the storage layer, with the storage layer's allowlist."""
    offenders: list[tuple[str, str]] = []
    for path in _module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders.extend(
                    (str(path.relative_to(_PKG_DIR)), alias.name) for alias in node.names if _is_forbidden(alias.name)
                )
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and _is_forbidden(node.module or ""):
                offenders.append((str(path.relative_to(_PKG_DIR)), node.module or ""))
    assert not offenders, f"background_memory must not import these: {offenders}"


def test_the_storage_allowlist_is_named_module_by_module_and_lets_nothing_else_through() -> None:
    """A prefix would let the runtime store, the engine and the blob provider in with them."""
    assert not _is_forbidden("ouroboros.storage.message_field_crypto")
    assert not _is_forbidden("ouroboros.storage.runtime_models")
    assert _is_forbidden("ouroboros.storage.runtime_persistence")
    assert _is_forbidden("ouroboros.storage.runtime_engine")
    assert _is_forbidden("infra.runtime_database")
    assert _is_forbidden("ouroboros.storage.core_memory.memory.infra.repositories.repo")
    assert _is_forbidden("ouroboros.storage")


def test_no_module_imports_a_scope_mint() -> None:
    """Background memory has no service work: every event it handles has an author,
    so no module of the package may import the home of the scope mints.

    ``ouroboros.contracts.wire`` is the one exception, named module by module rather than
    by relaxing the prefix: it holds the type-generic half both seam codecs share and
    mints nothing. ``ouroboros.contracts.owner_scope`` — the mints themselves — stays
    forbidden along with everything else there.
    """
    offenders = [
        (str(path.relative_to(_PKG_DIR)), module)
        for path in _module_files()
        for module in _forbidden_contracts(path.read_text(encoding="utf-8"), filename=str(path))
    ]
    assert not offenders, f"background_memory must not import a scope mint: {offenders}"


def test_the_scope_mint_check_sees_both_spellings_and_lets_the_two_shared_modules_through() -> None:
    """The check is worth only the node kinds it walks and the names it lets past.

    ``import ouroboros.contracts.owner_scope`` reaches the mints exactly as the ``from``
    spelling does, and a check that saw one of them would pass on whichever nobody used.
    """
    assert _forbidden_contracts("import ouroboros.contracts.owner_scope") == ["ouroboros.contracts.owner_scope"]
    assert _forbidden_contracts("from ouroboros.contracts.owner_scope import service_identity") == [
        "ouroboros.contracts.owner_scope"
    ]
    assert _forbidden_contracts("from ouroboros.contracts.wire import check_json_safe") == []
    assert _forbidden_contracts("from ouroboros.contracts.stream_replies import row") == []


def test_no_module_imports_the_broker_resolver() -> None:
    """A transport that reads the selector can disagree with the root that composed it."""
    offenders: list[tuple[str, str]] = []
    for path in _module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders.extend(
                    (str(path.relative_to(_PKG_DIR)), alias.name)
                    for alias in node.names
                    if alias.name == _RESOLVER or alias.name.startswith(f"{_RESOLVER}.")
                )
            elif isinstance(node, ast.ImportFrom) and (node.module or "") == _RESOLVER:
                offenders.append((str(path.relative_to(_PKG_DIR)), node.module or ""))
    assert not offenders, f"background_memory must not import the broker resolver: {offenders}"


def test_no_module_reads_a_setting_for_its_transport() -> None:
    """The transports take their stream, group and caps as constructor arguments.

    A settings read inside a backend is how a transport ends up disagreeing with the root
    that built it; the handlers' own thresholds are a different question and keep theirs.
    """
    offenders: list[tuple[str, str]] = []
    for path in _module_files():
        if path.parent.name != "events":
            continue  # thresholds belong to the handlers; the seam's transports are here
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and "settings" in (node.module or ""):
                offenders.append((str(path.relative_to(_PKG_DIR)), node.module or ""))
    assert not offenders, f"a memory event transport must read no setting: {offenders}"


def test_no_module_names_the_runtime_store() -> None:
    """Imports and constructor arguments alike: the package reaches its own repositories."""
    offenders: list[tuple[str, str]] = []
    for path in _module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        where = str(path.relative_to(_PKG_DIR))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "") in _STORE_MODULES:
                offenders.append((where, node.module or ""))
            elif isinstance(node, ast.Import):
                offenders.extend((where, alias.name) for alias in node.names if alias.name in _STORE_MODULES)
            elif isinstance(node, ast.Name) and node.id in _STORE_NAMES:
                offenders.append((where, node.id))
            elif isinstance(node, ast.Attribute) and node.attr in _STORE_NAMES:
                offenders.append((where, node.attr))
    assert not offenders, f"background_memory reaches the database through its own repositories: {offenders}"


def test_no_module_of_the_package_makes_a_thread_of_its_own() -> None:
    """The goal the owner named: no thread, and the loop owns none either."""
    offences = [
        offence
        for path in _module_files()
        for offence in _thread_offences(path.read_text(encoding="utf-8"), str(path.relative_to(_PKG_DIR)))
    ]

    assert not offences, f"background_memory owns no thread: {offences}"


def test_the_per_call_executor_is_allowed_at_one_module_and_refused_at_every_other() -> None:
    """2.3: the exception is a name, not a pattern — the same line elsewhere fails."""
    source = "from concurrent.futures import ThreadPoolExecutor\nx = ThreadPoolExecutor(max_workers=1)\n"

    assert _thread_offences(source, "handlers/distillation.py") == []
    assert _thread_offences(source, "handlers/consolidation.py") == ["handlers/consolidation.py:2 ThreadPoolExecutor()"]
    assert _thread_offences("import threading\nthreading.Thread(target=f).start()\n", "runtime.py") == [
        "runtime.py:2 Thread()"
    ]
    assert _thread_offences("import asyncio\nawait asyncio.to_thread(f)\n", "handlers/distillation.py") == [
        "handlers/distillation.py:2 to_thread()"
    ]


def test_naming_a_stop_signal_or_locking_a_queue_is_not_making_a_thread() -> None:
    """Both are recorded decisions: the loop only reads the flag, and `publish` is synchronous."""
    assert _thread_offences("import threading\ne: threading.Event\ne.is_set()\n", "runtime.py") == []
    assert _thread_offences("import threading\nlock = threading.Lock()\n", "infra/events/inmemory.py") == []


def test_every_repository_is_handed_an_engine_it_does_not_resolve() -> None:
    """A repository that built its own engine would be a composition root of its own."""
    offenders: list[str] = []
    for path in sorted((_PKG_DIR / "infra" / "repositories").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for klass in [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]:
            init = next((n for n in klass.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"), None)
            if init is None:
                continue
            arguments = [argument.arg for argument in init.args.args]
            if arguments[1:2] != ["engine"] or init.args.defaults:
                offenders.append(f"{path.name}:{klass.name}")
    assert not offenders, f"these take no required engine argument: {offenders}"


def test_every_repository_method_is_a_coroutine() -> None:
    offenders: list[str] = []
    for path in sorted((_PKG_DIR / "infra" / "repositories").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for klass in [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]:
            offenders.extend(
                f"{path.name}:{klass.name}.{node.name}"
                for node in klass.body
                if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
            )
    assert not offenders, f"these repository methods are not coroutines: {offenders}"
