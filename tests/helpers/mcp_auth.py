from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import httpx2
from mcp_types import METHOD_NOT_FOUND
from mcp_types.version import LATEST_HANDSHAKE_VERSION, LATEST_MODERN_VERSION

_MODERN_ENVELOPE = {"ttlMs": 0, "cacheScope": "private", "resultType": "complete"}


@dataclass
class McpPeerRequest:
    method: str
    url: str
    headers: httpx2.Headers
    arguments: dict
    request_id: object = None


@dataclass
class McpPeer:
    era: str = "handshake"
    sent: list[McpPeerRequest] = field(default_factory=list)
    on_call: Callable[[McpPeerRequest], Awaitable[httpx2.Response | None]] | None = None
    on_list: Callable[[], Awaitable[httpx2.Response | None]] | None = None
    clients: list[httpx2.AsyncClient] = field(default_factory=list)
    pinged: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)

    def of(self, method: str) -> list[McpPeerRequest]:
        return [sent for sent in self.sent if sent.method == method]

    def carrying(self, *names: str) -> list[McpPeerRequest]:
        return [sent for sent in self.sent if any(name in sent.headers for name in names)]

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        headers = httpx2.Headers(request.headers)
        if request.method != "POST":
            self.sent.append(McpPeerRequest(request.method, str(request.url), headers, {}))
            return httpx2.Response(405)
        body = json.loads(request.content or b"{}")
        arguments = (body.get("params") or {}).get("arguments") or {}
        sent = McpPeerRequest(body["method"], str(request.url), headers, arguments, body.get("id"))
        self.sent.append(sent)
        if sent.method == "ping":
            self.pinged.set()
        if sent.method == "notifications/cancelled":
            self.cancelled.set()
        if "id" not in body:
            return httpx2.Response(202)
        if body["method"] == "server/discover" and self.era != "modern":
            error = {"code": METHOD_NOT_FOUND, "message": "Method not found"}
            return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": error})
        if body["method"] == "tools/call" and self.on_call is not None:
            answered = await self.on_call(sent)
            if answered is not None:
                return answered
        if body["method"] == "tools/list" and self.on_list is not None:
            listed = await self.on_list()
            if listed is not None:
                return listed
        return self._result(body["id"], self._payload(body["method"], sent))

    def _payload(self, method: str, sent: McpPeerRequest) -> dict:
        payloads = {
            "initialize": {
                "protocolVersion": LATEST_HANDSHAKE_VERSION,
                "capabilities": {},
                "serverInfo": {"name": "mock", "version": "1.0"},
            },
            "tools/list": {"tools": [{"name": "echo", "description": "", "inputSchema": {"type": "object"}}]},
            "tools/call": {"content": [{"type": "text", "text": f"pong:{sent.arguments.get('who', '')}"}]},
            "ping": {},
            "server/discover": {"supportedVersions": [LATEST_MODERN_VERSION], "capabilities": {}},
        }
        payload = payloads[method]
        return {**_MODERN_ENVELOPE, **payload} if self.era == "modern" else payload

    def _result(self, request_id: object, result: dict, **headers: str) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={"jsonrpc": "2.0", "id": request_id, "result": result},
            headers={"mcp-session-id": "sess-1", **headers},
        )

    def pong(self, sent: McpPeerRequest, **headers: str) -> httpx2.Response:
        return self._result(sent.request_id, self._payload("tools/call", sent), **headers)


def mock_mcp_client(peer: McpPeer) -> Callable[..., httpx2.AsyncClient]:
    real_client = httpx2.AsyncClient

    def mock_client(**kwargs: Any) -> httpx2.AsyncClient:
        client = real_client(transport=httpx2.MockTransport(peer.handle), **kwargs)
        peer.clients.append(client)
        return client

    return mock_client


def drive_mcp_peer(peer: McpPeer, body: Callable[[], Awaitable[None]]) -> None:
    with patch.object(httpx2, "AsyncClient", mock_mcp_client(peer)):
        asyncio.run(body())


__all__ = [
    "McpPeer",
    "McpPeerRequest",
    "drive_mcp_peer",
    "mock_mcp_client",
]
