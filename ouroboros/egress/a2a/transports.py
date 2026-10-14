"""HTTP and SDK client construction for one outbound A2A connection."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from ouroboros.egress.auth.client_guard import CredentialedClientGuard, RequestOrigin
from ouroboros.observability import aef_tracing
from ouroboros.storage.settings.schema import A2aRemoteAgentEntry

if TYPE_CHECKING:
    from a2a.client import Client

try:
    from a2a.client import A2ACardResolver, ClientCallContext, ClientConfig, ClientFactory
    from a2a.client.client_factory import minimal_agent_card
    from a2a.types.a2a_pb2 import AgentCard
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


class A2aCardRefused(Exception):
    def __init__(self, configured: str, offered: tuple[str, ...]) -> None:
        super().__init__(
            f"the agent card offers no JSON-RPC interface on the configured origin {configured}; "
            f"it offers {', '.join(offered) if offered else 'no interface'}"
        )


def build_http_client(entry: A2aRemoteAgentEntry, forwarded_headers: frozenset[str]) -> httpx.AsyncClient:
    client = httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(REQUEST_READ_TIMEOUT_SEC, connect=CARD_CONNECT_TIMEOUT_SEC),
        event_hooks=aef_tracing.outbound_request_event_hooks(),
    )
    CredentialedClientGuard.keep_no_cookies(client)
    if forwarded_headers:
        CredentialedClientGuard.strip_off_origin(client, RequestOrigin.of(httpx.URL(entry.url)), forwarded_headers)
    return client


async def resolve_agent_card(http_client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> AgentCard:
    require_sdk()
    resolver = A2ACardResolver(http_client, base_url=url.rstrip("/"))
    return await resolver.get_agent_card(http_kwargs={"timeout": CARD_CONNECT_TIMEOUT_SEC, "headers": headers})


def fallback_agent_card(url: str) -> AgentCard:
    """Stands in for a card that could not be fetched: enough to reach the configured URL."""
    require_sdk()
    return minimal_agent_card(url.rstrip("/") + "/", [TransportProtocol.JSONRPC])


def same_origin_card(card: AgentCard, url: str) -> AgentCard:
    require_sdk()
    configured = RequestOrigin.of(httpx.URL(url))
    kept = [interface for interface in card.supported_interfaces if _origin_of(interface.url) == configured]
    if not any(interface.protocol_binding == TransportProtocol.JSONRPC for interface in kept):
        offered = [
            origin for interface in card.supported_interfaces if (origin := _origin_of(interface.url)) is not None
        ]
        raise A2aCardRefused(_rendered(configured), tuple(dict.fromkeys(_rendered(origin) for origin in offered)))
    narrowed = AgentCard()
    narrowed.CopyFrom(card)
    del narrowed.supported_interfaces[:]
    narrowed.supported_interfaces.extend(kept)
    return narrowed


def call_context(headers: dict[str, str]) -> ClientCallContext:
    require_sdk()
    return ClientCallContext(service_parameters=dict(headers))


def _origin_of(url: str) -> RequestOrigin | None:
    try:
        parsed = httpx.URL(url)
        return RequestOrigin.of(parsed) if parsed.scheme and parsed.host else None
    except (httpx.InvalidURL, UnicodeError):
        return None


def _rendered(origin: RequestOrigin) -> str:
    host = f"[{origin.host}]" if ":" in origin.host else origin.host
    return f"{origin.scheme}://{host}" if origin.port is None else f"{origin.scheme}://{host}:{origin.port}"


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
    "A2aCardRefused",
    "build_a2a_client",
    "build_http_client",
    "call_context",
    "fallback_agent_card",
    "require_sdk",
    "resolve_agent_card",
    "same_origin_card",
]
