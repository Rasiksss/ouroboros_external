"""ToolDescriptor: construction, defaults, and the name-follows-schema rule."""

from __future__ import annotations

import dataclasses

import pytest

from new_core.tools.interfaces import IToolProvider, ToolDescriptor, ToolSurface
from new_core.tools.schema import ToolSchema


def _schema(name: str = "demo_tool") -> ToolSchema:
    return ToolSchema(name=name, description="demo", parameters={"type": "object", "properties": {}})


def test_descriptor_carries_the_schema_object_itself() -> None:
    schema = _schema()
    desc = ToolDescriptor(
        schema=schema,
        timeout_sec=42,
        origin="builtin",
        surface="core",
        capability_class="read_only",
        safety_policy="check",
    )
    # The model request uses this object directly; no conversion step.
    assert desc.schema is schema


def test_name_delegates_to_the_schema() -> None:
    desc = ToolDescriptor(
        schema=_schema("renamed"),
        timeout_sec=1,
        origin="ext:demo",
        surface="discoverable",
        capability_class="read_only",
        safety_policy="check",
    )
    assert desc.name == "renamed"
    assert "name" not in {f.name for f in dataclasses.fields(desc)}


def test_policy_fields_are_required_not_defaulted() -> None:
    with pytest.raises(TypeError):
        ToolDescriptor(schema=_schema(), timeout_sec=1, origin="builtin")  # type: ignore[call-arg]


def test_optional_field_defaults() -> None:
    desc = ToolDescriptor(
        schema=_schema(),
        timeout_sec=360,
        origin="builtin",
        surface="discoverable",
        capability_class="read_only",
        safety_policy="check",
    )
    assert desc.skips_llm_safety is True
    assert desc.is_code_tool is False
    assert desc.env_prefix is None


def test_descriptor_is_frozen() -> None:
    desc = ToolDescriptor(
        schema=_schema(),
        timeout_sec=1,
        origin="builtin",
        surface="core",
        capability_class="read_only",
        safety_policy="check",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        desc.timeout_sec = 2  # type: ignore[misc]


def test_provider_interface_is_abstract() -> None:
    with pytest.raises(TypeError):
        IToolProvider()  # type: ignore[abstract]


def test_refuse_dispatch_is_abstract_with_no_inherited_default() -> None:
    """A source must answer `refuse_dispatch` for itself; it carries no `return None` default.

    With one, a source that *can* withhold a name it owns would inherit "nothing to say"
    without its author ever being asked — which is how a dead extension came to be reported as
    an unknown tool. `None` is a fine answer; it has to be one somebody gave. Asserted as
    membership rather than the exact abstract set, so adding a method does not break this.
    """
    assert "refuse_dispatch" in IToolProvider.__abstractmethods__
    assert {"refresh", "descriptors", "get", "execute"} <= IToolProvider.__abstractmethods__


def test_tool_surface_offers_no_invocation() -> None:
    # Explicitly out of scope: a tool may read the catalog and request a change, not call.
    assert "execute" not in dir(ToolSurface)
    assert "invoke" not in dir(ToolSurface)
