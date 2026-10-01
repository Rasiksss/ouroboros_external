"""`dispatch.execute_tool`: the gates run once, before any provider, and hold nothing."""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

from new_core.tools.context import ToolContext
from new_core.tools.interfaces import IToolProvider, ToolDescriptor
from new_core.tools.runtimes import THREAD_RUNTIME
from new_core.tools.schema import ToolSchema
from ouroboros.contracts.task_constraint import TaskConstraint
from tests._shared import make_catalog, run_tool


class RecordingProvider(IToolProvider):
    """A provider that records every execute it is asked to perform."""

    default_runtime = THREAD_RUNTIME

    def __init__(self, name: str, origin: str, *, skips_llm_safety: bool = False, safety_policy: str = "check") -> None:
        self._desc = ToolDescriptor(
            schema=ToolSchema(name=name, description="d", parameters={}),
            timeout_sec=1,
            origin=origin,
            surface="discoverable",
            capability_class="read_only",
            safety_policy=safety_policy,
            skips_llm_safety=skips_llm_safety,
        )
        self.calls: list[tuple[Any, str, dict]] = []

    def refresh(self) -> None:
        return

    def descriptors(self):
        return [self._desc]

    def get(self, name: str):
        return self._desc if name == self._desc.name else None

    def refuse_dispatch(self, name: str) -> str | None:
        return None

    def execute(self, ctx: Any, name: str, args: dict[str, Any]) -> str:
        self.calls.append((ctx, name, dict(args)))
        return f"ran {name}"


@pytest.fixture(autouse=True)
def _allow_safety(monkeypatch):
    import ouroboros.policy.safety as safety

    monkeypatch.setattr(safety, "check_safety", lambda *a, **kw: (True, ""))


def _ctx(tmp_path: pathlib.Path, **kwargs) -> ToolContext:
    return ToolContext(repo_dir=tmp_path, drive_root=tmp_path, **kwargs)


# --- 4.4 heal mode ----------------------------------------------------------------


def _heal_ctx(tmp_path: pathlib.Path) -> ToolContext:
    return _ctx(tmp_path, task_constraint=TaskConstraint(mode="skill_repair", skill_name="broken-skill"))


def test_heal_mode_refuses_extension_tools_before_any_provider(tmp_path) -> None:
    provider = RecordingProvider("ext_9_r_demo_ping", "ext:demo")

    result = run_tool(make_catalog([provider]), _heal_ctx(tmp_path), "ext_9_r_demo_ping", {})

    assert "HEAL_MODE_BLOCKED" in result
    assert provider.calls == []


def test_heal_mode_refuses_mcp_tools_before_any_provider(tmp_path) -> None:
    provider = RecordingProvider("mcp__srv__ping", "mcp:srv")

    result = run_tool(make_catalog([provider]), _heal_ctx(tmp_path), "mcp__srv__ping", {})

    assert "HEAL_MODE_BLOCKED" in result
    assert provider.calls == []


def test_heal_mode_still_allows_a_repair_tool(tmp_path) -> None:
    provider = RecordingProvider("skill_preflight", "builtin")

    result = run_tool(make_catalog([provider]), _heal_ctx(tmp_path), "skill_preflight", {"skill": "broken-skill"})

    assert result == "ran skill_preflight"
    assert len(provider.calls) == 1


def test_heal_mode_refuses_preflight_of_another_skill(tmp_path) -> None:
    provider = RecordingProvider("skill_preflight", "builtin")

    result = run_tool(make_catalog([provider]), _heal_ctx(tmp_path), "skill_preflight", {"skill": "other"})

    assert "Repair may only preflight the selected skill" in result
    assert provider.calls == []


# --- 4.5 no retained context ------------------------------------------------------


def test_execute_tool_holds_no_context_between_calls(tmp_path) -> None:
    provider = RecordingProvider("demo", "builtin")
    ctx_a = _ctx(tmp_path, task_id="a")
    ctx_b = _ctx(tmp_path, task_id="b")

    run_tool(make_catalog([provider]), ctx_a, "demo", {"n": 1})
    run_tool(make_catalog([provider]), ctx_b, "demo", {"n": 2})

    assert [call[0] for call in provider.calls] == [ctx_a, ctx_b]
    # Nothing from the first call leaked into the second.
    assert [call[2] for call in provider.calls] == [{"n": 1}, {"n": 2}]


def test_provider_does_not_retain_the_context(tmp_path) -> None:
    provider = RecordingProvider("demo", "builtin")
    run_tool(make_catalog([provider]), _ctx(tmp_path, task_id="a"), "demo", {})

    leaked = [attr for attr, value in vars(provider).items() if isinstance(value, ToolContext)]
    assert not leaked, f"provider retained context on {leaked}"


# --- resolution order and gates ---------------------------------------------------


def test_first_provider_in_order_wins(tmp_path) -> None:
    first = RecordingProvider("demo", "builtin")
    second = RecordingProvider("demo", "skill:shadow")

    assert run_tool(make_catalog([first, second]), _ctx(tmp_path), "demo", {}) == "ran demo"
    assert len(first.calls) == 1
    assert second.calls == []


def test_unknown_tool_reports_the_local_tools(tmp_path) -> None:
    """The listing comes from local entries, so no extension liveness walk runs."""
    from new_core.tools.entry import ToolEntry
    from tests._shared import static_tool_provider

    local = static_tool_provider(
        ToolEntry(
            "demo",
            {"name": "demo", "description": "d", "parameters": {"type": "object", "properties": {}}},
            lambda ctx, **kw: "ok",
        )
    )

    result = run_tool(make_catalog([local]), _ctx(tmp_path), "nope", {})

    assert "Unknown tool: nope" in result
    assert "demo" in result


def test_a_provider_without_local_entries_still_reports_the_unknown_name(tmp_path) -> None:
    provider = RecordingProvider("demo", "builtin")

    result = run_tool(make_catalog([provider]), _ctx(tmp_path), "nope", {})

    assert "Unknown tool: nope" in result
    assert provider.calls == []


# --- a name a provider owns but withholds ------------------------------------------


class WithholdingProvider(RecordingProvider):
    """Owns a name, offers nothing, and says why — the shape of a dead extension."""

    def __init__(self, name: str, refusal: str) -> None:
        super().__init__(name, "ext:demo")
        self._refusal = refusal
        self.refusals: list[str] = []

    def descriptors(self):
        return []

    def get(self, name: str):
        return None

    def refuse_dispatch(self, name: str) -> str | None:
        if name != self._desc.name:
            return None
        self.refusals.append(name)
        return self._refusal


def test_a_withheld_name_is_explained_not_reported_as_unknown(tmp_path) -> None:
    """The regression this guards: a dead extension answered `Unknown tool`.

    Its tool is hidden from every surface read, so nothing resolves it, and dispatch used to
    conclude the name was unknown — attaching the list of *builtin* tools to a model that
    asked for an extension, and skipping the unload that stops the next task paying for the
    same dead extension again.
    """
    provider = WithholdingProvider("ext_6_r_demo_ping", "⚠️ EXTENSION_NOT_LIVE: nope")

    result = run_tool(make_catalog([provider]), _ctx(tmp_path), "ext_6_r_demo_ping", {})

    assert result == "⚠️ EXTENSION_NOT_LIVE: nope"
    assert provider.refusals == ["ext_6_r_demo_ping"]
    assert provider.calls == [], "a withheld name must not reach execute"


def test_a_withheld_name_is_explained_before_the_safety_supervisor(tmp_path, monkeypatch) -> None:
    """A call that will not run must not first cost a model call."""
    import ouroboros.policy.safety as safety

    checked: list[str] = []
    monkeypatch.setattr(safety, "check_safety", lambda name, *a, **kw: (checked.append(name), (True, ""))[1])
    provider = WithholdingProvider("ext_6_r_demo_ping", "⚠️ EXTENSION_NOT_LIVE: nope")

    run_tool(make_catalog([provider]), _ctx(tmp_path), "ext_6_r_demo_ping", {})

    assert checked == []


def test_a_name_nobody_owns_is_still_unknown(tmp_path) -> None:
    """Asking for a refusal must not turn every typo into someone else's problem."""
    provider = WithholdingProvider("ext_6_r_demo_ping", "⚠️ EXTENSION_NOT_LIVE: nope")

    result = run_tool(make_catalog([provider]), _ctx(tmp_path), "nope", {})

    assert "Unknown tool: nope" in result
    assert provider.refusals == []


def test_heal_mode_still_wins_over_a_withheld_name(tmp_path) -> None:
    """Order preserved from the registry: the repair gate ran ahead of the liveness one."""
    provider = WithholdingProvider("ext_6_r_demo_ping", "⚠️ EXTENSION_NOT_LIVE: nope")

    result = run_tool(make_catalog([provider]), _heal_ctx(tmp_path), "ext_6_r_demo_ping", {})

    assert "HEAL_MODE_BLOCKED" in result
    assert provider.refusals == []


def test_safety_supervisor_can_block_before_the_provider(tmp_path, monkeypatch) -> None:
    import ouroboros.policy.safety as safety

    monkeypatch.setattr(safety, "check_safety", lambda *a, **kw: (False, "⚠️ blocked"))
    provider = RecordingProvider("demo", "builtin")

    assert run_tool(make_catalog([provider]), _ctx(tmp_path), "demo", {}) == "⚠️ blocked"
    assert provider.calls == []


def test_skips_llm_safety_bypasses_the_supervisor(tmp_path, monkeypatch) -> None:
    import ouroboros.policy.safety as safety

    monkeypatch.setattr(safety, "check_safety", lambda *a, **kw: (False, "⚠️ blocked"))
    provider = RecordingProvider("ext_9_r_demo_own", "ext:demo", skips_llm_safety=True)

    assert run_tool(make_catalog([provider]), _ctx(tmp_path), "ext_9_r_demo_own", {}) == "ran ext_9_r_demo_own"
    assert len(provider.calls) == 1


def test_dispatch_passes_the_descriptors_declared_policy_to_check_safety(tmp_path, monkeypatch) -> None:
    # Pins the wiring the other tests' parameter-agnostic mock cannot: the gate
    # must hand check_safety the descriptor's declared policy (the #84 contract),
    # keyed by the canonical name — reverting that call would leave this red.
    import ouroboros.policy.safety as safety

    seen: dict[str, object] = {}

    def _spy(tool_name, arguments, messages=None, ctx=None, policy=None):
        seen["tool_name"] = tool_name
        seen["policy"] = policy
        return True, ""

    monkeypatch.setattr(safety, "check_safety", _spy)
    provider = RecordingProvider("ext_9_r_demo_thing", "ext:demo", safety_policy="check")

    run_tool(make_catalog([provider]), _ctx(tmp_path), "ext_9_r_demo_thing", {})

    assert seen["tool_name"] == "ext_9_r_demo_thing"
    assert seen["policy"] == "check"


def test_a_declared_skip_short_circuits_without_calling_check_safety(tmp_path, monkeypatch) -> None:
    # A skip verdict is immediate, so dispatch takes no thread hop for it: a
    # parallel batch of skip tools must spawn no safety executors at all.
    import ouroboros.policy.safety as safety

    called: list[str] = []
    monkeypatch.setattr(safety, "check_safety", lambda name, *a, **kw: (called.append(name), (True, ""))[1])
    provider = RecordingProvider("ext_9_r_demo_fast", "ext:demo", safety_policy="skip")

    assert run_tool(make_catalog([provider]), _ctx(tmp_path), "ext_9_r_demo_fast", {}) == "ran ext_9_r_demo_fast"
    assert called == []


def test_check_safety_runs_off_the_event_loop(tmp_path, monkeypatch) -> None:
    """The LLM path of check_safety blocks; dispatch must not run it on the loop."""
    import asyncio

    import ouroboros.policy.safety as safety

    on_loop: list[bool] = []

    def _spy(*a, **kw):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return True, ""

    monkeypatch.setattr(safety, "check_safety", _spy)
    provider = RecordingProvider("demo", "builtin")

    run_tool(make_catalog([provider]), _ctx(tmp_path), "demo", {})

    assert on_loop == [False], "check_safety must run in a worker thread, not on the event loop"


def test_a_warning_from_safety_is_prepended_to_the_result(tmp_path, monkeypatch) -> None:
    import ouroboros.policy.safety as safety

    monkeypatch.setattr(safety, "check_safety", lambda *a, **kw: (True, "⚠️ careful"))
    provider = RecordingProvider("demo", "builtin")

    result = run_tool(make_catalog([provider]), _ctx(tmp_path), "demo", {})

    assert result == "⚠️ careful\n\n---\nran demo"


def test_excluded_surface_is_refused_before_the_provider(tmp_path, monkeypatch) -> None:
    import ouroboros.tools.dispatch as dispatch
    from ouroboros.tools.spec import ToolSpec

    monkeypatch.setattr(
        dispatch,
        "spec_for_tool",
        lambda name: ToolSpec(name=name, surface="excluded"),
    )
    provider = RecordingProvider("demo", "builtin")

    result = run_tool(make_catalog([provider]), _ctx(tmp_path), "demo", {})

    assert "TOOL_UNAVAILABLE" in result
    assert provider.calls == []
