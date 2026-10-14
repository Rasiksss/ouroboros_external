"""Tests for the frozen per-tool runtime-metadata snapshot and its consumers.

Covers ``resolve_tool_runtime_metadata`` source branches (built-in / extension /
extension-shaped-without-metadata / MCP / unknown), the parallel gate reading
``parallel_safe`` through the snapshot, and retrieval-alias indexing.

The snapshot is assembled from the owning provider's ``ToolDescriptor``, so most of
these go through a catalog — the same object the loop resolves against. The two shapes
that reach the resolver *without* one are deliberate and tested as such: a name only the
static policy table knows, and an extension-shaped name nothing resolves at all.
"""

from __future__ import annotations

import dataclasses
import json
import threading
from types import SimpleNamespace

import pytest

from new_core.llm.types import ToolCall, ToolCallFunction
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.tools.runtime_metadata import ResolvedToolRuntimeMetadata, resolve_tool_runtime_metadata
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.egress_auth import null_egress_auth_dependencies
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.factories import permissive_safety
from tests.helpers.shared import (
    METADATA_EXTENSION,
    METADATA_EXTENSION_ALIASES,
    METADATA_EXTENSION_RESULT_LIMIT,
    METADATA_EXTENSION_TIMEOUT_SEC,
    METADATA_EXTENSION_TOOL,
    TEST_SCOPE,
    collecting_event_sink,
    load_metadata_extension,
    make_catalog,
    make_tool_ctx,
    static_tool_provider,
)
from tests.helpers.user_settings import StubMcpServerIdsReader

# ---------------------------------------------------------------------------
# Resolver source branches
# ---------------------------------------------------------------------------


def test_builtin_metadata_matches_catalog_spec_values():
    from ouroboros.tools.catalog import DEFAULT_TOOL_RESULT_LIMIT, TOOL_SPECS, spec_for_tool

    for name in TOOL_SPECS:
        spec = spec_for_tool(name)
        metadata = resolve_tool_runtime_metadata(name, scope=TEST_SCOPE)
        assert metadata.stable_name == name
        assert metadata.retrieval_aliases == ()
        assert metadata.result_limit == (spec.result_limit or DEFAULT_TOOL_RESULT_LIMIT)
        assert metadata.timeout_sec == spec.timeout_sec
        assert metadata.parallel_safe == spec.parallel
        assert metadata.log_redacted_args == frozenset()


def test_builtin_metadata_pins_effective_per_tool_values():
    data_read = resolve_tool_runtime_metadata("data_read", scope=TEST_SCOPE)
    assert (data_read.result_limit, data_read.parallel_safe) == (80_000, True)
    # Module-declared ToolEntry timeouts were mirrored into the catalog so the
    # snapshot swap keeps today's effective loop timeouts.
    assert resolve_tool_runtime_metadata("calendar_resolve", scope=TEST_SCOPE).timeout_sec == 10
    assert resolve_tool_runtime_metadata("vlm_query", scope=TEST_SCOPE).timeout_sec == 90
    assert resolve_tool_runtime_metadata("skill_exec", scope=TEST_SCOPE).timeout_sec == 300
    assert resolve_tool_runtime_metadata("run_shell", scope=TEST_SCOPE).timeout_sec == 360


def test_catalog_timeouts_match_tool_entry_declarations(tmp_path):
    """Drift guard: catalog timeout_sec must track the module ToolEntry declarations."""
    from ouroboros.tools.catalog import TOOL_SPECS, spec_for_tool
    from tests.helpers.shared import make_tool_env

    catalog, _ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")
    for name, entry in catalog.local_entries().items():
        if name not in TOOL_SPECS:
            continue
        spec = spec_for_tool(name)
        assert entry.timeout_sec == spec.timeout_sec, (
            f"{name}: ToolEntry timeout {entry.timeout_sec} != catalog {spec.timeout_sec}"
        )


@pytest.mark.parametrize(
    "slot_seconds,parse_seconds,read_seconds,expected",
    [(80, 60, 100, (247, 507)), (400, 400, 100, (907, 1167)), (80, 60, 900, (1047, 507))],
)
def test_attachment_tool_timeouts_preserve_io_allowance_beside_parser_budgets(
    isolate_settings, monkeypatch, slot_seconds, parse_seconds, read_seconds, expected
):
    from new_core.tools.providers.builtin import BuiltinToolProvider
    from ouroboros.tools.catalog import spec_for_tool

    for suffix, value in {
        "SLOT_TIMEOUT_SEC": slot_seconds,
        "PARSE_TIMEOUT_SEC": parse_seconds,
        "PARSER_JOIN_TIMEOUT_SEC": 2,
        "READ_TIMEOUT_SEC": read_seconds,
    }.items():
        monkeypatch.setenv(f"OUROBOROS_ATTACHMENT_{suffix}", str(value))
    isolate_settings.bootstrap_config(force=True)
    provider = BuiltinToolProvider(modules=("attachments", "service.messaging"))
    catalog = make_catalog([provider])
    for name, timeout in zip(("attachment_read", "send_file"), expected, strict=True):
        assert provider.entries()[name].timeout_sec == spec_for_tool(name).timeout_sec == timeout
        assert resolve_tool_runtime_metadata(name, catalog, scope=TEST_SCOPE).timeout_sec == timeout
        assert resolve_tool_runtime_metadata(name, scope=TEST_SCOPE).timeout_sec == timeout


def test_provider_descriptor_keeps_declared_timeout_for_tool_skill_alias():
    """Namespaced ``type=tool`` skill entries resolve through the catalog, not fail-closed."""
    catalog = make_catalog(
        [
            static_tool_provider(
                ToolEntry(
                    name="echo_skill.echo",
                    schema=ToolSchema(name="echo_skill.echo", description="", parameters={}),
                    handler=lambda ctx, **args: "ok",
                    timeout_sec=360,
                )
            )
        ]
    )

    metadata = resolve_tool_runtime_metadata("echo_skill.echo", catalog, scope=TEST_SCOPE)
    assert metadata.timeout_sec == 360
    assert metadata.log_redacted_args == frozenset()
    assert metadata.parallel_safe is False
    assert metadata.result_limit == 15_000
    # Without the catalog the dotted name has no live metadata and fails closed.
    assert resolve_tool_runtime_metadata("echo_skill.echo", scope=TEST_SCOPE).timeout_sec == 60
    # Builtins keep resolving from the catalog spec even when the name is unknown to it.
    assert resolve_tool_runtime_metadata("run_shell", catalog, scope=TEST_SCOPE).timeout_sec == 360


def test_extension_metadata_from_a_loaded_extension(tmp_path):
    from ouroboros.skills.extensions import loader as extension_loader

    surface_name, _drive_root, catalog = load_metadata_extension(tmp_path)
    stable_name = f"{METADATA_EXTENSION}.{METADATA_EXTENSION_TOOL}"
    try:
        metadata = resolve_tool_runtime_metadata(surface_name, catalog, scope=TEST_SCOPE)
        assert metadata.stable_name == stable_name
        assert metadata.retrieval_aliases == METADATA_EXTENSION_ALIASES
        assert metadata.result_limit == METADATA_EXTENSION_RESULT_LIMIT
        assert metadata.timeout_sec == METADATA_EXTENSION_TIMEOUT_SEC
        assert metadata.parallel_safe is True
        assert metadata.log_redacted_args == frozenset()
        # The ``skill.tool`` alias form resolves to the same snapshot.
        assert resolve_tool_runtime_metadata(stable_name, catalog, scope=TEST_SCOPE) == metadata
    finally:
        extension_loader.unload_extension(METADATA_EXTENSION)


def test_extension_shaped_name_without_metadata_fails_closed():
    from ouroboros.skills.extensions.loader import extension_surface_name

    for name in (extension_surface_name("ghost-skill", "vanished"), "ghost-skill.vanished"):
        metadata = resolve_tool_runtime_metadata(name, scope=TEST_SCOPE)
        assert metadata.stable_name == name
        assert metadata.log_redacted_args is None
        assert metadata.retrieval_aliases == ()
        assert metadata.result_limit == 15_000
        assert metadata.timeout_sec == 60
        assert metadata.parallel_safe is False


def test_mcp_name_unknown_to_manager_gets_defaults():
    # Not in the manager's tool index: no aliases, no configured limit.
    metadata = resolve_tool_runtime_metadata("mcp__server__tool", scope=TEST_SCOPE)
    assert metadata.stable_name == "mcp__server__tool"
    assert metadata.retrieval_aliases == ()
    assert metadata.result_limit == 15_000
    assert metadata.parallel_safe is False
    assert metadata.log_redacted_args == frozenset()


def test_mcp_metadata_uses_manager_aliases_and_limit(monkeypatch):
    from new_core.tools.providers import McpToolProvider
    from ouroboros.mcp.manager import mcp_manager

    monkeypatch.setattr(mcp_manager, "is_available", lambda: True)
    monkeypatch.setattr(mcp_manager, "resolve", lambda name: ("server", "tool"))
    monkeypatch.setattr(mcp_manager, "discovered_tools", list)
    monkeypatch.setattr(mcp_manager, "retrieval_aliases", lambda name: ["drive", "диск"])
    monkeypatch.setattr(mcp_manager, "tool_result_limit", lambda name: 30_000)

    metadata = resolve_tool_runtime_metadata(
        "mcp__server__tool",
        make_catalog(
            [
                McpToolProvider(
                    servers=[SimpleNamespace(id="server")],
                    server_ids_reader=StubMcpServerIdsReader(["server"]),
                    auth_dependencies=null_egress_auth_dependencies(),
                )
            ]
        ),
        scope=TEST_SCOPE,
    )
    assert metadata.retrieval_aliases == ("drive", "диск")
    assert metadata.result_limit == 30_000


def test_unknown_tool_gets_defaults():
    metadata = resolve_tool_runtime_metadata("definitely_not_a_tool", scope=TEST_SCOPE)
    assert metadata == ResolvedToolRuntimeMetadata(
        stable_name="definitely_not_a_tool",
        retrieval_aliases=(),
        result_limit=15_000,
        timeout_sec=60,
        parallel_safe=False,
        log_redacted_args=frozenset(),
    )


def test_resolver_never_raises_on_garbage():
    class Unstringable:
        def __str__(self):
            raise RuntimeError("boom")

    for garbage in (None, "", "   ", 123, b"bytes", Unstringable(), "имя с пробелами", "a" * 500):
        metadata = resolve_tool_runtime_metadata(garbage, scope=TEST_SCOPE)  # type: ignore[arg-type]
        assert isinstance(metadata, ResolvedToolRuntimeMetadata)


def test_resolver_internal_failure_fails_closed_for_extension_shape(monkeypatch, tmp_path):
    import ouroboros.tools.runtime_metadata as rm
    from ouroboros.skills.extensions import loader as extension_loader

    surface_name, _drive_root, catalog = load_metadata_extension(tmp_path)
    try:

        def _boom(name, descriptor):
            raise RuntimeError("metadata explosion")

        monkeypatch.setattr(rm, "_from_descriptor", _boom)
        metadata = resolve_tool_runtime_metadata(surface_name, catalog, scope=TEST_SCOPE)
        assert metadata.log_redacted_args is None
        assert metadata.result_limit == 15_000
    finally:
        extension_loader.unload_extension(METADATA_EXTENSION)


def test_metadata_snapshot_is_frozen():
    metadata = resolve_tool_runtime_metadata("data_read", scope=TEST_SCOPE)
    with pytest.raises(dataclasses.FrozenInstanceError):
        metadata.result_limit = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Parallel gate through the snapshot
# ---------------------------------------------------------------------------


def _call(name: str, call_id: str, args: dict | None = None) -> ToolCall:
    return ToolCall(id=call_id, function=ToolCallFunction(name=name, arguments=json.dumps(args or {})))


def _overlap_probe():
    """A handler that reports whether a second call entered while the first was inside.

    Overlap is the only observable difference between the batched and the serial path —
    both return the same results in the same order — so it is what the gate is asserted on.
    """
    started = threading.Barrier(2, timeout=5)
    overlapped = threading.Event()

    def handler(ctx, **kwargs):
        try:
            started.wait()
            overlapped.set()
        except threading.BrokenBarrierError:
            pass
        return "ok"

    return handler, overlapped


def test_two_parallel_safe_calls_overlap(tmp_path, monkeypatch):
    from ouroboros.agent.loop_adapters import build_tool_call_executor
    from ouroboros.policy import safety

    monkeypatch.setattr(safety, "check_safety", permissive_safety)
    handler, overlapped = _overlap_probe()
    catalog = make_catalog(
        [
            static_tool_provider(
                ToolEntry(
                    name="data_read",
                    schema=ToolSchema(name="data_read", description="d", parameters={}),
                    handler=handler,
                )
            )
        ]
    )
    ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path)

    assert resolve_tool_runtime_metadata("data_read", catalog, scope=TEST_SCOPE).parallel_safe is True
    executor = build_tool_call_executor(
        confirmation_gate=False,
        catalog=catalog,
        events=collecting_event_sink(),
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    results = executor.execute(
        [_call("data_read", "call-1"), _call("data_read", "call-2")],
        ctx=ctx,
        task_id="task-parallel",
        decisions={},
    )

    assert [r["result"] for r in results] == ["ok", "ok"]
    assert overlapped.is_set(), "two parallel_safe calls must be allowed to overlap"


def test_mixed_batch_with_non_parallel_tool_runs_serially(tmp_path, monkeypatch):
    from ouroboros.agent.loop_adapters import build_tool_call_executor
    from ouroboros.policy import safety

    monkeypatch.setattr(safety, "check_safety", permissive_safety)
    handler, overlapped = _overlap_probe()
    entries = [
        ToolEntry(
            name=name,
            schema=ToolSchema(name=name, description="d", parameters={}),
            handler=handler,
        )
        for name in ("data_read", "data_write")
    ]
    catalog = make_catalog([static_tool_provider(*entries)])
    ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path)

    # data_write is not parallel-safe, so the whole batch loses the parallel branch.
    assert resolve_tool_runtime_metadata("data_write", catalog, scope=TEST_SCOPE).parallel_safe is False
    executor = build_tool_call_executor(
        confirmation_gate=False,
        catalog=catalog,
        events=collecting_event_sink(),
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    results = executor.execute(
        [_call("data_read", "call-1"), _call("data_write", "call-2")],
        ctx=ctx,
        task_id="task-serial",
        decisions={},
    )

    assert len(results) == 2
    assert not overlapped.is_set(), "a batch containing a non-parallel tool must run serially"


# ---------------------------------------------------------------------------
# Retrieval aliases -> search terms
# ---------------------------------------------------------------------------


def test_retrieval_aliases_and_stable_name_reach_search_terms():
    from ouroboros.agent.working_retrieval import build_tool_invocation

    metadata = ResolvedToolRuntimeMetadata(
        stable_name="demo-skill.demo_search",
        retrieval_aliases=("demo_search", "demo", "демо"),
        result_limit=30_000,
        timeout_sec=120,
        parallel_safe=True,
        log_redacted_args=frozenset(),
    )
    execution = {
        "tool_call_id": "call-1",
        "fn_name": "ext_1_r_demo-skill_demo_search",
        "result": "ok",
        "is_error": False,
        "args_for_log": {},
        "result_meta": {"status": "ok"},
        "tool_metadata": metadata,
    }
    invocation = build_tool_invocation(
        execution,
        task_id="task-1",
        scope=TEST_SCOPE,
        retrieval_aliases=(*metadata.retrieval_aliases, metadata.stable_name),
    )
    assert "demo" in invocation.search_terms
    assert "демо" in invocation.search_terms
    # Keying never changes: tool_name stays the surface name.
    assert invocation.tool_name == "ext_1_r_demo-skill_demo_search"


def test_build_tool_invocation_defaults_keep_prior_shape():
    from ouroboros.agent.working_retrieval import build_tool_invocation

    invocation = build_tool_invocation(
        {"tool_call_id": "call-1", "fn_name": "data_read", "result": "ok", "args_for_log": {}},
        task_id="task-1",
        scope=TEST_SCOPE,
    )
    assert invocation.tool_name == "data_read"
    assert "навигатор" not in invocation.search_terms
