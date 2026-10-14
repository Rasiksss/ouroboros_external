"""Container probe for owner-and-agent MCP selection over the running multi-user stand."""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import threading
import urllib.error
import urllib.request
import uuid
from typing import Any

from config_registry.user_settings.infra.repositories.repo import (
    SqlAsyncUserSettingsStore,
    SqlSyncUserSettingsRepo,
    SqlUserSettingsStore,
)
from config_registry.user_settings.reader import UserSettings
from infra.cache import cache_factory
from new_core.identity import OwnerScope, SurfaceId
from new_core.tools.context import ToolContext
from new_core.tools.providers import McpToolProvider
from ouroboros.egress.auth.interfaces import EgressAuthDependencies
from ouroboros.mcp.manager import MCPManager
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.settings.store import load_settings
from security_config.mcp import mcp_servers
from tests.helpers.security_context import NullSecurityContextStore
from tests.helpers.user_tokens import NullUserTokenStore

API_BASE = os.environ.get("MCP_E2E_API_BASE", "http://balancer:8080").rstrip("/")
RUN_ID = os.environ.get("MCP_E2E_RUN_ID", uuid.uuid4().hex[:8])


def _request(method: str, path: str, owner_id: str, body: dict[str, Any] | None = None) -> Any:
    payload = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        f"{API_BASE}{path}",
        data=payload,
        headers={"Content-Type": "application/json", "X-Ouroboros-Owner-Id": owner_id},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            document = response.read()
    except urllib.error.HTTPError as failure:
        raise AssertionError(f"{method} {path} returned {failure.code}: {failure.read().decode()}") from failure
    return json.loads(document) if document else None


def _create_agent(owner_id: str, name: str) -> str:
    created = _request("POST", "/api/agents/", owner_id, {"name": name})
    return str(created["agent_id"])


def _set_servers(owner_id: str, agent_id: str, server_ids: list[str]) -> None:
    _request(
        "PUT",
        f"/api/owner-settings/{agent_id}/OUROBOROS_USER_ENABLED_MCP_IDS",
        owner_id,
        {"value": json.dumps(server_ids)},
    )


def _scope(owner_id: str, agent_id: str) -> OwnerScope:
    return OwnerScope(owner_id=owner_id, agent_id=agent_id, surface_id=SurfaceId.BROWSER, chat_id="mcp-e2e")


def _server_ids(provider: McpToolProvider, scope: OwnerScope) -> list[str]:
    return sorted({descriptor.origin.removeprefix("mcp:") for descriptor in provider.descriptors(scope)})


def main() -> int:
    settings = load_settings()
    runtime_persistence.initialize()
    cache_factory.initialize(
        str(settings.OUROBOROS_SETTINGS_CACHE_BACKEND or "none"),
        int(settings.OUROBOROS_SETTINGS_CACHE_TTL_SEC),
    )
    selection = UserSettings(
        settings,
        SqlUserSettingsStore(),
        SqlAsyncUserSettingsStore(),
        SqlSyncUserSettingsRepo(),
        cache_factory.cache(),
        cache_factory.async_cache(),
    )

    owners = {role: f"mcp-e2e-{RUN_ID}-{role}" for role in ("search", "calendar", "denied")}
    agents: dict[str, str] = {}
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, name="mcp-e2e-loop")
    loop_thread.start()
    manager = MCPManager()
    provider = McpToolProvider(
        servers=mcp_servers,
        server_ids_reader=selection.OUROBOROS_USER_ENABLED_MCP_IDS,
        auth_dependencies=EgressAuthDependencies(
            security_context_store=NullSecurityContextStore(), user_token_store=NullUserTokenStore()
        ),
        loop=loop,
        manager=manager,
    )

    try:
        agents = {role: _create_agent(owner, f"{role}-{RUN_ID}") for role, owner in owners.items()}
        _set_servers(owners["search"], agents["search"], ["search"])
        _set_servers(owners["calendar"], agents["calendar"], ["calendar"])

        provider.refresh()
        discovered = {tool.server_id for tool in manager.discovered_tools()}
        assert discovered == {"search", "calendar"}, discovered

        scopes = {role: _scope(owners[role], agents[role]) for role in owners}
        assert _server_ids(provider, scopes["search"]) == ["search"]
        assert _server_ids(provider, scopes["calendar"]) == ["calendar"]
        assert _server_ids(provider, scopes["denied"]) == []

        search_context = ToolContext(pathlib.Path("/app"), pathlib.Path("/data"), scopes["search"], None)
        calendar_context = ToolContext(pathlib.Path("/app"), pathlib.Path("/data"), scopes["calendar"], None)
        denied_context = ToolContext(pathlib.Path("/app"), pathlib.Path("/data"), scopes["denied"], None)
        search_result = provider.execute(search_context, "mcp__search__ping", {})
        calendar_result = provider.execute(calendar_context, "mcp__calendar__ping", {})
        assert "POLICY_DENIED" in provider.execute(search_context, "mcp__calendar__ping", {})
        assert "POLICY_DENIED" in provider.execute(calendar_context, "mcp__search__ping", {})
        assert "POLICY_DENIED" in provider.execute(denied_context, "mcp__search__ping", {})

        _request(
            "DELETE",
            f"/api/owner-settings/{agents['search']}/OUROBOROS_USER_ENABLED_MCP_IDS",
            owners["search"],
        )
        search_after_clear = _server_ids(provider, scopes["search"])
        assert search_after_clear == []
        assert "POLICY_DENIED" in provider.execute(search_context, "mcp__search__ping", {})
        assert _server_ids(provider, scopes["calendar"]) == ["calendar"]

        summary = {
            "calendar_agent": _server_ids(provider, scopes["calendar"]),
            "calendar_result": calendar_result,
            "denied_agent": _server_ids(provider, scopes["denied"]),
            "search_after_clear": search_after_clear,
            "search_agent": ["search"],
            "search_result": search_result,
        }
        print(f"MCP_OWNER_SELECTION_E2E={json.dumps(summary, sort_keys=True)}", flush=True)
        return 0
    finally:
        asyncio.run_coroutine_threadsafe(manager.shutdown(), loop).result(timeout=15)
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=15)
        runtime_persistence.close()
        for role, agent_id in agents.items():
            try:
                _request("DELETE", f"/api/owner-settings/{agent_id}/OUROBOROS_USER_ENABLED_MCP_IDS", owners[role])
                _request("DELETE", f"/api/agents/{agent_id}", owners[role])
            except Exception as cleanup_failure:
                print(f"cleanup failed for {role}: {cleanup_failure}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
