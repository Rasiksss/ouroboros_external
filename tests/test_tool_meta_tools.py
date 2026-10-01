"""`list_available_tools` / `enable_tools` read the catalog off `ctx.tool_surface`."""

from __future__ import annotations

import pathlib
from typing import Any

import ouroboros.tools.tool_discovery as td
from new_core.tools.context import ToolContext
from new_core.tools.interfaces import IToolProvider, ToolDescriptor
from new_core.tools.schema import ToolSchema
from ouroboros.tools.surface import ProviderToolSurface
from tests._shared import make_catalog


class _Provider(IToolProvider):
    def __init__(self, names: list[str], surface: str = "discoverable") -> None:
        self._descs = {
            name: ToolDescriptor(
                schema=ToolSchema(name=name, description=f"{name} does things", parameters={}),
                timeout_sec=1,
                origin="builtin",
                surface=surface,
                capability_class="read_only",
                safety_policy="check",
            )
            for name in names
        }

    def refresh(self) -> None:
        return

    def descriptors(self):
        return list(self._descs.values())

    def get(self, name: str):
        return self._descs.get(name)

    def refuse_dispatch(self, name: str) -> str | None:
        return None

    def execute(self, ctx: Any, name: str, args: dict[str, Any]) -> str:
        return "ok"


def _ctx(tmp_path: pathlib.Path, providers, active: set[str] | None = None) -> ToolContext:
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.tool_surface = ProviderToolSurface(make_catalog(providers), active if active is not None else set())
    return ctx


# --- 6.5 two calls in one round ---------------------------------------------------


def test_two_enable_calls_in_one_round_see_each_other(tmp_path) -> None:
    ctx = _ctx(tmp_path, [_Provider(["calculator"])])

    first = td._enable_tools(ctx, tools="calculator")
    second = td._enable_tools(ctx, tools="calculator")

    assert first == "✅ Enabled: calculator"
    assert second == "✅ Enabled: calculator (already active)"
    # Only one intent reaches the override channel.
    assert [name for name, _ in ctx.tool_surface.drain_pending_enables()] == ["calculator"]


def test_an_already_active_tool_is_reported_as_such(tmp_path) -> None:
    ctx = _ctx(tmp_path, [_Provider(["calculator"])], active={"calculator"})

    assert td._enable_tools(ctx, tools="calculator") == "✅ Enabled: calculator (already active)"
    assert ctx.tool_surface.drain_pending_enables() == []


def test_an_unknown_name_is_reported_synchronously(tmp_path) -> None:
    ctx = _ctx(tmp_path, [_Provider(["calculator"])])

    result = td._enable_tools(ctx, tools="calculator, nope")

    assert "✅ Enabled: calculator" in result
    assert "❌ Not found: nope" in result
    # No intent is recorded for the unknown name.
    assert [name for name, _ in ctx.tool_surface.drain_pending_enables()] == ["calculator"]


def test_an_alias_onto_an_active_tool_reports_already_active(tmp_path) -> None:
    """The active check must run on the resolved name, not the one the model typed."""

    class _AliasProvider(_Provider):
        def get(self, name: str):
            return super().get("ext_6_r_demo_ping" if name == "demo.ping" else name)

    ctx = _ctx(tmp_path, [_AliasProvider(["ext_6_r_demo_ping"])], active={"ext_6_r_demo_ping"})

    assert td._enable_tools(ctx, tools="demo.ping") == "✅ Enabled: demo.ping (already active)"
    assert ctx.tool_surface.drain_pending_enables() == []


def test_an_alias_is_not_enabled_twice_in_one_round(tmp_path) -> None:
    class _AliasProvider(_Provider):
        def get(self, name: str):
            return super().get("ext_6_r_demo_ping" if name == "demo.ping" else name)

    ctx = _ctx(tmp_path, [_AliasProvider(["ext_6_r_demo_ping"])])

    assert td._enable_tools(ctx, tools="demo.ping") == "✅ Enabled: ext_6_r_demo_ping"
    assert td._enable_tools(ctx, tools="demo.ping") == "✅ Enabled: demo.ping (already active)"
    assert len(ctx.tool_surface.drain_pending_enables()) == 1


def test_an_excluded_tool_is_not_found(tmp_path) -> None:
    ctx = _ctx(tmp_path, [_Provider(["hidden"], surface="excluded")])

    assert td._enable_tools(ctx, tools="hidden") == "❌ Not found: hidden"
    assert ctx.tool_surface.drain_pending_enables() == []


def test_enabling_nothing_says_so(tmp_path) -> None:
    ctx = _ctx(tmp_path, [_Provider(["calculator"])])
    assert td._enable_tools(ctx, tools="  ") == "No tools specified."


def test_the_intent_carries_the_schema_the_request_will_use(tmp_path) -> None:
    provider = _Provider(["calculator"])
    ctx = _ctx(tmp_path, [provider])

    td._enable_tools(ctx, tools="calculator")
    pending = ctx.tool_surface.drain_pending_enables()

    assert len(pending) == 1
    assert pending[0][1] is provider.get("calculator").schema


# --- listing ----------------------------------------------------------------------


def test_listing_excludes_active_and_meta_tools(tmp_path) -> None:
    providers = [_Provider(["calculator", "gigasearch", "list_available_tools", "enable_tools"])]
    ctx = _ctx(tmp_path, providers, active={"gigasearch"})

    output = td._list_available_tools(ctx)

    assert "calculator" in output
    assert "gigasearch" not in output
    assert "list_available_tools" not in output


def test_listing_says_so_when_everything_is_active(tmp_path) -> None:
    ctx = _ctx(tmp_path, [_Provider(["calculator"])], active={"calculator"})
    assert td._list_available_tools(ctx) == "All tools are already in your active set."


def test_a_pending_enable_hides_the_tool_from_the_listing(tmp_path) -> None:
    ctx = _ctx(tmp_path, [_Provider(["calculator", "gigasearch"])])

    td._enable_tools(ctx, tools="calculator")

    assert "calculator" not in td._list_available_tools(ctx)


# --- 6.3 the module global is gone ------------------------------------------------


def test_the_module_global_registry_is_gone() -> None:
    assert not hasattr(td, "_registry")
    assert not hasattr(td, "set_registry")
