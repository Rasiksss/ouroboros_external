"""MCP over HTTP carries one identifier per *request*, chosen by who queued it.

Both HTTP transports POST from a long-lived pump task created when the connection is opened, but
the SDK hands each outgoing message to that task under the context of whichever task queued it.
So the hook that stamps the header sees two different things:

- `initialize`, `notifications/initialized`, the tool-list refresh and each idle ping are queued
  from `MCPServerConnection._serve_session`'s own frame, so they carry the identifier the
  connection minted for itself. Work with no caller belongs to the connection.
- A tool call is queued from its caller's task, so it carries the caller's identifier.

That split is a property of the transport, not a decision here, so the two halves are pinned
against the real `streamable_http` transport rather than against the hook — the hook is unchanged
and passes either way. They catch opposite collapses, one each:
`test_a_tool_call_carries_its_callers_identifier` fails if the hand-off stops carrying the
queueing context, which would put the connection's identifier back on every tool call;
`test_the_requests_the_connection_originates_carry_its_own` fails if `_serve_session` stops
minting a scope of its own, which would let a caller's identifier ride on the connection's own
traffic. Neither failure is visible to type checking, and neither shows up as an error —
`apply_trace_header` omits the header rather than raising — so nothing else would report it.

What still has to hold either way: every request carries something, and what the connection
originates is never inherited from whatever was running when the connection was opened. That last
rule lives one layer up, in
`tests/ouroboros/mcp/test_mcp_client.py::test_a_session_runs_under_an_operation_it_minted_for_itself`.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import httpx2
from mcp_types import METHOD_NOT_FOUND
from mcp_types.version import LATEST_HANDSHAKE_VERSION, LATEST_MODERN_VERSION

import ouroboros.mcp.connection as connection_mod
from ouroboros.egress.auth.interfaces import EgressAuthDependencies
from ouroboros.mcp.connection import MCPServerConnection
from ouroboros.observability import aef_tracing
from ouroboros.storage.settings.schema import McpServerEntry
from tests.helpers.egress_auth import unread_egress_auth_dependencies
from tests.helpers.security_context import FixedSecurityContextStore
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.user_tokens import FixedUserTokenStore

#: What a connection mints for itself when its session opens.
SESSION = "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"
OTHER = "463ac35c-9f64-13ad-4848-5a3953bb6124"
#: The operation a tool call is issued under, distinct from the connection's.
CALLER = "1c8f6c0a-2c2f-4f39-88a2-6a6f1b2f4c11"


def _body(request_id: object, method: str = "tools/call") -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method}).encode()


#: The hook both MCP transports install, reached where it is defined rather than through
#: `transports`: it is `aef_tracing`'s single spelling of "stamp the identifiers at send time", and
#: this package appends it rather than wrapping it. That the transports really do install *this*
#: object is pinned in `tests/ouroboros/mcp/test_mcp_client.py`, one assertion per transport.
_HOOK = aef_tracing.add_outbound_headers_on_request


def _hook_header(content: bytes) -> str | None:
    """Run the production hook against a request, as the pump task would."""
    request = httpx2.Request("POST", "http://mcp.test/mcp", content=content)
    asyncio.run(_HOOK(request))
    return request.headers.get(aef_tracing.TRACE_HEADER_NAME)


def test_the_hook_stamps_whatever_identifier_is_active_where_it_runs():
    with aef_tracing.trace_scope(SESSION):
        assert _hook_header(_body(1)) == SESSION


def test_the_hook_stamps_every_kind_of_request_alike():
    """The keepalive was the case that went out bare before: no caller, so no header at all.

    The hook does not know one method from another — which identifier each of these carries in
    production is decided by the task that queues it, and is pinned below against the transport.
    """
    with aef_tracing.trace_scope(SESSION):
        headers = [
            _hook_header(_body(1, method="initialize")),
            _hook_header(_body(2, method="tools/list")),
            _hook_header(_body(3, method="ping")),
            _hook_header(_body(4, method="tools/call")),
        ]

    assert headers == [SESSION] * 4


def test_a_request_with_no_session_carries_nothing():
    """Production always has a session scope; tracing never invents one to fill the gap."""
    assert _hook_header(_body(5)) is None


def test_the_identifier_is_read_per_request_not_fixed_when_the_client_is_built():
    """A client outlives the session whose identifier it carries, so a reconnect must show."""
    hook = _HOOK

    def header_under(trace_id: str) -> str | None:
        request = httpx2.Request("POST", "http://mcp.test/mcp", content=_body(1))
        with aef_tracing.trace_scope(trace_id):
            asyncio.run(hook(request))
        return request.headers.get(aef_tracing.TRACE_HEADER_NAME)

    assert header_under(SESSION) == SESSION
    assert header_under(OTHER) == OTHER


def test_a_real_redirect_hop_neither_fails_the_call_nor_loses_the_identifier():
    """`follow_redirects=True` is on both HTTP clients so a 307 from a gateway is not fatal.

    `httpx2` re-runs request hooks for every hop against a request it rebuilt around the
    *original* stream, which is where a body-reading hook used to raise `RequestNotRead` and turn
    the redirect it was meant to survive into a failed call. This hook reads no body, but the hop
    still has to carry the header, and no test using a hand-built `httpx2.Request` can see that —
    so this one drives the real client.
    """
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.url.path, request.headers.get(aef_tracing.TRACE_HEADER_NAME)))
        if request.url.path == "/mcp":
            return httpx2.Response(307, headers={"location": "http://mcp.test/mcp/"})
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": 7, "result": {}})

    async def call() -> httpx2.Response:
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            follow_redirects=True,
            event_hooks={"request": [_HOOK]},
        ) as client:
            return await client.post("http://mcp.test/mcp", content=_body(7))

    with aef_tracing.trace_scope(SESSION):
        response = asyncio.run(call())

    assert response.status_code == 200
    assert seen == [("/mcp", SESSION), ("/mcp/", SESSION)], "both hops must carry the identifier"


# ---------------------------------------------------------------------------
# The real transport: which identifier each request actually leaves with
# ---------------------------------------------------------------------------


#: What every `2026-07-28` result carries beyond its own fields; the client validates for it.
_MODERN_RESULT_ENVELOPE = {"ttlMs": 0, "cacheScope": "private", "resultType": "complete"}

#: Enough of a server for `ClientSession` to get through the handshake and one call.
_MOCK_RESULTS: dict[str, dict] = {
    "initialize": {
        "protocolVersion": LATEST_HANDSHAKE_VERSION,
        "capabilities": {},
        "serverInfo": {"name": "mock", "version": "1.0"},
    },
    "tools/list": {"tools": [{"name": "echo", "description": "", "inputSchema": {"type": "object"}}]},
    "tools/call": {"content": [{"type": "text", "text": "pong"}], "isError": False},
    "ping": {},
    "server/discover": {
        **_MODERN_RESULT_ENVELOPE,
        "supportedVersions": [LATEST_MODERN_VERSION],
        "capabilities": {},
    },
}


def _drive_a_connection(monkeypatch, *, era: str = "handshake") -> list[tuple[str, str | None]]:
    """Run a real `MCPServerConnection` over a real `streamable_http` transport.

    Returns (method, header) in send order. Nothing here opens a trace scope for the session:
    `_serve_session` mints one, and the connection's own requests must come out under it. The
    tool call is issued from a separate task under `CALLER`, exactly as a caller issues one, and
    `OTHER` is active at connect time so an inherited identifier would be visible rather than
    indistinguishable from a minted one.

    `era` decides what the mock server answers the connect-time probe with, and therefore which
    requests exist at all: a handshake peer refuses `server/discover` and gets `initialize`,
    `notifications/initialized` and a keepalive; a `2026-07-28` peer answers it and has no
    keepalive to send. Both are driven, because the split under test is era-independent and a
    test that only ever saw one era could not say so.
    """
    seen: list[tuple[str, str | None]] = []
    pinged = asyncio.Event()

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method != "POST":
            # The transport's SSE listener and its goodbye DELETE carry the header too, but they
            # have no JSON-RPC method to name, so they are not part of the assertion.
            return httpx2.Response(405)
        body = json.loads(request.content or b"{}")
        seen.append((body["method"], request.headers.get(aef_tracing.TRACE_HEADER_NAME)))
        if body["method"] == "ping":
            pinged.set()
        if "id" not in body:
            return httpx2.Response(202)  # a notification
        if body["method"] == "server/discover" and era != "modern":
            # What a server built before the revision answers for a method it does not implement.
            return httpx2.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {"code": METHOD_NOT_FOUND, "message": "Method not found"},
                },
            )
        result = dict(_MOCK_RESULTS[body["method"]])
        if era == "modern":
            # Every result carries the revision's cache hints, and the client validates them per
            # method: a handshake-shaped result is rejected outright on a modern session.
            result = {**_MODERN_RESULT_ENVELOPE, **result}
        return httpx2.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": result},
            headers={"mcp-session-id": "sess-1"},
        )

    real_client = httpx2.AsyncClient

    def mock_client(**kwargs: object) -> httpx2.AsyncClient:
        # `open_transport` builds its own client and exposes no transport seam, so the class it
        # reaches for is what gets swapped. Everything else about the client stays production's.
        return real_client(transport=httpx2.MockTransport(handler), **kwargs)  # type: ignore[arg-type]

    # The keepalive has to land inside the test rather than 30s later.
    monkeypatch.setattr(connection_mod, "_HEALTHCHECK_INTERVAL_SEC", 0.05)

    async def _run() -> None:
        entry = McpServerEntry(id="t", transport="streamable_http", url="http://mcp.test/mcp")
        conn = MCPServerConnection(entry, unread_egress_auth_dependencies())
        with aef_tracing.trace_scope(OTHER):
            await conn.connect()
        try:

            async def _caller() -> str:
                with aef_tracing.trace_scope(CALLER):
                    return await conn.call_tool(TEST_SCOPE, "echo", {}, timeout_sec=5)

            assert await asyncio.create_task(_caller()) == "pong"
            if era != "modern":
                # Wait for a keepalive issued *after* the call, so an inherited one would show.
                # There is none on `2026-07-28`: `ping` left the protocol with the session state.
                await asyncio.wait_for(pinged.wait(), timeout=5)
        finally:
            await conn.aclose()

    with patch.object(httpx2, "AsyncClient", mock_client):
        asyncio.run(_run())
    return seen


def _headers_for(seen: list[tuple[str, str | None]], method: str) -> list[str | None]:
    """Every header sent for `method`, not just the last.

    A dict over the pairs would keep one occurrence per method, so a single mis-attributed ping
    or tool-list refresh followed by a correct one would read as clean.
    """
    found = [header for sent, header in seen if sent == method]
    assert found, f"no {method} was sent"
    return found


def test_a_tool_call_carries_its_callers_identifier(monkeypatch):
    """The caller is visible where the request is built, so the request names the caller."""
    seen = _drive_a_connection(monkeypatch)

    assert _headers_for(seen, "tools/call") == [CALLER]


def test_a_tool_call_carries_its_callers_identifier_on_the_newer_era(monkeypatch):
    """The split comes from the SDK's context streams, which do not read the protocol version."""
    seen = _drive_a_connection(monkeypatch, era="modern")

    # Without this the test says nothing about the era it names: a probe that quietly fell back to
    # the handshake still sends `tools/call` under the caller, so the assertion below would pass on
    # a run that never reached `2026-07-28`.
    assert [method for method, _header in seen if method == "initialize"] == []
    assert _headers_for(seen, "tools/call") == [CALLER]


def test_the_requests_the_connection_originates_carry_its_own(monkeypatch):
    """Work with no caller belongs to the connection — minted by it, and never a caller's."""
    seen = _drive_a_connection(monkeypatch)
    session = _headers_for(seen, "server/discover")[0]

    assert session not in (None, "", CALLER, OTHER), "minted for the connection, not inherited"
    # The probe is the connection's own request too — refused here, which is what produces the
    # handshake that follows it.
    for method in ("server/discover", "initialize", "notifications/initialized", "tools/list", "ping"):
        # The ping is sent after the call, so it is also the keepalive-does-not-inherit case.
        assert _headers_for(seen, method) == [session] * len(_headers_for(seen, method)), method


def test_the_requests_the_connection_originates_carry_its_own_on_the_newer_era(monkeypatch):
    """The same rule, over the era's own method set: the probe and the refresh, and no keepalive.

    Enumerated rather than derived, because the point of the assertion is *which* requests a
    connection makes with no caller — a set that changed with the era and would otherwise be
    asserted from whatever the run happened to produce.
    """
    seen = _drive_a_connection(monkeypatch, era="modern")
    session = _headers_for(seen, "server/discover")[0]

    assert session not in (None, "", CALLER, OTHER), "minted for the connection, not inherited"
    for method in ("server/discover", "tools/list"):
        assert _headers_for(seen, method) == [session] * len(_headers_for(seen, method)), method
    assert [method for method, _header in seen if method == "ping"] == []
    assert [method for method, _header in seen if method == "initialize"] == []


# ---------------------------------------------------------------------------
# The channel the newer era opens: `Mcp-Param-*`, and what it cannot reach
# ---------------------------------------------------------------------------


def _drive_a_param_mirroring_call(annotation: str) -> list[httpx2.Request]:
    """One `tools/call` on a modern session, against a server annotating an argument.

    `annotation` is what the server declares as the header token — including, deliberately, the
    name of a header this system relies on. Returns the requests as sent, so the assertion reads
    the wire rather than the client's intent.
    """
    sent: list[httpx2.Request] = []
    listing = {
        "tools": [
            {
                "name": "echo",
                "description": "",
                "inputSchema": {
                    "type": "object",
                    "properties": {"tenant": {"type": "string", "x-mcp-header": annotation}},
                },
            }
        ]
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method != "POST":
            return httpx2.Response(405)
        sent.append(request)
        body = json.loads(request.content or b"{}")
        if "id" not in body:
            return httpx2.Response(202)
        result = listing if body["method"] == "tools/list" else _MOCK_RESULTS[body["method"]]
        return httpx2.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": {**_MODERN_RESULT_ENVELOPE, **result}},
        )

    real_client = httpx2.AsyncClient

    def mock_client(**kwargs: object) -> httpx2.AsyncClient:
        return real_client(transport=httpx2.MockTransport(handler), **kwargs)  # type: ignore[arg-type]

    async def _run() -> None:
        entry = McpServerEntry(
            id="t",
            transport="streamable_http",
            url="https://mcp.test/mcp",
            auth={"kind": "user_token", "auth_header": "Authorization", "auth_token": "Bearer server-token"},
        )
        conn = MCPServerConnection(
            entry,
            EgressAuthDependencies(
                security_context_store=FixedSecurityContextStore(
                    error=AssertionError("a user token server read the security context store")
                ),
                user_token_store=FixedUserTokenStore({}),
            ),
        )
        await conn.connect()
        try:

            async def _caller() -> str:
                with aef_tracing.trace_scope(CALLER):
                    return await conn.call_tool(TEST_SCOPE, "echo", {"tenant": "acme"}, timeout_sec=5)

            assert await asyncio.create_task(_caller()) == "pong"
        finally:
            await conn.aclose()

    with patch.object(httpx2, "AsyncClient", mock_client):
        asyncio.run(_run())
    return sent


def _calls(sent: list[httpx2.Request]) -> list[httpx2.Request]:
    return [request for request in sent if json.loads(request.content or b"{}").get("method") == "tools/call"]


def test_an_annotated_argument_is_mirrored_into_its_own_namespace():
    """The era lets a server route an argument into a header. Accepted — inside `Mcp-Param-*`."""
    (call,) = _calls(_drive_a_param_mirroring_call("Tenant"))

    assert call.headers.get("Mcp-Param-Tenant") == "acme"


def test_a_mirrored_argument_cannot_displace_the_headers_this_system_relies_on():
    """The bound that makes accepting the channel reasonable, asserted rather than believed.

    A server declares the annotation, and the value inside it is chosen by the model — so if the
    namespace ever stopped confining it, a tool argument could rewrite the credential this
    connection authenticates with or the identifier its requests are correlated by. A future SDK
    that widens the namespace has to fail here.
    """
    for annotation in ("Authorization", aef_tracing.TRACE_HEADER_NAME, "X-Agent-Id"):
        (call,) = _calls(_drive_a_param_mirroring_call(annotation))

        assert call.headers["Authorization"] == "Bearer server-token"
        assert call.headers[aef_tracing.TRACE_HEADER_NAME] == CALLER
        assert call.headers.get(annotation) != "acme"
        assert call.headers[f"Mcp-Param-{annotation}"] == "acme"
