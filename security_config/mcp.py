"""MCP server list for this image — the security-owned source of the list.

Editing this file is the only way to change which MCP servers the process talks to, so a change
goes through code review, CI and image signing. The list is built once at import time and is fixed
for the process lifetime; secrets are read here, never written here.

**A server listed here can route a tool argument into an outbound header.** On the 2026-07-28
revision a server's own tool schema may mirror an argument into an `Mcp-Param-*` request header,
and the value is whatever the model passed. The namespace cannot displace `Authorization`,
`X-Trace-Id` or `X-Agent-Id` (pinned by `tests/ouroboros/mcp/test_mcp_trace_header.py`), so adding an externally
operated server is a decision to be made deliberately.

**Each entry declares exactly one authorization, `auth`, by `kind`.** There is no top-level
`auth_header`, `auth_token` or `tls`; an entry carrying one is refused as an unknown key.

- `none` (the default) — nothing is sent; any transport, and an `http://` URL is allowed.
- `user_token` — `auth_header` names the header, and `auth_token` is an optional static token of the
  deployment, read through `_token(...)` below (`""` means none). On `streamable_http` a call from a
  person's surface carries that person's token for this server and agent when the runtime database holds
  an unexpired one, otherwise the static token, otherwise it is refused; negotiation, discovery and
  keepalive carry the static token alone, and an A2A run never looks up a personal token. On `sse` only
  the static token is sent, so `sse` requires one.
- `user_headers` — `headers` and `cookies` name what is forwarded from the headers the browser presented
  on its WebSocket handshake: each named header as received (a header sent more than once is not forwarded at all), and one
  `Cookie` header holding the named cookies only. Only the HTTP request of a `tools/call` carries them —
  not its cancellation, a resumption or a reply to the server — and never to another origin.
  `streamable_http` only; the multi-user mode refuses no kind.
- `mtls` — the URL addresses the deployment's egress gateway, which originates mutual TLS to the server
  with its own certificate; the agent opens the connection as for `none`, holds no certificate and adds
  no credential. Any transport, and an `http://` URL is allowed.

`user_headers` and `user_token` refuse an `http://` URL: only `none` and `mtls` may address a plaintext
hop, so a credential and the gateway's mutual TLS cannot be combined on one entry.

**What the browser transport collects is declared beside this file**, in `auth/header_collector.yaml`:
`parameters`, each with `type` (`header` or `cookie`), `name` and `ttl_sec`, the lifetime of the collected
value. A `user_headers` entry can forward only what is declared there with the same type — the server,
and every tier of the multi-user mode, refuses to start otherwise — so replace the declaration together
with this list.
"""

import os

from ouroboros.storage.settings.schema import McpServerEntry, MTLSAuth, UserHeadersAuth, UserTokenAuth
from ouroboros.storage.settings.store import load_settings


def _token(name: str) -> str:
    """Secret for `name` from the environment — replace the body with the team's secret engine."""
    return os.getenv(name, "")


_polygon = load_settings().OUROBOROS_POLIGON

mcp_servers: list[McpServerEntry]

if _polygon == "DEV":
    # Legacy path: the list still comes from settings, as before this file existed.
    mcp_servers = list(load_settings().MCP_SERVERS)
elif _polygon == "EXAMPLE":
    mcp_servers = [
        McpServerEntry(
            id="example-mcp",
            name="Example MCP",
            transport="streamable_http",
            url="https://mcp.example.internal/mcp",
            auth=UserTokenAuth(auth_header="X-API-Key", auth_token=_token("MCP_EXAMPLE_TOKEN")),
            allowed_tools=["search"],
            retrieval_aliases=["example"],
        ),
        McpServerEntry(
            id="example-user-mcp",
            name="Example MCP authorized by the user",
            transport="streamable_http",
            url="https://user-mcp.example.internal/mcp",
            auth=UserHeadersAuth(headers=["X-Sudir-Token"], cookies=["sudir_session"]),
            allowed_tools=["search"],
            retrieval_aliases=["example-user"],
        ),
        McpServerEntry(
            id="example-mtls-mcp",
            name="Example MCP behind the egress gateway's mutual TLS",
            transport="streamable_http",
            url="http://egress-gateway.example.internal/mtls-mcp",
            auth=MTLSAuth(),
            allowed_tools=["search"],
            retrieval_aliases=["example-mtls"],
        ),
    ]
else:
    mcp_servers = []
