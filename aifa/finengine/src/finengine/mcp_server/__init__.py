"""Native OpenCLAW MCP server exposing finengine business tools.

This is the NATIVE form of our tools: instead of the FA-CLAW backend injecting
tool definitions inline into every raw `/v1/responses` call, an OpenCLAW gateway
registers this server once (`mcp.servers.finengine`) and exposes its tools to the
agent natively. The server REUSES the existing dispatcher — it does not
re-implement any tool: it serves `REGISTRY` (filtered by the whitelist) and runs
each call through `execute(name, args, ctx)` from `finengine.llm.tools`.

Transport: streamable-http (a long-running service carrying the document store
model + per-user data — not a per-call stdio spawn).

Per-user identity (owner-scoping) is read from request headers the gateway/adapter
injects — see `_ctx_from_request`. This mirrors the bank Workstation adapter's
"agent never sets identity; the adapter injects it" model. The exact header names
are the integration contract documented for the gateway side.

`ask_user` is intentionally NOT exposed: it is agent-loop flow control (maps to the
gateway's native clarify/elicit), not a business function.
"""
from __future__ import annotations

import json
import logging
import os

import mcp.types as types
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from finengine.llm.tools import REGISTRY, ToolContext, _load_whitelist, execute

log = logging.getLogger(__name__)


def _transport_security() -> TransportSecuritySettings:
    """Streamable-HTTP host/origin policy. The MCP SDK's DNS-rebinding protection
    accepts only localhost by default, which breaks the OpenCLAW gateway reaching
    us over the docker bridge (`Host: 172.17.0.1:8848` → "Invalid Host header").
    This server binds to a PRIVATE interface (loopback / docker-bridge), not the
    internet. `FINENGINE_MCP_ALLOWED_HOSTS` (CSV, e.g. `172.17.0.1:8848,localhost`)
    restricts to specific hosts; unset = disable the check (dev default). The bank
    sets the allowlist to its gateway host."""
    hosts = os.environ.get("FINENGINE_MCP_ALLOWED_HOSTS", "").strip()
    if hosts:
        allowed = [h.strip() for h in hosts.split(",") if h.strip()]
        return TransportSecuritySettings(allowed_hosts=allowed, allowed_origins=allowed)
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)

# Not business functions — handled by the native runtime, never via MCP.
_NATIVE_ONLY = {"ask_user"}

# Headers the gateway/adapter is expected to inject per call (integration contract).
_H_USER = "x-aifa-user-id"
_H_PROJECT = "x-aifa-project-id"
_H_SESSION = "x-aifa-session-id"
_H_ROLE = "x-aifa-role"


def enabled_tool_names() -> list[str]:
    """Business tools to expose: whitelist ∩ REGISTRY, minus native-only ones."""
    wl = _load_whitelist()  # set[str] | None (None = all registered)
    return [n for n in REGISTRY if (wl is None or n in wl) and n not in _NATIVE_ONLY]


def _ctx_from_request(server) -> ToolContext:
    """Build the owner-scoping ToolContext from the per-request HTTP headers the
    gateway/adapter injects. Falls back to a dev identity from env so the server
    is usable standalone (e.g. `openclaw mcp probe`, local smoke tests)."""
    headers: dict[str, str] = {}
    try:
        req = server.request_context.request  # Starlette Request on streamable-http
        if req is not None:
            headers = {k.lower(): v for k, v in req.headers.items()}
    except Exception:  # noqa: BLE001 — no HTTP request context → use env fallback
        headers = {}
    uid = headers.get(_H_USER) or os.environ.get("FINENGINE_MCP_DEV_USER_ID") or None
    pid = headers.get(_H_PROJECT) or os.environ.get("FINENGINE_MCP_DEV_PROJECT_ID") or None
    sid = headers.get(_H_SESSION) or None
    role = headers.get(_H_ROLE) or ""
    return ToolContext(user_id=uid, project_id=pid, session_id=sid, role=role, progress=None)


def build_server() -> FastMCP:
    fastmcp = FastMCP(
        name="finengine-aifaclaw",
        instructions=(
            "AIFA CLAW — финансовые бизнес-инструменты (классификация и извлечение "
            "показателей из отчётов, финансовый анализ, факторный анализ, дайджест "
            "новостей, календарь корп-событий, база знаний чата, поиск). Плейбуки "
            "«когда какой инструмент» — в SKILL.md навыках агента, не здесь."
        ),
        transport_security=_transport_security(),
    )
    server = fastmcp._mcp_server  # low-level Server: explicit JSON-schema tools

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=name,
                description=REGISTRY[name].description,
                inputSchema=REGISTRY[name].parameters,
            )
            for name in enabled_tool_names()
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
        if name in _NATIVE_ONLY or name not in REGISTRY:
            return [types.TextContent(
                type="text",
                text=json.dumps({"error": f"tool '{name}' is not exposed by this server"}, ensure_ascii=False),
            )]
        ctx = _ctx_from_request(server)
        try:
            result = await execute(name, arguments or {}, ctx)
        except Exception as e:  # noqa: BLE001 — never crash the session on a tool error
            log.exception("MCP tool %s failed", name)
            result = json.dumps({"error": f"tool '{name}' failed: {e}"}, ensure_ascii=False)
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False, default=str)
        return [types.TextContent(type="text", text=result)]

    return fastmcp


# Module-level singleton + ASGI app for uvicorn (`finengine.mcp_server:app`).
mcp_server = build_server()


def build_app():
    """Starlette ASGI app (streamable-http, default mount `/mcp`) for uvicorn."""
    return mcp_server.streamable_http_app()


app = build_app()
