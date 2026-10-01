"""`ProviderToolSurface`: catalog reads, the pending-intent union, and the drain."""

from __future__ import annotations

from typing import Any

from new_core.tools.interfaces import IToolProvider, ToolDescriptor
from new_core.tools.schema import ToolSchema
from ouroboros.tools.surface import ProviderToolSurface
from tests._shared import make_catalog


class _Provider(IToolProvider):
    def __init__(self, names: list[str], origin: str = "builtin") -> None:
        self._descs = {
            name: ToolDescriptor(
                schema=ToolSchema(name=name, description="d", parameters={}),
                timeout_sec=1,
                origin=origin,
                surface="discoverable",
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


def test_catalog_spans_the_provider_list_first_wins() -> None:
    surface = ProviderToolSurface(make_catalog([_Provider(["a", "b"]), _Provider(["b", "c"], "ext:x")]), set())

    names = [d.name for d in surface.catalog()]

    assert names == ["a", "b", "c"]
    found = surface.find("b")
    assert found is not None
    assert found.origin == "builtin"


def test_find_returns_none_for_an_unknown_name() -> None:
    surface = ProviderToolSurface(make_catalog([_Provider(["a"])]), set())
    assert surface.find("nope") is None
    assert surface.find("") is None


def test_active_names_unions_applied_and_pending() -> None:
    surface = ProviderToolSurface(make_catalog([_Provider(["a", "b"])]), {"a"})

    assert surface.active_names() == frozenset({"a"})
    surface.request_enable("b", object())
    assert surface.active_names() == frozenset({"a", "b"})


def test_request_enable_ignores_a_name_that_is_already_active() -> None:
    surface = ProviderToolSurface(make_catalog([_Provider(["a"])]), {"a"})
    surface.request_enable("a", object())
    assert surface.drain_pending_enables() == []


def test_request_enable_is_recorded_once_per_round() -> None:
    surface = ProviderToolSurface(make_catalog([_Provider(["a"])]), set())
    schema = object()

    surface.request_enable("a", schema)
    surface.request_enable("a", schema)

    assert surface.drain_pending_enables() == [("a", schema)]


def test_draining_resets_the_buffer() -> None:
    surface = ProviderToolSurface(make_catalog([_Provider(["a"])]), set())
    surface.request_enable("a", object())

    assert len(surface.drain_pending_enables()) == 1
    assert surface.drain_pending_enables() == []


def test_the_surface_never_writes_the_active_set() -> None:
    active: set[str] = {"a"}
    surface = ProviderToolSurface(make_catalog([_Provider(["a", "b"])]), active)
    surface.request_enable("b", object())

    # Applying the intent is the node's job; the surface only records it.
    assert active == {"a"}
