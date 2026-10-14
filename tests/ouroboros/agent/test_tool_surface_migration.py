"""The prompt surface the `prepare_tools` node builds, and the boundaries around it.

Was a characterization test comparing the node against `ouroboros.tools.selection`. That
module is gone, so the comparison went with it; what remains are the behaviours it was
pinning — the heal-mode set, the attachment rule, the legacy env promotion — asserted
directly, plus the structural guards that keep the retired path from coming back.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from ouroboros.agent.task_graph import bind_tool_surface
from tests.helpers.shared import make_tool_env

_PRODUCTION_ROOT = pathlib.Path("ouroboros")

#: Everything that ships or is executed. Scoping the import guard to `ouroboros/` alone is
#: what let three `ouroboros.tools.registry` imports survive in `tests_headless/` — the
#: suite went red on a path the guard could not see.
_PYTHON_ROOTS = (
    pathlib.Path("ouroboros"),
    pathlib.Path("new_core"),
    pathlib.Path("supervisor"),
    pathlib.Path("tests"),
    pathlib.Path("tests_headless"),
    pathlib.Path("scripts"),
)

#: Docs and prompts that tell a human or the agent which module to import. A stale line
#: here is worse than a stale comment: a skill written to it fails to load, and the loader
#: swallows the ImportError.
_AUTHORING_DOCS = (
    pathlib.Path("docs"),
    pathlib.Path("prompts"),
    pathlib.Path("AGENTS.md"),
)

#: Retired from the live path. Named here so a reintroduction is a visible edit.
#: `ouroboros.tools.registry` is retired but not deleted — it survives as a deprecation
#: shim for skills written against the old import path (see `test_tool_import_shims.py`).
#: Nothing in the tree may import it either way; the shims' own test is the one exception.
_RETIRED_MODULES = {
    "ouroboros.tools.registry",
    "ouroboros.tools.selection",
    "ouroboros.policy.tool_policy",
}

#: Deleted outright, so the file must not be back either.
_DELETED_MODULES = _RETIRED_MODULES - {"ouroboros.tools.registry"}

#: The one file allowed to import the shim: it asserts the deprecation rather than using it.
_SHIM_TEST = pathlib.Path("tests/ouroboros/tools/test_tool_import_shims.py")


def _production_files() -> list[pathlib.Path]:
    return sorted(p for p in _PRODUCTION_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _all_python_files() -> list[pathlib.Path]:
    return sorted(
        p for root in _PYTHON_ROOTS if root.exists() for p in root.rglob("*.py") if "__pycache__" not in p.parts
    )


def _authoring_docs() -> list[pathlib.Path]:
    found: list[pathlib.Path] = []
    for target in _AUTHORING_DOCS:
        if target.is_dir():
            found.extend(target.rglob("*.md"))
        elif target.exists():
            found.append(target)
    return sorted(found)


@pytest.fixture
def _clean_env(monkeypatch):
    monkeypatch.delenv("TOOL_CALCULATOR_ENABLED", raising=False)
    monkeypatch.delenv("OUROBOROS_ENABLED_TOOLS", raising=False)


def _settings_with(**overrides):
    from ouroboros.storage.settings.store import load_settings as real_load

    real = real_load()

    class _Settings:
        def __getattr__(self, item):
            if item in overrides:
                return overrides[item]
            return getattr(real, item)

        def get(self, key, default=None):
            if key in overrides:
                return overrides[key]
            return real.get(key, default)

    return lambda: _Settings()


# --- what the node selects --------------------------------------------------------


def test_the_first_request_carries_the_core_surface(tmp_path, _clean_env) -> None:
    from ouroboros.tools.catalog import core_tool_names

    catalog, ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")

    schemas, active = bind_tool_surface(catalog, ctx, [])

    assert schemas, "expected a non-empty first-request surface"
    assert active == {s.name for s in schemas}
    # Core tools are the floor; nothing discoverable rides along unasked.
    assert active <= set(core_tool_names())
    assert "calculator" not in active


def test_a_legacy_env_flag_promotes_a_discoverable_tool(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OUROBOROS_ENABLED_TOOLS", raising=False)
    monkeypatch.setattr(
        "ouroboros.storage.settings.store.load_settings",
        _settings_with(TOOL_CALCULATOR_ENABLED="true"),
    )
    catalog, ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")

    _schemas, active = bind_tool_surface(catalog, ctx, [])

    assert "calculator" in active


def test_an_image_attachment_adds_vlm_query(tmp_path, _clean_env) -> None:
    catalog, ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")
    ctx.attachments = [{"kind": "image", "mime": "image/png"}]

    _schemas, active = bind_tool_surface(catalog, ctx, [])

    assert "vlm_query" in active


def test_without_an_attachment_vlm_query_stays_off(tmp_path, _clean_env) -> None:
    catalog, ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")

    _schemas, active = bind_tool_surface(catalog, ctx, [])

    assert "vlm_query" not in active


# --- the retired path stays retired -----------------------------------------------


def test_no_module_imports_the_retired_registry_or_selection() -> None:
    """Covers tests too, not just `ouroboros/` — an unmigrated test file is a red suite."""
    offenders: list[tuple[str, str]] = []
    for path in _all_python_files():
        if path == _SHIM_TEST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in _RETIRED_MODULES:
                offenders.append((str(path), node.module))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in _RETIRED_MODULES:
                        offenders.append((str(path), alias.name))
    assert not offenders, f"retired modules are back on the live path: {offenders}"


def test_no_doc_or_prompt_tells_an_author_to_import_a_retired_module() -> None:
    """A skill written to a stale doc fails to import, and the loader logs it at warning.

    `SkillToolProvider.refresh` catches the ImportError per skill, so the only symptom is
    that the skill's tools quietly never appear. The doc is the contract; pin it.
    """
    offenders: list[tuple[str, int, str]] = []
    for path in _authoring_docs():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for module in _RETIRED_MODULES:
                if module in line or module.replace(".", "/") + ".py" in line:
                    offenders.append((str(path), lineno, module))
    assert not offenders, f"docs still name retired modules: {offenders}"


def test_the_retired_modules_are_gone_from_the_tree() -> None:
    for module in _DELETED_MODULES:
        assert not pathlib.Path(module.replace(".", "/") + ".py").exists(), module


def test_the_old_import_paths_grow_no_types_of_their_own() -> None:
    """The two paths are back on purpose, and must stay shims rather than grow storage again.

    A skill's `tool.py` written against either would otherwise fail to import, and
    `SkillToolProvider.refresh` swallows that per skill — the skill's tools just never appear.
    That reaching for a moved name warns and resolves to the new-core type is a behaviour,
    owned by `test_tool_import_shims.py`; this guards only the shape, via the AST: no types of
    their own, so the shim cannot regrow the retired storage.
    """
    for name in ("ouroboros/tools/registry.py", "ouroboros/tools/__init__.py"):
        path = pathlib.Path(name)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=name)
        defined = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
        assert not defined, f"{name} must define no types of its own: {sorted(defined)}"


def test_no_module_names_the_registry_at_all() -> None:
    offenders: list[tuple[str, int]] = []
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "ToolRegistry":
                offenders.append((str(path), node.lineno))
            elif isinstance(node, ast.Attribute) and node.attr == "ToolRegistry":
                offenders.append((str(path), node.lineno))
    assert not offenders, f"ToolRegistry is back: {offenders}"


def test_the_standalone_loop_entry_point_is_gone() -> None:
    import importlib.util

    assert importlib.util.find_spec("ouroboros.agent.loop") is None, (
        "the legacy loop module was dissolved into the adapters; handle_task drives the task graph directly"
    )


def test_the_agent_owns_no_tool_storage() -> None:
    """Neither a registry nor a catalog: tool wiring is per task, off the graph context."""
    from ouroboros.agent.core import OuroborosAgent

    assert not hasattr(OuroborosAgent, "tools")


def test_the_catalog_is_process_wide_and_reaches_nodes_through_an_interface() -> None:
    """One catalog per process, and behind `ToolRunner` — no node holds a concrete one.

    It used to ride on `GraphContext` so that the then-ouroboros `prepare_tools` could
    enumerate tools. That node is a `new_core/loop` node now and reaches the surface
    decision through `ToolRunner.bind_surface`, so the catalog went back to being an
    implementation detail of the adapter that holds it.
    """
    from new_core.loop.context import GraphContext
    from new_core.tools.catalog import ToolCatalog

    assert not hasattr(ToolCatalog, "standard"), "no per-task catalog factory"
    assert "tool_catalog" not in GraphContext.__dataclass_fields__
