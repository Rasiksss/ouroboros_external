"""The two deprecated import paths for the tool declaration types.

The registry was deleted, but the paths a `type: tool` skill's `tool.py` may still be written
against are not — `ouroboros.tools.registry` and the `ouroboros.tools` package itself.
Without the shims those imports raise, `SkillToolProvider.refresh` catches the error per
skill, and the skill's tools quietly never appear. These tests pin the two things that make
the shims worth having: the old import resolves to the new object, and reaching for it is
reported.

This is the one file allowed to import the retired path; the guard in
`test_tool_surface_migration.py` excludes it by name.
"""

from __future__ import annotations

import logging
import warnings

import pytest

import ouroboros.tools as tools_pkg
import ouroboros.tools.registry as registry_shim

#: (shim module, its logger name). Every behaviour below holds for both paths.
_SHIMS = [
    pytest.param(registry_shim, "ouroboros.tools.registry", id="registry"),
    pytest.param(tools_pkg, "ouroboros.tools", id="package"),
]


def _records(caplog, logger_name: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == logger_name]


@pytest.mark.parametrize(("shim", "logger_name"), _SHIMS)
def test_the_old_import_path_yields_the_new_core_types(shim, logger_name) -> None:
    """A skill written against the old path gets exactly the object it would have got."""
    del logger_name
    from new_core.tools.context import BrowserState, ToolContext
    from new_core.tools.entry import ToolEntry
    from new_core.tools.schema import ToolSchema

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert shim.ToolEntry is ToolEntry
        assert shim.ToolSchema is ToolSchema
        assert shim.ToolContext is ToolContext
        assert shim.BrowserState is BrowserState


@pytest.mark.parametrize(("shim", "logger_name"), _SHIMS)
def test_reaching_for_a_moved_type_warns(shim, logger_name) -> None:
    del logger_name
    with pytest.warns(DeprecationWarning, match=r"new_core\.tools"):
        _ = shim.ToolEntry


@pytest.mark.parametrize(("shim", "logger_name"), _SHIMS)
def test_reaching_for_a_moved_type_logs_with_the_importing_file(shim, logger_name, caplog) -> None:
    """The log line is what an operator sees; it has to name who still uses the old path."""
    with caplog.at_level(logging.WARNING, logger=logger_name), warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        _ = shim.ToolSchema

    records = _records(caplog, logger_name)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "deprecated" in message
    assert __file__ in message, "the warning must name the file that reached for the old path"


@pytest.mark.parametrize(("shim", "logger_name"), _SHIMS)
def test_every_access_is_reported_not_just_the_first(shim, logger_name) -> None:
    """`sys.modules` caches the shim, so a bound name would report the first skill only."""
    del logger_name
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _ = shim.ToolEntry
        _ = shim.ToolEntry

    assert len(caught) == 2


@pytest.mark.parametrize(("shim", "logger_name"), _SHIMS)
def test_the_registry_class_itself_is_not_offered_back(shim, logger_name) -> None:
    """Its storage, dispatch and per-task context were removed, not moved."""
    del logger_name
    with pytest.raises(AttributeError) as excinfo:
        _ = shim.ToolRegistry

    assert "removed, not moved" in str(excinfo.value)


def test_the_registry_shim_lists_only_what_it_can_hand_back() -> None:
    assert dir(registry_shim) == ["BrowserState", "ToolContext", "ToolEntry", "ToolSchema"]


@pytest.mark.parametrize(
    ("source", "logger_name"),
    [
        pytest.param("from ouroboros.tools.registry import ToolEntry", "ouroboros.tools.registry", id="registry"),
        pytest.param("from ouroboros.tools import ToolEntry", "ouroboros.tools", id="package"),
    ],
)
def test_a_skill_module_loads_and_is_reported_exactly_once(source, logger_name, tmp_path, caplog) -> None:
    """The real shape: a skill's `tool.py` exec'd the way `SkillToolProvider` loads it.

    Once, not twice: `from <package> import <name>` reaches a package `__getattr__` a second
    time as the import machinery's `hasattr` probe, which would otherwise report
    `<frozen importlib._bootstrap>` as the importer alongside the real line.
    """
    import importlib.util

    entry = tmp_path / "tool.py"
    entry.write_text(f"{source}\n\nLOADED = ToolEntry\n", encoding="utf-8")

    spec = importlib.util.spec_from_file_location("_ouroboros_skill_tool_legacy", entry)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    with caplog.at_level(logging.WARNING, logger=logger_name), warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        spec.loader.exec_module(module)

    from new_core.tools.entry import ToolEntry

    assert module.LOADED is ToolEntry, "the skill gets the real type, not a stand-in"

    records = _records(caplog, logger_name)
    assert len(records) == 1, [record.getMessage() for record in records]
    assert str(entry) in records[0].getMessage()


def test_the_package_still_imports_its_submodules() -> None:
    """The reason unknown names get a plain `AttributeError` rather than an explanation.

    A package `__getattr__` sits in front of submodule loading: `from ouroboros.tools import
    skill_exec` reaches it first, and the import system only falls through to loading the
    submodule if it raises `AttributeError`.
    """
    from ouroboros.tools import dispatch, skill_exec

    assert skill_exec.__name__ == "ouroboros.tools.skill_exec"
    assert dispatch.__name__ == "ouroboros.tools.dispatch"
