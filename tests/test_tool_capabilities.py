"""Tests for tool capability SSOT and no-drift invariants.

Verifies:
- tool_capabilities.py is the single source of truth
- loop_tool_execution.py imports from capabilities (no local copy)
- run_shell list-cmd happy path (string-cmd cascade lives in test_shell_run_shell.py)
"""

import inspect
import pathlib
import re
import tempfile

from tests._shared import make_tool_env

# ---------------------------------------------------------------------------
# SSOT drift tests
# ---------------------------------------------------------------------------


def test_capability_consumers_share_ssot_objects():
    import ouroboros.agent.loop_tool_execution as lte
    from ouroboros.policy import capabilities

    # Parallelism is no longer read from a module-level set here: the gate asks the
    # per-call metadata snapshot, covered by
    # ``test_loop_execution_parallel_gate_uses_metadata_snapshot`` below.
    assert lte.REVIEWED_MUTATIVE_TOOLS is capabilities.REVIEWED_MUTATIVE_TOOLS
    assert lte._UNTRUNCATED_TOOL_RESULTS is capabilities.UNTRUNCATED_TOOL_RESULTS
    assert lte._TOOL_RESULT_LIMITS is capabilities.TOOL_RESULT_LIMITS


def test_capabilities_sets_are_frozensets():
    """All exported sets must be frozensets (immutable)."""
    from ouroboros.policy.capabilities import (
        CORE_TOOL_NAMES,
        META_TOOL_NAMES,
        READ_ONLY_PARALLEL_TOOLS,
        UNTRUNCATED_TOOL_RESULTS,
    )

    for name, obj in [
        ("CORE_TOOL_NAMES", CORE_TOOL_NAMES),
        ("META_TOOL_NAMES", META_TOOL_NAMES),
        ("READ_ONLY_PARALLEL_TOOLS", READ_ONLY_PARALLEL_TOOLS),
        ("UNTRUNCATED_TOOL_RESULTS", UNTRUNCATED_TOOL_RESULTS),
    ]:
        assert isinstance(obj, frozenset), f"{name} must be a frozenset"


def test_headless_registry_excludes_self_mod_tools():
    """Production tool surface must not expose shell, git, or self-evolution tools."""

    tmp = pathlib.Path(tempfile.mkdtemp())
    catalog, _ctx = make_tool_env(tmp, tmp)
    all_names = {d.name for d in catalog.descriptors() if d.surface == "core"}
    forbidden = {
        "run_shell",
        "repo_commit",
        "repo_write",
        "git_status",
        "git_diff",
    }
    assert forbidden.isdisjoint(all_names)


# ---------------------------------------------------------------------------
# Discovery path drift test
# ---------------------------------------------------------------------------


def test_discovery_without_a_tool_surface_says_so():
    """The meta-tool needs no module-global registry; without a surface it degrades."""
    import ouroboros.tools.tool_discovery as td
    from new_core.tools.context import ToolContext

    tmp = pathlib.Path(tempfile.mkdtemp())
    ctx = ToolContext(repo_dir=tmp, drive_root=tmp)

    assert td._list_available_tools(ctx) == "Tool discovery not available in this context."


def test_discovery_path_consistent_with_the_catalog():
    """list_available_tools must list exactly the discoverable tools not already active."""
    import ouroboros.tools.tool_discovery as td
    from new_core.tools.context import ToolContext
    from new_core.tools.providers import BuiltinToolProvider
    from ouroboros.tools.surface import ProviderToolSurface
    from tests._shared import make_catalog

    tmp = pathlib.Path(tempfile.mkdtemp())
    catalog = make_catalog([BuiltinToolProvider()])
    catalog.refresh()

    ctx = ToolContext(repo_dir=tmp, drive_root=tmp)
    ctx.tool_surface = ProviderToolSurface(catalog, set())

    expected = {d.name for d in catalog.descriptors() if d.surface == "discoverable"}
    expected -= {"list_available_tools", "enable_tools"}

    output = td._list_available_tools(ctx)

    if not expected:
        assert "All tools are already" in output
    else:
        for name in expected:
            assert name in output, f"'{name}' is discoverable but the listing does not show it"


def test_discovery_hides_tools_already_on_the_surface():
    import ouroboros.tools.tool_discovery as td
    from new_core.tools.context import ToolContext
    from new_core.tools.providers import BuiltinToolProvider
    from ouroboros.tools.surface import ProviderToolSurface
    from tests._shared import make_catalog

    tmp = pathlib.Path(tempfile.mkdtemp())
    catalog = make_catalog([BuiltinToolProvider()])
    catalog.refresh()
    discoverable = sorted(d.name for d in catalog.descriptors() if d.surface == "discoverable")
    assert discoverable, "expected at least one discoverable built-in"

    ctx = ToolContext(repo_dir=tmp, drive_root=tmp)
    ctx.tool_surface = ProviderToolSurface(catalog, {discoverable[0]})

    assert discoverable[0] not in td._list_available_tools(ctx)


# ---------------------------------------------------------------------------
# capability_class metadata and policy matrix invariants
# ---------------------------------------------------------------------------


def test_every_catalog_tool_has_capability_class():
    """Every TOOL_SPECS entry must declare a non-empty capability_class."""
    from ouroboros.tools.catalog import TOOL_SPECS

    for name, spec in TOOL_SPECS.items():
        assert spec.capability_class, f"{name} missing capability_class"


def test_skill_tools_require_sandbox():
    """skill_exec and skill_preflight must set sandbox_required=True."""
    from ouroboros.policy.tool_authorization import authorize_tool

    for tool in ("skill_exec", "skill_preflight"):
        decision = authorize_tool(tool, {})
        assert decision.outcome == "allow", tool
        assert decision.sandbox_required is True, tool
        assert decision.sandbox_policy_factory in {"skill_default", "preflight_default"}, tool


def test_run_shell_not_in_core_tools():
    """run_shell MUST NOT appear in CORE_TOOL_NAMES."""
    from ouroboros.policy.capabilities import CORE_TOOL_NAMES

    assert "run_shell" not in CORE_TOOL_NAMES


def test_excluded_tools_have_deny_capability_class():
    """Excluded self-mod tools must map to shell/git/control capability classes."""
    from ouroboros.tools.catalog import TOOL_SPECS

    assert TOOL_SPECS["run_shell"].capability_class == "shell"
    assert TOOL_SPECS["repo_write"].capability_class == "git"
    assert TOOL_SPECS["toggle_evolution"].capability_class == "control"


def test_mutative_core_tools_allow_without_sandbox():
    """Mutative core tools allow without sandbox (owner confirm is P2 gap)."""
    from ouroboros.policy.tool_authorization import authorize_tool

    decision = authorize_tool("data_write", {})
    assert decision.outcome == "allow"
    assert decision.sandbox_required is False
    assert decision.capability_class == "mutative"


def test_external_network_tool_allow_without_sandbox():
    from ouroboros.policy.tool_authorization import authorize_tool

    decision = authorize_tool("gigasearch", {})
    assert decision.outcome == "allow"
    assert decision.sandbox_required is False
    assert decision.capability_class == "external_network"


def test_authorize_tool_honors_the_declared_capability_class():
    """#84: a namespaced name is absent from TOOL_SPECS; without the declared
    class every skill/extension tool would audit as the default read_only."""
    from ouroboros.policy.tool_authorization import authorize_tool

    decision = authorize_tool("ext_9_r_assignment_send_assignment", {}, capability_class="external_network")
    assert decision.outcome == "allow"
    assert decision.capability_class == "external_network"
    # Unstated keeps the name-keyed spec classification for core tools.
    assert authorize_tool("data_write", {}).capability_class == "mutative"


def test_metadata_snapshot_carries_the_descriptor_capability_class():
    """The loop authorizes from the frozen snapshot, so the descriptor's
    declared class must survive into it."""
    from new_core.tools.interfaces import IToolProvider, ToolDescriptor
    from new_core.tools.runtimes import THREAD_RUNTIME
    from new_core.tools.schema import ToolSchema
    from ouroboros.tools.runtime_metadata import resolve_tool_runtime_metadata
    from tests._shared import make_catalog

    class _OneToolProvider(IToolProvider):
        default_runtime = THREAD_RUNTIME

        def __init__(self):
            self._desc = ToolDescriptor(
                schema=ToolSchema(name="ext_9_r_demo_send", description="d", parameters={}),
                timeout_sec=1,
                origin="ext:demo",
                surface="discoverable",
                capability_class="external_network",
                safety_policy="check",
            )

        def refresh(self):
            return

        def descriptors(self):
            return [self._desc]

        def get(self, name):
            return self._desc if name == self._desc.name else None

        def refuse_dispatch(self, name):
            return None

        def execute(self, ctx, name, args):
            return "ok"

    metadata = resolve_tool_runtime_metadata("ext_9_r_demo_send", make_catalog([_OneToolProvider()]))
    assert metadata.capability_class == "external_network"


def test_metadata_spec_fallback_carries_the_catalog_capability_class():
    """A name no provider offers but TOOL_SPECS lists (excluded tombstones)
    must resolve to its declared class, not the dataclass default."""
    from ouroboros.tools.catalog import TOOL_SPECS
    from ouroboros.tools.runtime_metadata import resolve_tool_runtime_metadata

    assert resolve_tool_runtime_metadata("run_shell").capability_class == TOOL_SPECS["run_shell"].capability_class


def test_loop_execution_authorizes_with_the_snapshot_capability():
    """Wiring pin: authorize_tool must receive the snapshot's class, or every
    namespaced tool silently audits as read_only again."""
    import ouroboros.agent.loop_tool_execution as lte

    source = inspect.getsource(lte)
    assert "authorize_tool(fn_name, args, ctx, capability_class=metadata.capability_class)" in source


def test_loop_execution_imports_from_capabilities():
    """loop_tool_execution.py must import sets from tool_capabilities."""
    import ouroboros.agent.loop_tool_execution as lte

    source = inspect.getsource(lte)
    assert "from ouroboros.policy.capabilities import" in source
    # Must NOT have local frozenset definitions for these sets
    for name in ("READ_ONLY_PARALLEL_TOOLS", "_UNTRUNCATED_TOOL_RESULTS"):
        # Check there's no local `X = frozenset({` pattern
        pattern = rf"^{re.escape(name)}\s*[:=]\s*frozenset"
        assert not re.search(pattern, source, re.MULTILINE), (
            f"{name} is locally defined in loop_tool_execution.py — should import from tool_capabilities"
        )


def test_loop_execution_parallel_gate_uses_metadata_snapshot():
    """The parallel gate reads spec.parallel through the runtime-metadata snapshot."""
    from ouroboros.policy.capabilities import READ_ONLY_PARALLEL_TOOLS
    from ouroboros.tools.runtime_metadata import resolve_tool_runtime_metadata

    assert resolve_tool_runtime_metadata("data_read").parallel_safe
    assert not resolve_tool_runtime_metadata("run_shell").parallel_safe
    # Metadata parallel_safe must agree with the capabilities SSOT for built-ins.
    from ouroboros.tools.catalog import TOOL_SPECS

    for name in TOOL_SPECS:
        assert resolve_tool_runtime_metadata(name).parallel_safe == (name in READ_ONLY_PARALLEL_TOOLS)


def test_discovery_reads_the_catalog_through_the_tool_context():
    """tool_discovery.py must reach the catalog via ctx.tool_surface, not a global."""
    import ouroboros.tools.tool_discovery as td

    source = inspect.getsource(td)
    assert "tool_surface" in source, "tool_discovery.py must read the catalog through ctx.tool_surface"
    assert "_registry" not in source, "tool_discovery.py must not hold a module-global registry"
    assert "set_registry" not in source, "set_registry is gone; the surface arrives on ToolContext"
    assert "tool_policy" not in source, "tool_discovery.py must not route through the superseded tool_policy"
