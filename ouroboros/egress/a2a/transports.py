"""HTTP and SDK client construction for one outbound A2A connection."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from ouroboros.storage.settings.schema import A2aRemoteAgentEntry

if TYPE_CHECKING:
    from a2a.client import Client
    from a2a.types.a2a_pb2 import AgentCard

try:
    from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
    from a2a.client.client_factory import minimal_agent_card
    from a2a.utils.constants import TransportProtocol

    A2A_SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only where the SDK is absent
    A2A_SDK_AVAILABLE = False

CARD_CONNECT_TIMEOUT_SEC = 30.0
#: Bounds one HTTP round trip. The call deadline is A2A_TOOL_TIMEOUT_SEC, enforced across the
#: polling loop.
REQUEST_READ_TIMEOUT_SEC = 60.0

_SDK_MISSING = (
    "a2a-sdk is not installed — outbound A2A is unavailable. Install with: pip install 'a2a-sdk[http-server]==1.0.3'"
)


def require_sdk() -> None:
    if not A2A_SDK_AVAILABLE:
        raise RuntimeError(_SDK_MISSING)


def auth_headers(entry: A2aRemoteAgentEntry) -> dict[str, str]:
    """The credential header, or nothing. Both halves are required to send either."""
    if entry.auth_header and entry.auth_token:
        return {entry.auth_header: entry.auth_token}
    return {}


def build_http_client(entry: A2aRemoteAgentEntry) -> httpx.AsyncClient:
    """The one HTTP client this connection uses, for discovery and for calls alike."""
    return httpx.AsyncClient(
        headers=auth_headers(entry),
        timeout=httpx.Timeout(REQUEST_READ_TIMEOUT_SEC, connect=CARD_CONNECT_TIMEOUT_SEC),
    )


async def resolve_agent_card(http_client: httpx.AsyncClient, url: str) -> AgentCard:
    require_sdk()
    resolver = A2ACardResolver(http_client, base_url=url.rstrip("/"))
    return await resolver.get_agent_card(http_kwargs={"timeout": CARD_CONNECT_TIMEOUT_SEC})


def fallback_agent_card(url: str) -> AgentCard:
    """Stands in for a card that could not be fetched: enough to reach the configured URL."""
    require_sdk()
    return minimal_agent_card(url.rstrip("/") + "/", [TransportProtocol.JSONRPC])


def build_a2a_client(card: AgentCard, http_client: httpx.AsyncClient) -> Client:
    """``polling=True`` sets ``return_immediately`` on every send — the non-blocking submit
    this feature rests on. Streaming stays off: nothing here consumes partial output.

    The factory also sets the ``A2A-Version`` header, without which a compliant server reads
    the request as protocol 0.3.
    """
    require_sdk()
    factory = ClientFactory(ClientConfig(httpx_client=http_client, streaming=False, polling=True))
    return factory.create(card)


__all__ = [
    "A2A_SDK_AVAILABLE",
    "CARD_CONNECT_TIMEOUT_SEC",
    "REQUEST_READ_TIMEOUT_SEC",
    "auth_headers",
    "build_a2a_client",
    "build_http_client",
    "fallback_agent_card",
    "require_sdk",
    "resolve_agent_card",
]
