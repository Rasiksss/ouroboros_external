"""`ToolCatalog`: the ordered provider list as one object, holding no task context."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from new_core.tools.catalog import ToolCatalog
from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.interfaces import IToolProvider, ToolDescriptor
from new_core.tools.runtimes import THREAD_RUNTIME
from new_core.tools.schema import ToolSchema
from tests._shared import make_catalog


class _Provider(IToolProvider):
    default_runtime = THREAD_RUNTIME

    def __init__(self, names: list[str], origin: str = "builtin", *, code: bool = False) -> None:
        self.refreshed = 0
        self.closed = 0
        self.calls: list[tuple[Any, str]] = []
        self._descs = {
            name: ToolDescriptor(
                schema=ToolSchema(name=name, description="d", parameters={}),
                timeout_sec=7,
                origin=origin,
                surface="discoverable",
                capability_class="read_only",
                safety_policy="check",
                is_code_tool=code,
            )
            for name in names
        }

    def refresh(self) -> None:
        self.refreshed += 1

    def descriptors(self):
        return list(self._descs.values())

    def get(self, name: str):
        return self._descs.get(name)

    def refuse_dispatch(self, name: str) -> str | None:
        return None

    def execute(self, ctx: Any, name: str, args: dict[str, Any]) -> str:
        self.calls.append((ctx, name))
        return f"{self._descs[name].origin}:{name}"

    def close(self) -> None:
        self.closed += 1


class _LocalProvider(_Provider):
    """A provider with local callables, like the builtin and skill ones."""

    def entries(self) -> dict[str, ToolEntry]:
        return {
            name: ToolEntry(
                name=name,
                schema=desc.schema,
                handler=lambda ctx, **kw: "ok",
                is_code_tool=desc.is_code_tool,
            )
            for name, desc in self._descs.items()
        }


def _ctx(tmp_path) -> ToolContext:
    return ToolContext(repo_dir=tmp_path, drive_root=tmp_path)


# --- composition ------------------------------------------------------------------


def test_refresh_reaches_every_provider() -> None:
    a, b = _Provider(["x"]), _Provider(["y"])
    catalog = ToolCatalog()
    catalog.initialize([a, b])
    assert (a.refreshed, b.refreshed) == (1, 1), "initialize refreshes what it installs"

    catalog.refresh()
    assert (a.refreshed, b.refreshed) == (2, 2)


def test_initialize_replaces_the_provider_list() -> None:
    """A second wiring installs its list, rather than stacking it behind the first.

    There is one catalog per process, so the alternative — appending — would leave the
    original providers in front, shadowing every name the new list declares.
    """
    first, second = _Provider(["x"]), _Provider(["y"])
    catalog = ToolCatalog()
    catalog.initialize([first])
    catalog.initialize([second])

    assert list(catalog.providers) == [second]
    assert [d.name for d in catalog.descriptors()] == ["y"]


def test_close_reaches_every_provider_even_if_one_raises() -> None:
    class _Angry(_Provider):
        def close(self) -> None:
            raise RuntimeError("nope")

    a, b = _Angry(["x"]), _Provider(["y"])
    make_catalog([a, b]).close()
    assert b.closed == 1


def test_providers_are_exposed_read_only() -> None:
    catalog = make_catalog([_Provider(["x"])])
    assert len(catalog) == 1
    assert isinstance(catalog.providers, tuple)

    catalog.add(_Provider(["y"]))
    assert len(catalog) == 2
    assert [d.name for d in catalog.descriptors()] == ["x", "y"]


# --- resolution order -------------------------------------------------------------


def test_first_provider_wins_a_name_collision(tmp_path) -> None:
    first, second = _Provider(["shared"], "builtin"), _Provider(["shared"], "skill:s")
    catalog = make_catalog([first, second])

    found = catalog.find("shared")
    assert found is not None and found.origin == "builtin"
    assert [d.name for d in catalog.descriptors()] == ["shared"]
    assert asyncio.run(catalog.aexecute(_ctx(tmp_path), "shared", {})) == "builtin:shared"
    assert second.calls == []


def test_a_collision_is_logged_once_per_refresh(caplog) -> None:
    catalog = make_catalog([_Provider(["shared"], "builtin"), _Provider(["shared"], "ext:e")])
    caplog.clear()  # wiring already refreshed once; count the warnings of one refresh
    with caplog.at_level(logging.WARNING, logger="new_core.tools.catalog"):
        catalog.refresh()
    assert len([r for r in caplog.records if "Tool name collision: shared" in r.getMessage()]) == 1


def test_find_tolerates_a_blank_or_unknown_name() -> None:
    catalog = make_catalog([_Provider(["x"])])
    assert catalog.find("") is None
    assert catalog.find("   ") is None
    assert catalog.find("nope") is None


# --- a name a provider owns but withholds ------------------------------------------


class _Withholding(_Provider):
    """Offers nothing and refuses one name — the shape of a dead extension's provider."""

    def __init__(self, name: str, refusal: str) -> None:
        super().__init__([], "ext:e")
        self._name = name
        self._refusal = refusal

    def refuse_dispatch(self, name: str) -> str | None:
        return self._refusal if name == self._name else None


def test_the_first_provider_with_a_refusal_wins() -> None:
    """Same resolution order as every other read, so one name has one answer."""
    first = _Withholding("shared", "first says no")
    second = _Withholding("shared", "second says no")

    assert make_catalog([first, second]).refuse_dispatch("shared") == "first says no"


def test_a_refusal_is_looked_up_by_the_stripped_name() -> None:
    catalog = make_catalog([_Withholding("shared", "no")])
    assert catalog.refuse_dispatch("  shared  ") == "no"


def test_nobody_refuses_a_blank_or_unowned_name() -> None:
    catalog = make_catalog([_Withholding("shared", "no"), _Provider(["x"])])
    assert catalog.refuse_dispatch("") is None
    assert catalog.refuse_dispatch("nope") is None
    assert catalog.refuse_dispatch("x") is None, "a name a provider offers is not a refusal"


# --- local reads stay off the expensive path --------------------------------------


def test_local_reads_ignore_providers_without_entries() -> None:
    local = _LocalProvider(["repo_tool"], "builtin", code=True)
    remote = _Provider(["ext_tool"], "ext:e")
    catalog = make_catalog([local, remote])

    assert catalog.local_tool_names() == ["repo_tool"]
    assert catalog.code_tool_names() == {"repo_tool"}
    assert set(catalog.local_entries()) == {"repo_tool"}
    # The full catalog still sees both.
    assert {d.name for d in catalog.descriptors()} == {"repo_tool", "ext_tool"}


def test_local_entries_are_first_wins_too() -> None:
    first = _LocalProvider(["shared"], "builtin")
    second = _LocalProvider(["shared"], "skill:s")
    entries = make_catalog([first, second]).local_entries()

    assert entries["shared"].schema is first.descriptors()[0].schema


# --- the context is never retained ------------------------------------------------


def test_the_catalog_holds_no_task_context(tmp_path) -> None:
    """The line between this and the retired ToolRegistry (D3)."""
    provider = _Provider(["demo"])
    catalog = make_catalog([provider])
    first, second = _ctx(tmp_path), _ctx(tmp_path)

    asyncio.run(catalog.aexecute(first, "demo", {}))
    asyncio.run(catalog.aexecute(second, "demo", {}))

    assert [c for c, _ in provider.calls] == [first, second]
    assert not [v for v in vars(catalog).values() if isinstance(v, ToolContext)]


def test_executing_an_unknown_name_reports_it(tmp_path) -> None:
    assert "Unknown tool: nope" in asyncio.run(make_catalog([_Provider(["demo"])]).aexecute(_ctx(tmp_path), "nope", {}))
