from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace
from typing import Any

from new_core.tools.interfaces import IToolProvider, ToolDescriptor
from new_core.tools.schema import ToolSchema
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.egress_auth import null_egress_auth_dependencies
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.factories import permissive_safety
from tests.helpers.shared import (
    TEST_SCOPE,
    make_catalog,
    make_empty_skill_surface_factory,
    make_tool_ctx,
    make_tool_env,
    run_tool,
)
from tests.helpers.user_settings import StubMcpServerIdsReader

EXPECTED_INITIAL_HEADLESS_TOOLS = {
    "attachment_read",
    "calendar_resolve",
    "cancel_task",
    "chat_history",
    "check_skills",
    "data_list",
    "data_read",
    "data_write",
    "enable_tools",
    "get_task_result",
    "gigasearch",
    "list_available_tools",
    "list_skills",
    "memory_delete",
    "memory_list",
    "memory_read",
    "memory_save",
    "recent_tasks",
    "schedule_task",
    "send_photo",
    "send_image",
    "send_file",
    "send_user_message",
    "skill_read",
    "update_scratchpad",
    "wait_for_task",
}


def _tool_env(tmp_path: pathlib.Path):
    from new_core.tools.providers import ExtensionToolProvider

    catalog, ctx = make_tool_env(tmp_path / "repo", tmp_path / "data")
    for provider in catalog.providers:
        if isinstance(provider, ExtensionToolProvider):
            provider._record_is_available = lambda *_args, **_kwargs: True  # type: ignore[method-assign]
    return catalog, ctx


def test_initial_headless_tool_surface_stays_minimal(tmp_path):
    from ouroboros.agent.task_graph import bind_tool_surface
    from ouroboros.policy.safety import TOOL_POLICY

    catalog, ctx = _tool_env(tmp_path)
    names = set(bind_tool_surface(catalog, ctx, [])[1])

    assert names == EXPECTED_INITIAL_HEADLESS_TOOLS
    assert {name: TOOL_POLICY[name] for name in names} == dict.fromkeys(names, "skip")


def test_legacy_memory_registry_tools_are_not_registered(tmp_path):
    catalog, _ctx = _tool_env(tmp_path)

    assert catalog.find("memory_map", TEST_SCOPE) is None
    assert catalog.find("memory_update_registry", TEST_SCOPE) is None
    assert "memory_map" not in [d.name for d in catalog.descriptors(TEST_SCOPE) if d.surface == "core"]
    assert "memory_update_registry" not in [d.name for d in catalog.descriptors(TEST_SCOPE) if d.surface == "core"]


def test_file_tool_contracts_describe_runtime_artifacts(tmp_path):
    catalog, _ctx = _tool_env(tmp_path)

    data_write = catalog.find("data_write", TEST_SCOPE).schema
    send_file = catalog.find("send_file", TEST_SCOPE).schema
    write_contract = f"{data_write.description} {data_write.parameters}".lower()
    send_contract = f"{send_file.description} {send_file.parameters}".lower()

    assert all(term in write_contract for term in ("runtime data storage", "relative", "uploads/<filename>"))
    assert "all files must use uploads" not in write_contract
    assert all(term in send_contract for term in ("current task workspace", "uploads/... path from this chat"))


def test_chat_history_schema_has_no_text_search(tmp_path):
    catalog, _ctx = _tool_env(tmp_path)

    desc = catalog.find("chat_history", TEST_SCOPE)

    assert desc is not None
    schema = desc.schema
    properties = schema.parameters["properties"]
    assert set(properties) == {"count", "offset", "date_from", "date_to"}
    assert "search" not in schema.description.lower()


def test_tool_capability_views_are_derived_from_catalog():
    from ouroboros.policy import capabilities
    from ouroboros.policy.safety import TOOL_POLICY
    from ouroboros.tools.catalog import (
        core_tool_names,
        read_only_parallel_tool_names,
        reviewed_mutative_tool_names,
        safety_policy_by_tool_name,
        tool_result_limits,
        untruncated_tool_names,
    )

    assert core_tool_names() == capabilities.CORE_TOOL_NAMES
    assert read_only_parallel_tool_names() == capabilities.READ_ONLY_PARALLEL_TOOLS
    assert untruncated_tool_names() == capabilities.UNTRUNCATED_TOOL_RESULTS
    assert tool_result_limits() == capabilities.TOOL_RESULT_LIMITS
    assert reviewed_mutative_tool_names() == capabilities.REVIEWED_MUTATIVE_TOOLS
    assert safety_policy_by_tool_name() == TOOL_POLICY


def test_discoverable_tool_is_not_initial_but_can_be_enabled_for_current_task(tmp_path, monkeypatch):
    from ouroboros.agent.task_graph import bind_tool_surface

    monkeypatch.delenv("TOOL_CALCULATOR_ENABLED", raising=False)
    monkeypatch.delenv("OUROBOROS_ENABLED_TOOLS", raising=False)
    catalog, ctx = _tool_env(tmp_path)
    messages: list[dict] = []

    active_schemas, _active_names = bind_tool_surface(catalog, ctx, messages)

    assert "calculator" not in {schema.name for schema in active_schemas}
    assert catalog.find("calculator", TEST_SCOPE) is not None

    result = run_tool(catalog, ctx, "enable_tools", {"tools": "calculator"})

    assert "Enabled: calculator" in result
    # The intent is recorded here; the apply_overrides node puts it on the surface.
    assert "calculator" in ctx.tool_surface.active_names()
    assert dict(ctx.tool_surface.drain_pending_enables())["calculator"].name == "calculator"
    assert run_tool(catalog, ctx, "calculator", {"expression": "2 + 2 * 3"}) == "8"


def test_dynamic_tools_note_merges_into_first_system_message(tmp_path, monkeypatch):
    from ouroboros.agent.task_graph import bind_tool_surface

    monkeypatch.delenv("TOOL_CALCULATOR_ENABLED", raising=False)
    monkeypatch.delenv("OUROBOROS_ENABLED_TOOLS", raising=False)
    catalog, ctx = _tool_env(tmp_path)
    messages: list[dict] = [
        {"role": "system", "content": "base system"},
        {"role": "user", "content": "do the task"},
    ]

    bind_tool_surface(catalog, ctx, messages)

    assert [message["role"] for message in messages].count("system") == 1
    assert messages[0]["role"] == "system"
    assert messages[0]["content"].startswith("base system")
    assert "list_available_tools" in messages[0]["content"]
    assert "enable_tools" in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "do the task"}


def test_dynamic_tools_note_counts_only_the_scoped_projection(tmp_path) -> None:
    from ouroboros.agent.task_graph import bind_tool_surface

    class _Provider(IToolProvider):
        def __init__(self, name: str, *, owner_id: str | None = None) -> None:
            self._owner_id = owner_id
            self._descriptor = ToolDescriptor(
                schema=ToolSchema(name=name, description="d", parameters={}),
                timeout_sec=1,
                origin="mcp:private" if owner_id else "builtin",
                surface="discoverable",
                capability_class="read_only",
                safety_policy="skip",
            )

        def refresh(self) -> None:
            return None

        def descriptors(self, scope):
            return [self._descriptor] if self._owner_id is None or scope.owner_id == self._owner_id else []

        def get(self, name: str, scope):
            return self._descriptor if name == self._descriptor.name and self.descriptors(scope) else None

        def refuse_dispatch(self, name: str, scope):
            return None

        def execute(self, ctx: Any, name: str, args: dict[str, Any]) -> str:
            return "ok"

    ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path)
    catalog = make_catalog(
        [
            _Provider("calculator"),
            _Provider("mcp__private__search", owner_id="bob"),
        ]
    )
    messages = [{"role": "system", "content": "base"}]

    bind_tool_surface(catalog, ctx, messages)

    assert "There are 1 additional tools available" in messages[0]["content"]
    assert [descriptor.name for descriptor in ctx.tool_surface.catalog()] == ["calculator"]


def test_dynamic_tools_note_preserves_multipart_system_content(tmp_path, monkeypatch):
    from ouroboros.agent.task_graph import bind_tool_surface

    monkeypatch.delenv("TOOL_CALCULATOR_ENABLED", raising=False)
    monkeypatch.delenv("OUROBOROS_ENABLED_TOOLS", raising=False)
    catalog, ctx = _tool_env(tmp_path)
    messages: list[dict] = [
        {"role": "system", "content": [{"type": "text", "text": "base system"}]},
        {"role": "user", "content": "do the task"},
    ]

    bind_tool_surface(catalog, ctx, messages)

    assert [message["role"] for message in messages].count("system") == 1
    content = messages[0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "base system"}
    assert content[-1]["type"] == "text"
    assert "enable_tools" in content[-1]["text"]


def test_discoverable_tool_call_requires_active_tool_set(tmp_path, monkeypatch):
    from new_core.llm.types import ToolCall, ToolCallFunction
    from ouroboros.agent.task_graph import bind_tool_surface
    from tests.helpers.shared import run_tool_calls

    monkeypatch.delenv("TOOL_CALCULATOR_ENABLED", raising=False)
    monkeypatch.delenv("OUROBOROS_ENABLED_TOOLS", raising=False)
    catalog, ctx = _tool_env(tmp_path)
    tool_schemas, active_names = bind_tool_surface(catalog, ctx, [])

    call = ToolCall(
        id="call-calculator",
        function=ToolCallFunction(name="calculator", arguments=json.dumps({"expression": "2 + 2"})),
    )
    messages: list[dict] = []
    trace = {"tool_calls": []}

    errors = run_tool_calls(
        [call],
        catalog,
        tmp_path / "logs",
        "task-1",
        messages,
        trace,
        lambda _text: None,
        ctx=ctx,
        active_tool_names=active_names,
    )

    assert errors == 1
    assert "TOOL_NOT_ENABLED" in messages[-1]["content"]

    assert "Enabled: calculator" in run_tool(catalog, ctx, "enable_tools", {"tools": "calculator"})
    # The node applies the drained intent; here the test plays that part.
    for name, schema in ctx.tool_surface.drain_pending_enables():
        tool_schemas.append(schema)
        active_names.add(name)
    messages.clear()
    trace["tool_calls"].clear()

    errors = run_tool_calls(
        [call],
        catalog,
        tmp_path / "logs",
        "task-1",
        messages,
        trace,
        lambda _text: None,
        ctx=ctx,
        active_tool_names=active_names,
    )

    assert errors == 0
    assert messages[-1]["content"] == "4"


def test_extension_tool_call_requires_active_tool_set(tmp_path, monkeypatch):
    from new_core.llm.types import ToolCall, ToolCallFunction
    from ouroboros.policy import safety
    from ouroboros.skills.extensions import loader as extension_loader
    from tests.helpers.shared import run_tool_calls

    catalog, ctx = _tool_env(tmp_path)
    tool_name = extension_loader.extension_surface_name("demo_ext", "ping")
    calls: list[dict] = []
    with extension_loader._lock:
        extension_loader._tools[tool_name] = {
            "name": tool_name,
            "description": "Ping extension",
            "schema": {"type": "object", "properties": {}},
            "skill": "demo_ext",
            "skill_id": "distribution:demo_ext",
            "handler": lambda ctx, **kwargs: calls.append(kwargs) or "pong",
        }
    monkeypatch.setattr(extension_loader, "is_extension_live", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(safety, "check_safety", permissive_safety)
    try:
        call = ToolCall(
            id="call-extension",
            function=ToolCallFunction(name=tool_name, arguments="{}"),
        )
        messages: list[dict] = []
        trace = {"tool_calls": []}

        errors = run_tool_calls(
            [call],
            catalog,
            tmp_path / "logs",
            "task-1",
            messages,
            trace,
            lambda _text: None,
            ctx=ctx,
            active_tool_names=set(),
        )

        assert errors == 1
        assert "TOOL_NOT_ENABLED" in messages[-1]["content"]
        assert calls == []

        messages.clear()
        trace["tool_calls"].clear()
        errors = run_tool_calls(
            [call],
            catalog,
            tmp_path / "logs",
            "task-1",
            messages,
            trace,
            lambda _text: None,
            ctx=ctx,
            active_tool_names={tool_name},
        )

        assert errors == 0
        assert messages[-1]["content"] == "pong"
        assert calls == [{}]
    finally:
        with extension_loader._lock:
            extension_loader._tools.pop(tool_name, None)


def test_mcp_tool_call_requires_active_tool_set(tmp_path, monkeypatch):
    import asyncio
    import threading

    from new_core.llm.types import ToolCall, ToolCallFunction
    from new_core.tools.providers import McpToolProvider
    from new_core.tools.schema import ToolSchema
    from ouroboros.app import config as app_config
    from ouroboros.mcp.connection import DiscoveredMCPTool
    from ouroboros.mcp.manager import mcp_manager
    from ouroboros.policy import safety
    from tests.helpers.shared import run_tool_calls

    _, ctx = _tool_env(tmp_path)
    tool_name = "mcp__demo__ping"
    catalog = make_catalog(
        [
            McpToolProvider(
                servers=[SimpleNamespace(id="demo")],
                server_ids_reader=StubMcpServerIdsReader(["demo"]),
                auth_dependencies=null_egress_auth_dependencies(),
            )
        ]
    )

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    class _FakeConnection:
        def __init__(self):
            self.calls: list[dict] = []

        async def call_tool(self, scope, name, arguments, timeout_sec):
            self.calls.append(arguments)
            return "pong"

    fake = _FakeConnection()
    mcp_manager._loop = loop
    mcp_manager._connections["demo"] = fake
    mcp_manager._tool_index[tool_name] = DiscoveredMCPTool(
        server_id="demo",
        original_name="ping",
        full_name=tool_name,
        schema=ToolSchema(name=tool_name, description="Ping", parameters={"type": "object", "properties": {}}),
    )
    mcp_manager._initialized = True
    monkeypatch.setattr(safety, "check_safety", permissive_safety)
    monkeypatch.setattr(app_config, "get_mcp_enabled", lambda: True)

    try:
        call = ToolCall(id="call-mcp", function=ToolCallFunction(name=tool_name, arguments="{}"))
        messages: list[dict] = []
        trace = {"tool_calls": []}

        errors = run_tool_calls(
            [call],
            catalog,
            tmp_path / "logs",
            "task-1",
            messages,
            trace,
            lambda _text: None,
            ctx=ctx,
            active_tool_names=set(),
        )

        assert errors == 1
        assert "TOOL_NOT_ENABLED" in messages[-1]["content"]
        assert fake.calls == []

        messages.clear()
        trace["tool_calls"].clear()
        errors = run_tool_calls(
            [call],
            catalog,
            tmp_path / "logs",
            "task-1",
            messages,
            trace,
            lambda _text: None,
            ctx=ctx,
            active_tool_names={tool_name},
        )

        assert errors == 0
        assert messages[-1]["content"] == "pong"
        assert fake.calls == [{}]
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def test_mcp_tool_dispatch_runs_safety_check_and_uses_mcp_timeout(tmp_path, monkeypatch):
    import asyncio
    import threading

    from new_core.tools.providers import McpToolProvider
    from new_core.tools.schema import ToolSchema
    from ouroboros.app import config as app_config
    from ouroboros.mcp.connection import DiscoveredMCPTool
    from ouroboros.mcp.manager import mcp_manager
    from ouroboros.policy import safety

    _, ctx = _tool_env(tmp_path)
    tool_name = "mcp__demo__ping"
    catalog = make_catalog(
        [
            McpToolProvider(
                servers=[SimpleNamespace(id="demo")],
                server_ids_reader=StubMcpServerIdsReader(["demo"]),
                auth_dependencies=null_egress_auth_dependencies(),
            )
        ]
    )

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    class _FakeConnection:
        async def call_tool(self, scope, name, arguments, timeout_sec):
            return f"pong:{timeout_sec}"

    mcp_manager._loop = loop
    mcp_manager._connections["demo"] = _FakeConnection()
    mcp_manager._tool_index[tool_name] = DiscoveredMCPTool(
        server_id="demo",
        original_name="ping",
        full_name=tool_name,
        schema=ToolSchema(name=tool_name, description="Ping", parameters={"type": "object", "properties": {}}),
    )
    mcp_manager._initialized = True

    safety_calls: list[str] = []

    def _fake_check_safety(name, args, **kwargs):
        safety_calls.append(name)
        return True, ""

    monkeypatch.setattr(safety, "check_safety", _fake_check_safety)
    monkeypatch.setattr(app_config, "get_mcp_tool_timeout_sec", lambda: 42)
    monkeypatch.setattr(app_config, "get_mcp_enabled", lambda: True)

    try:
        assert catalog.find(tool_name, TEST_SCOPE).timeout_sec == 42
        result = run_tool(catalog, ctx, tool_name, {})
        assert result == "pong:42"
        assert safety_calls == []
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def test_empty_tool_call_arguments_are_normalized_for_history(tmp_path, monkeypatch):
    from new_core.llm.types import ToolCall, ToolCallFunction
    from ouroboros.policy import safety
    from ouroboros.skills.extensions import loader as extension_loader
    from tests.helpers.shared import run_tool_calls

    catalog, ctx = _tool_env(tmp_path)
    tool_name = extension_loader.extension_surface_name("demo_ext", "empty_args")
    calls: list[dict] = []
    with extension_loader._lock:
        extension_loader._tools[tool_name] = {
            "name": tool_name,
            "description": "Records received kwargs",
            "schema": {"type": "object", "properties": {}},
            "skill": "demo_ext",
            "skill_id": "distribution:demo_ext",
            "handler": lambda ctx, **kwargs: calls.append(kwargs) or "ok",
        }
    monkeypatch.setattr(extension_loader, "is_extension_live", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(safety, "check_safety", permissive_safety)
    try:
        call = ToolCall(
            id="call-empty",
            function=ToolCallFunction(name=tool_name, arguments=""),
        )
        messages: list[dict] = []
        trace = {"tool_calls": []}

        errors = run_tool_calls(
            [call],
            catalog,
            tmp_path / "logs",
            "task-1",
            messages,
            trace,
            lambda _text: None,
            ctx=ctx,
            active_tool_names={tool_name},
        )

        assert errors == 0
        assert calls == [{}]
        assert messages[-1]["content"] == "ok"
    finally:
        with extension_loader._lock:
            extension_loader._tools.pop(tool_name, None)


def test_non_object_tool_call_arguments_fail_before_execution(tmp_path, monkeypatch):
    from new_core.llm.types import ToolCall, ToolCallFunction
    from ouroboros.policy import safety
    from ouroboros.skills.extensions import loader as extension_loader
    from tests.helpers.shared import run_tool_calls

    catalog, ctx = _tool_env(tmp_path)
    tool_name = extension_loader.extension_surface_name("demo_ext", "bad_args")
    calls: list[dict] = []
    with extension_loader._lock:
        extension_loader._tools[tool_name] = {
            "name": tool_name,
            "description": "Should not be called with malformed args",
            "schema": {"type": "object", "properties": {}},
            "skill": "demo_ext",
            "skill_id": "distribution:demo_ext",
            "handler": lambda ctx, **kwargs: calls.append(kwargs) or "unexpected",
        }
    monkeypatch.setattr(extension_loader, "is_extension_live", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(safety, "check_safety", permissive_safety)
    try:
        cases = (
            ("{", {tool_name}, "TOOL_ARG_ERROR"),
            ('[["path", "x"]]', {tool_name}, "TOOL_ARG_ERROR"),
            ("{", set(), "TOOL_NOT_ENABLED"),
        )
        for arguments, active_names, expected_error in cases:
            call = ToolCall(id="call-bad", function=ToolCallFunction(name=tool_name, arguments=arguments))
            messages: list[dict] = []
            trace = {"tool_calls": []}
            errors = run_tool_calls(
                [call],
                catalog,
                tmp_path / "logs",
                "task-1",
                messages,
                trace,
                lambda _text: None,
                ctx=ctx,
                active_tool_names=active_names,
            )

            assert errors == 1
            assert expected_error in messages[-1]["content"]
            assert "args_hash" not in trace["tool_calls"][-1]
            assert calls == []
    finally:
        with extension_loader._lock:
            extension_loader._tools.pop(tool_name, None)


def test_enabled_tools_env_promotes_discoverable_tools_to_initial_surface(tmp_path, monkeypatch):
    from ouroboros.agent.task_graph import bind_tool_surface

    monkeypatch.setenv("OUROBOROS_ENABLED_TOOLS", "calculator, unknown_tool, browse_page, browser_action, a2a_send")
    from tests.conftest import refresh_test_config

    refresh_test_config()
    catalog, ctx = _tool_env(tmp_path)
    names = set(bind_tool_surface(catalog, ctx, [])[1])

    assert "calculator" in names
    assert "browse_page" in names
    assert "browser_action" in names
    assert "unknown_tool" not in names
    assert "a2a_send" not in names
    assert run_tool(catalog, ctx, "calculator", {"expression": "2 + 2 * 3"}) == "8"


def test_browser_tools_are_discoverable_but_not_initial_by_default(tmp_path, monkeypatch):
    from ouroboros.agent.task_graph import bind_tool_surface

    monkeypatch.delenv("OUROBOROS_ENABLED_TOOLS", raising=False)
    from tests.conftest import refresh_test_config

    refresh_test_config()
    catalog, ctx = _tool_env(tmp_path)
    initial_names = set(bind_tool_surface(catalog, ctx, [])[1])
    discoverable_names = {d.name for d in catalog.descriptors(TEST_SCOPE) if d.surface == "discoverable"}

    assert {"browse_page", "browser_action"}.isdisjoint(initial_names)
    assert {"browse_page", "browser_action"}.issubset(discoverable_names)
    assert catalog.find("browse_page", TEST_SCOPE) is not None
    assert catalog.find("browser_action", TEST_SCOPE) is not None


def test_vlm_query_is_initial_only_for_image_tasks_in_server_profile(tmp_path, monkeypatch):
    from ouroboros.agent.task_graph import bind_tool_surface

    monkeypatch.setenv("OUROBOROS_ENV", "production")
    monkeypatch.setenv("OUROBOROS_ENABLED_TOOLS", "vlm_query")
    from tests.conftest import refresh_test_config

    refresh_test_config()
    catalog, ctx = _tool_env(tmp_path)

    initial_names = set(bind_tool_surface(catalog, ctx, [])[1])
    assert "vlm_query" not in initial_names
    assert "vlm_query" in {d.name for d in catalog.descriptors(TEST_SCOPE) if d.surface == "discoverable"}
    assert catalog.find("vlm_query", TEST_SCOPE) is not None

    ctx.attachments = [{"id": "image-1", "kind": "image", "mime": "image/png"}]

    image_task_names = set(bind_tool_surface(catalog, ctx, [])[1])
    assert "vlm_query" in image_task_names


def test_system_prompt_does_not_advertise_unavailable_web_browser_vision_tools():
    system_prompt = pathlib.Path("prompts/SYSTEM.md").read_text(encoding="utf-8")

    assert "### Web and browser" not in system_prompt
    assert "web/browser/vision" not in system_prompt


def test_system_prompt_routes_file_delivery_through_send_tools():
    system_prompt = pathlib.Path("prompts/SYSTEM.md").read_text(encoding="utf-8")

    assert "`send_file`" in system_prompt
    assert "`send_image`" in system_prompt
    assert "attachment of the current conversation" in system_prompt


def test_legacy_calculator_env_still_promotes_calculator_to_initial_surface(tmp_path, monkeypatch):
    from ouroboros.agent.task_graph import bind_tool_surface

    monkeypatch.delenv("OUROBOROS_ENABLED_TOOLS", raising=False)
    monkeypatch.setenv("TOOL_CALCULATOR_ENABLED", "true")
    from tests.conftest import refresh_test_config

    refresh_test_config()
    catalog, ctx = _tool_env(tmp_path)

    assert "calculator" in set(bind_tool_surface(catalog, ctx, [])[1])


def test_removed_tool_surfaces_are_absent_from_registry_and_catalog(tmp_path):
    from ouroboros.agent.task_graph import bind_tool_surface
    from ouroboros.tools.catalog import TOOL_SPECS

    catalog, ctx = _tool_env(tmp_path)
    initial_names = set(bind_tool_surface(catalog, ctx, [])[1])
    discoverable_names = {d.name for d in catalog.descriptors(TEST_SCOPE) if d.surface == "discoverable"}
    registered_names = set(catalog.local_entries())

    removed = {
        "a2a_discover",
        "a2a_send",
        "a2a_status",
        "analyze_screenshot",
        "forward_to_worker",
        "set_tool_timeout",
        "toggle_skill",
        "update_identity",
        "web_search",
    }
    assert removed.isdisjoint(TOOL_SPECS)
    assert removed.isdisjoint(registered_names)
    for tool_name in removed:
        assert catalog.find(tool_name, TEST_SCOPE) is None
        assert tool_name not in initial_names
        assert tool_name not in discoverable_names
        assert "Unknown tool" in run_tool(catalog, ctx, tool_name, {})


def test_live_extension_tool_schema_is_executable(tmp_path, monkeypatch):
    from ouroboros.agent.task_graph import bind_tool_surface
    from ouroboros.policy import safety
    from ouroboros.skills.extensions import loader as extension_loader
    from tests.conftest import refresh_test_config

    monkeypatch.setenv("OUROBOROS_ENABLED_TOOLS", "demo_ext.ping")
    refresh_test_config()
    catalog, ctx = _tool_env(tmp_path)
    tool_name = extension_loader.extension_surface_name("demo_ext", "ping")
    calls: list[dict] = []
    with extension_loader._lock:
        extension_loader._tools[tool_name] = {
            "name": tool_name,
            "description": "Ping extension",
            "schema": {"type": "object", "properties": {}},
            "skill": "demo_ext",
            "skill_id": "distribution:demo_ext",
            "handler": lambda ctx, **kwargs: calls.append(kwargs) or "pong",
        }
    monkeypatch.setattr(extension_loader, "is_extension_live", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(safety, "check_safety", permissive_safety)
    try:
        names = set(bind_tool_surface(catalog, ctx, [])[1])
        assert tool_name in names
        assert run_tool(catalog, ctx, tool_name, {}) == "pong"
        assert calls == [{}]
    finally:
        with extension_loader._lock:
            extension_loader._tools.pop(tool_name, None)


def test_extension_tool_timeout_defaults_to_300_and_respects_manifest_override(tmp_path, monkeypatch):
    from ouroboros.skills.extensions import loader as extension_loader

    catalog, _ctx = _tool_env(tmp_path)
    # As every other extension test here does: the records are injected straight into the
    # loader with no skill on disk, so liveness has to be stubbed for them to resolve.
    monkeypatch.setattr(extension_loader, "is_extension_live", lambda *_args, **_kwargs: True)
    default_tool = extension_loader.extension_surface_name("demo_ext", "default_timeout")
    explicit_tool = extension_loader.extension_surface_name("demo_ext", "explicit_timeout")
    invalid_tool = extension_loader.extension_surface_name("demo_ext", "invalid_timeout")
    with extension_loader._lock:
        extension_loader._tools[default_tool] = {
            "name": default_tool,
            "description": "Default timeout extension",
            "schema": {"type": "object", "properties": {}},
            "skill": "demo_ext",
            "skill_id": "distribution:demo_ext",
            "handler": lambda ctx, **kwargs: "ok",
        }
        extension_loader._tools[explicit_tool] = {
            "name": explicit_tool,
            "description": "Explicit timeout extension",
            "schema": {"type": "object", "properties": {}},
            "skill": "demo_ext",
            "skill_id": "distribution:demo_ext",
            "handler": lambda ctx, **kwargs: "ok",
            "timeout_sec": 45,
        }
        extension_loader._tools[invalid_tool] = {
            "name": invalid_tool,
            "description": "Invalid timeout extension",
            "schema": {"type": "object", "properties": {}},
            "skill": "demo_ext",
            "skill_id": "distribution:demo_ext",
            "handler": lambda ctx, **kwargs: "ok",
            "timeout_sec": "not-an-int",
        }
    try:
        assert catalog.find(default_tool, TEST_SCOPE).timeout_sec == 300
        assert catalog.find(explicit_tool, TEST_SCOPE).timeout_sec == 45
        assert catalog.find(invalid_tool, TEST_SCOPE).timeout_sec == 300
    finally:
        with extension_loader._lock:
            extension_loader._tools.pop(default_tool, None)
            extension_loader._tools.pop(explicit_tool, None)
            extension_loader._tools.pop(invalid_tool, None)


def test_async_extension_tool_dispatch_uses_300_second_default_timeout(tmp_path, monkeypatch):
    import asyncio

    from ouroboros.policy import safety
    from ouroboros.skills.extensions import loader as extension_loader

    catalog, ctx = _tool_env(tmp_path)
    tool_name = extension_loader.extension_surface_name("demo_ext", "async_ping")
    captured_timeouts: list[int | float | None] = []

    async def handler(ctx, **kwargs):
        return "async-pong"

    async def wait_for_spy(awaitable, timeout=None):
        captured_timeouts.append(timeout)
        return await awaitable

    with extension_loader._lock:
        extension_loader._tools[tool_name] = {
            "name": tool_name,
            "description": "Async ping extension",
            "schema": {"type": "object", "properties": {}},
            "skill": "demo_ext",
            "skill_id": "distribution:demo_ext",
            "handler": handler,
        }
    monkeypatch.setattr(extension_loader, "is_extension_live", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(safety, "check_safety", permissive_safety)
    monkeypatch.setattr(asyncio, "wait_for", wait_for_spy)
    try:
        assert run_tool(catalog, ctx, tool_name, {}) == "async-pong"
        assert captured_timeouts == [300]
    finally:
        with extension_loader._lock:
            extension_loader._tools.pop(tool_name, None)


def test_safety_prompt_is_loaded_from_repo_prompt_file():
    from ouroboros.policy.safety import _get_safety_prompt

    prompt = _get_safety_prompt()

    assert "Three verdicts:" in prompt
    assert "Modifying or deleting safety-critical runtime paths" in prompt


def test_data_write_blocks_managed_memory_paths(tmp_path):
    catalog, ctx = _tool_env(tmp_path)

    result = run_tool(
        catalog,
        ctx,
        "data_write",
        {"path": "memory/project_notes.md", "content": "Use this as memory.\n"},
    )

    assert "DATA_WRITE_BLOCKED" in result
    assert "memory_save" in result


def test_data_write_records_agent_generated_provenance_and_read_warns(tmp_path):
    from ouroboros.storage.blob.factory import get_storage_provider
    from ouroboros.storage.blob.scopes import SCOPE_UPLOADS

    catalog, ctx = _tool_env(tmp_path)
    ctx.task_id = "artifact-task"

    result = run_tool(
        catalog,
        ctx,
        "data_write",
        {"path": "uploads/note.md", "content": "temporary self note\n"},
    )
    assert result.startswith("OK:")

    from ouroboros.tools.service.artifacts import resolve_artifact

    metadata = resolve_artifact(ctx, "uploads/note.md")
    assert metadata["source"] == "agent_generated"

    read_result = run_tool(catalog, ctx, "data_read", {"path": "uploads/note.md"})
    assert "DATA_PROVENANCE_WARNING" in read_result
    assert "temporary self note" in read_result
    forged = run_tool(
        catalog,
        ctx,
        "data_write",
        {"path": "uploads/report.txt.metadata.json", "content": '{"task_id": "forged"}'},
    )
    assert "DATA_WRITE_BLOCKED" in forged
    assert not get_storage_provider().exists(ctx.scope, SCOPE_UPLOADS, "report.txt.metadata.json")


def test_data_list_hides_internal_agent_notes_by_default(tmp_path):
    from ouroboros.storage.blob.factory import get_storage_provider
    from ouroboros.storage.blob.scopes import SCOPE_STATE

    catalog, ctx = _tool_env(tmp_path)
    # Written underneath the tool rather than through it: `state` object keys carry no
    # owner, so `data_write` refuses the scope outright now. What is pinned here is that a
    # listing does not surface the notes an earlier release left behind.
    get_storage_provider().put_text(ctx.scope, SCOPE_STATE, "agent_notes/note.md", "temporary self note\n")

    listing = run_tool(catalog, ctx, "data_list", {"dir": "state"})

    assert "agent_notes" not in listing


def test_data_write_refuses_a_scope_whose_keys_carry_no_owner(tmp_path, monkeypatch):
    """Two owners writing one `state/` path would land on one physical key.

    `uploads` is the only owner-keyed scope (`ouroboros/storage/blob/scopes.py`), so a
    generic per-caller write anywhere else is content one owner overwrites for another.
    The second owner exists only under the mode, and so does the refusal.
    """
    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")
    catalog, ctx = _tool_env(tmp_path)

    refused = run_tool(
        catalog,
        ctx,
        "data_write",
        {"path": "state/agent_notes/note.md", "content": "temporary self note\n"},
    )

    assert "DATA_WRITE_BLOCKED" in refused
    assert "carries no owner" in refused
    assert not (tmp_path / "data" / "state" / "agent_notes" / "note.md").exists()


def test_truncate_tool_result_honors_limit_override():
    from ouroboros.agent.loop_tool_execution import ToolCallExecutor

    _truncate_tool_result = ToolCallExecutor._truncate_tool_result

    big = "x" * 40000
    # Without override the default (15k) applies; the override raises it to 30k.
    assert "limit=15000" in _truncate_tool_result(big, "ext_1_r_demo_search")
    capped = _truncate_tool_result(big, "ext_1_r_demo_search", limit_override=30_000)
    assert len(capped.split("\n... (truncated")[0]) == 30_000
    assert "limit=30000" in capped


def test_extension_result_limit_enforced_through_process_results(tmp_path):
    """An extension's declared result_limit=30_000 caps at 30k, not the 15k default.

    Going through a loaded extension (rather than a fabricated ``_tools`` entry) means
    the limit travels register_tool -> descriptor -> metadata snapshot -> process_results.
    """
    from ouroboros.agent.loop_adapters import build_tool_call_executor
    from ouroboros.skills.extensions import loader as extension_loader
    from ouroboros.tools.runtime_metadata import resolve_tool_runtime_metadata
    from tests.helpers.shared import METADATA_EXTENSION, collecting_event_sink, load_metadata_extension

    tool_name, _drive_root, catalog = load_metadata_extension(tmp_path)
    try:
        metadata = resolve_tool_runtime_metadata(tool_name, catalog, scope=TEST_SCOPE)
        assert metadata.result_limit == 30_000
        messages: list[dict] = []
        results = [
            {
                "tool_call_id": "call-1",
                "fn_name": tool_name,
                "result": "x" * 40000,
                "is_error": False,
                "tool_args": {},
                "args_for_log": {},
                "is_code_tool": False,
                "result_meta": {"status": "ok"},
                "tool_metadata": metadata,
            }
        ]
        executor = build_tool_call_executor(
            confirmation_gate=False,
            catalog=catalog,
            events=collecting_event_sink(),
            execution_memory_repository=sql_execution_memory_repository(),
            audit_event_repository=sql_audit_event_repository(),
        )
        errors = executor.process_results(
            results,
            messages=messages,
            trace={"tool_calls": []},
        )
        assert errors == 0
        content = messages[0]["content"]
        body = content.split("\n... (truncated")[0]
        assert len(body) == 30_000
        assert "limit=30000" in content
    finally:
        extension_loader.unload_extension(METADATA_EXTENSION)


def test_pinned_result_limit_override_survives_extension_unload(tmp_path):
    """FIX B: the metadata snapshot is pinned at execute time, so an unload
    before truncation must not drop the declared 30k cap back to the 15k default."""
    from new_core.tools.catalog import ToolCatalog
    from new_core.tools.providers import ExtensionToolProvider
    from ouroboros.agent.loop_adapters import build_tool_call_executor
    from ouroboros.skills.extensions import loader as extension_loader
    from ouroboros.skills.extensions.runtime import ExtensionRuntime
    from ouroboros.tools.runtime_metadata import resolve_tool_runtime_metadata
    from tests.helpers.shared import collecting_event_sink

    catalog = ToolCatalog()
    provider = ExtensionToolProvider(
        skill_surface=make_empty_skill_surface_factory(),
        extension_runtime=ExtensionRuntime(),
    )
    catalog.initialize([provider])
    # The provider filters by liveness against the skills tree, which this fabricated
    # record has no place in; the record itself is what the descriptor is built from.
    monkeypatch_live = provider._record_is_live
    monkeypatch_available = provider._record_is_available
    provider._record_is_live = lambda *args, **kwargs: True  # type: ignore[method-assign]
    provider._record_is_available = lambda *args, **kwargs: True  # type: ignore[method-assign]
    tool_name = extension_loader.extension_surface_name("demo_ext", "big_result")
    with extension_loader._lock:
        extension_loader._tools[tool_name] = {
            "name": tool_name,
            "description": "Big result extension",
            "schema": {"type": "object", "properties": {}},
            "skill": "demo_ext",
            "skill_id": "distribution:demo_ext",
            "handler": lambda ctx, **kwargs: "x" * 40000,
            "result_limit": 30_000,
        }
    try:
        # Pin the snapshot at execute/resolve time (mirrors handle_tool_calls).
        pinned = resolve_tool_runtime_metadata(tool_name, catalog, scope=TEST_SCOPE)
        assert pinned.result_limit == 30_000
    finally:
        with extension_loader._lock:
            extension_loader._tools.pop(tool_name, None)
        provider._record_is_live = monkeypatch_live  # type: ignore[method-assign]
        provider._record_is_available = monkeypatch_available  # type: ignore[method-assign]

    # Extension is now unloaded: a live re-resolve fails closed (15k default).
    assert resolve_tool_runtime_metadata(tool_name, catalog, scope=TEST_SCOPE).result_limit == 15_000

    messages: list[dict] = []
    results = [
        {
            "tool_call_id": "call-1",
            "fn_name": tool_name,
            "result": "x" * 40000,
            "is_error": False,
            "tool_args": {},
            "args_for_log": {},
            "is_code_tool": False,
            "result_meta": {"status": "ok"},
            # Pinned at execute time; process_tool_results must honor this instead
            # of re-resolving against the now-unloaded extension.
            "tool_metadata": pinned,
        }
    ]
    executor = build_tool_call_executor(
        confirmation_gate=False,
        catalog=catalog,
        events=collecting_event_sink(),
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    errors = executor.process_results(
        results,
        messages=messages,
        trace={"tool_calls": []},
    )
    assert errors == 0
    content = messages[0]["content"]
    body = content.split("\n... (truncated")[0]
    assert len(body) == 30_000
    assert "limit=30000" in content


def test_mcp_metadata_carries_configured_limit_and_aliases(monkeypatch):
    """The MCP provider's descriptor absorbs the manager's per-server config lookups."""
    from new_core.tools.providers import McpToolProvider
    from new_core.tools.schema import ToolSchema
    from ouroboros.mcp.connection import DiscoveredMCPTool
    from ouroboros.mcp.manager import mcp_manager
    from ouroboros.storage.settings.schema import McpServerEntry
    from ouroboros.tools.runtime_metadata import resolve_tool_runtime_metadata
    from tests.helpers.shared import make_catalog

    tool_name = "mcp__hr-provider__mcp_query"
    entry = McpServerEntry(
        id="hr-provider",
        url="https://hr.example/mcp",
        retrieval_aliases=["pulse", "пульс"],
        tool_result_limits={"mcp_query": 30_000},
    )

    class _FakeConnection:
        def __init__(self):
            self.entry = entry

    discovered = DiscoveredMCPTool(
        server_id="hr-provider",
        original_name="mcp_query",
        full_name=tool_name,
        schema=ToolSchema(name=tool_name, description="Query", parameters={"type": "object", "properties": {}}),
    )
    mcp_manager._connections["hr-provider"] = _FakeConnection()
    mcp_manager._tool_index[tool_name] = discovered
    monkeypatch.setattr(mcp_manager, "is_available", lambda: True)
    monkeypatch.setattr(mcp_manager, "discovered_tools", lambda: [discovered])
    catalog = make_catalog(
        [
            McpToolProvider(
                servers=[SimpleNamespace(id="hr-provider")],
                server_ids_reader=StubMcpServerIdsReader(["hr-provider"]),
                auth_dependencies=null_egress_auth_dependencies(),
            )
        ]
    )
    try:
        metadata = resolve_tool_runtime_metadata(tool_name, catalog, scope=TEST_SCOPE)
        assert metadata.retrieval_aliases == ("pulse", "пульс")
        assert metadata.result_limit == 30_000
        # Unconfigured MCP tools keep the core default.
        other = resolve_tool_runtime_metadata("mcp__hr-provider__unknown_tool", catalog, scope=TEST_SCOPE)
        assert other.retrieval_aliases == ()
        assert other.result_limit == 15_000
    finally:
        mcp_manager._connections.pop("hr-provider", None)
        mcp_manager._tool_index.pop(tool_name, None)
