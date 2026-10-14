from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

pytest.importorskip("a2a.server.routes.jsonrpc_routes", reason="a2a-sdk not installed; skip A2A tests")

from a2a.client.transports.http_helpers import get_http_args
from a2a.types.a2a_pb2 import AgentCard, AgentInterface
from a2a.utils.constants import TransportProtocol

from infra.user_tokens.interface import UserTokenTarget, UserTokenTargetKind
from new_core.identity import OwnerScope, SurfaceId
from ouroboros.egress.a2a.connection import A2aRemoteAgentConnection
from ouroboros.egress.a2a.dependencies import A2aEgressDependencies
from ouroboros.egress.a2a.manager import A2aManager
from ouroboros.egress.a2a.transports import (
    A2aCardRefused,
    build_http_client,
    call_context,
    fallback_agent_card,
    same_origin_card,
)
from ouroboros.egress.auth.authenticators import NoAuthenticator, authenticator_for, static_headers
from ouroboros.egress.auth.interfaces import EgressAuthDependencies
from ouroboros.storage.settings.schema import (
    A2aRemoteAgentEntry,
    EgressAuth,
    MTLSAuth,
    NoAuth,
    UserHeadersAuth,
    UserTokenAuth,
)
from tests.helpers.egress_auth import a2a_egress_dependencies, null_egress_auth_dependencies
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.security_context import FixedSecurityContextStore, context_of
from tests.helpers.user_tokens import FixedUserTokenStore
from tests.test_helper.a2a import FAKE_HOST, build_fake_agent, install_fake_transport
from tests.test_helper.a2a.scripted_agent import ScriptedAgent

URL = "https://agent.example.test/a2a"
STATIC = "static-secret-42"
PERSONAL = "personal-secret-17"
SUDIR = "sudir-header-7"
SESSION = "sudir-cookie-9"
CREDENTIAL_NAMES = frozenset({"x-api-key", "x-sudir-token", "cookie", "authorization"})
SCOPE = OwnerScope(owner_id="alice", agent_id="main", surface_id=SurfaceId.BROWSER, chat_id="1")

_AUTHS: dict[str, EgressAuth] = {
    "none": NoAuth(),
    "user_headers": UserHeadersAuth(headers=["X-Sudir-Token"], cookies=["sudir"]),
    "user_token": UserTokenAuth(auth_header="X-API-Key", auth_token=STATIC),
    "mtls": MTLSAuth(),
}
_EVERY_KIND = {kind: {"auth": auth} for kind, auth in _AUTHS.items()}


def _entry(auth: EgressAuth, url: str, agent_id: str) -> A2aRemoteAgentEntry:
    return A2aRemoteAgentEntry(id=agent_id, url=url, auth=auth, poll_interval_sec=0.05)


def _forwarded(entry: A2aRemoteAgentEntry) -> frozenset[str]:
    return authenticator_for(entry, UserTokenTargetKind.A2A_AGENT, null_egress_auth_dependencies()).forwarded_headers


def _card(*interfaces: tuple[str, str]) -> AgentCard:
    return AgentCard(
        name="remote",
        supported_interfaces=[AgentInterface(url=url, protocol_binding=binding) for url, binding in interfaces],
    )


def _recording_client(
    entry: A2aRemoteAgentEntry, sent: list[httpx.Request], *, status_code: int, headers: dict[str, str]
) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(status_code, json={}, headers=headers)

    client = build_http_client(entry, _forwarded(entry))
    client._transport = httpx.MockTransport(handle)
    return client


def _stores() -> tuple[FixedSecurityContextStore, FixedUserTokenStore, A2aEgressDependencies]:
    contexts = FixedSecurityContextStore({SCOPE.security_token: context_of(headers={"x-sudir-token": SUDIR})})
    tokens = FixedUserTokenStore({(UserTokenTarget(UserTokenTargetKind.A2A_AGENT, "crm"), "alice", "main"): PERSONAL})
    auth = EgressAuthDependencies(security_context_store=contexts, user_token_store=tokens)
    return contexts, tokens, a2a_egress_dependencies(auth)


def _discovered(agent: ScriptedAgent, auth: EgressAuth, dependencies: A2aEgressDependencies) -> bool:
    connection = A2aRemoteAgentConnection(_entry(auth, agent.url, "crm"), dependencies)

    async def scenario() -> bool:
        await connection.connect()
        try:
            return connection.card_available
        finally:
            await connection.aclose()

    return asyncio.run(scenario())


@parametrize_from_dict(_EVERY_KIND)
def test_the_client_sends_no_credential_by_default(auth) -> None:
    sent: list[httpx.Request] = []
    client = _recording_client(_entry(auth, URL, "crm"), sent, status_code=200, headers={})

    async def scenario() -> None:
        try:
            await client.post(URL, json={"jsonrpc": "2.0"})
        finally:
            await client.aclose()

    asyncio.run(scenario())

    assert CREDENTIAL_NAMES.isdisjoint(name.lower() for name in client.headers)
    assert CREDENTIAL_NAMES.isdisjoint(name.lower() for name in sent[0].headers)


@parametrize_from_dict(_EVERY_KIND)
def test_the_client_keeps_no_cookie_a_response_sets(auth) -> None:
    sent: list[httpx.Request] = []
    client = _recording_client(
        _entry(auth, URL, "crm"), sent, status_code=200, headers={"set-cookie": "session=s1; Path=/"}
    )

    async def scenario() -> None:
        try:
            await client.post(URL, json={"owner": "a"})
            await client.post(URL, json={"owner": "b"})
            await client.post(URL, json={"owner": "a"})
        finally:
            await client.aclose()

    asyncio.run(scenario())

    assert [request.headers.get("cookie") for request in sent] == [None, None, None]
    assert not client.cookies


def test_discovery_carries_the_static_token_and_no_user_credential(monkeypatch) -> None:
    agent = build_fake_agent("crm")
    install_fake_transport(monkeypatch, agent)
    contexts, tokens, dependencies = _stores()

    assert _discovered(agent, _AUTHS["user_token"], dependencies) is True

    (card_request,) = agent.requests
    assert (card_request.method, card_request.path) == ("GET", "/crm/.well-known/agent-card.json")
    assert card_request.headers.get("x-api-key") == STATIC
    assert "cookie" not in card_request.headers
    assert (contexts.loads, tokens.loads) == ([], [])


@parametrize_from_dict(
    {
        "none": {"auth": _AUTHS["none"]},
        "mtls": {"auth": _AUTHS["mtls"]},
        "user_headers": {"auth": _AUTHS["user_headers"]},
        "user_token without a static token": {"auth": UserTokenAuth(auth_header="X-API-Key")},
    }
)
def test_discovery_is_anonymous_without_a_static_token(auth, monkeypatch) -> None:
    agent = build_fake_agent("crm")
    install_fake_transport(monkeypatch, agent)
    contexts, tokens, dependencies = _stores()

    assert _discovered(agent, auth, dependencies) is True

    (card_request,) = agent.requests
    assert CREDENTIAL_NAMES.isdisjoint(card_request.headers)
    assert (contexts.loads, tokens.loads) == ([], [])


def test_a_redirect_takes_neither_the_credential_nor_the_request() -> None:
    sent: list[httpx.Request] = []
    client = _recording_client(
        _entry(_AUTHS["user_token"], URL, "crm"),
        sent,
        status_code=307,
        headers={"location": "https://elsewhere.example.test/steal"},
    )

    async def scenario() -> httpx.Response:
        try:
            return await client.post(URL, json={"jsonrpc": "2.0"}, headers={"X-API-Key": PERSONAL})
        finally:
            await client.aclose()

    response = asyncio.run(scenario())

    assert response.status_code == 307
    assert [request.url.host for request in sent] == ["agent.example.test"]
    assert sent[0].headers.get("x-api-key") == PERSONAL


_OTHER_ORIGINS = {
    "scheme": "http://agent.example.test/a2a",
    "host": "https://other.example.test/a2a",
    "port": "https://agent.example.test:8443/a2a",
}
_STRIPPED = {
    f"{kind} {change}": {"auth": _AUTHS[kind], "target": target}
    for kind in ("user_headers", "user_token")
    for change, target in _OTHER_ORIGINS.items()
}


@parametrize_from_dict(_STRIPPED)
def test_credentials_are_stripped_from_a_request_to_another_origin(auth, target) -> None:
    entry = _entry(auth, URL, "crm")
    sent: list[httpx.Request] = []
    client = _recording_client(entry, sent, status_code=200, headers={})
    credentials = {"X-API-Key": PERSONAL, "X-Sudir-Token": SUDIR, "Cookie": f"sudir={SESSION}"}

    async def scenario() -> None:
        try:
            await client.post(URL, json={}, headers=credentials)
            await client.post(target, json={}, headers=credentials)
        finally:
            await client.aclose()

    asyncio.run(scenario())

    forwarded = {name.lower() for name in _forwarded(entry)}
    same_origin, other_origin = sent
    assert forwarded <= {name.lower() for name in same_origin.headers}
    assert forwarded.isdisjoint(name.lower() for name in other_origin.headers)


def test_off_origin_interfaces_are_dropped_from_the_card(monkeypatch) -> None:
    narrowed = same_origin_card(
        _card(
            ("https://other.example.test/rpc", TransportProtocol.JSONRPC),
            ("https://agent.example.test/rpc", TransportProtocol.JSONRPC),
            ("https://agent.example.test/rest", TransportProtocol.HTTP_JSON),
        ),
        URL,
    )
    agent = build_fake_agent("crm")
    interfaces = [
        AgentInterface(url="https://other.example.test/rpc", protocol_binding=TransportProtocol.JSONRPC),
        *agent.card.supported_interfaces,
    ]
    del agent.card.supported_interfaces[:]
    agent.card.supported_interfaces.extend(interfaces)
    install_fake_transport(monkeypatch, agent)
    _, _, dependencies = _stores()
    connection = A2aRemoteAgentConnection(_entry(NoAuth(), agent.url, "crm"), dependencies)

    async def scenario() -> str:
        await connection.connect()
        try:
            return await connection.call(SCOPE, "hello there", timeout_sec=10.0)
        finally:
            await connection.aclose()

    result = asyncio.run(scenario())

    assert [interface.url for interface in narrowed.supported_interfaces] == [
        "https://agent.example.test/rpc",
        "https://agent.example.test/rest",
    ]
    assert "answer: hello there" in result
    assert [request.headers.get("host") for request in agent.requests] == ["fake-a2a.test"] * len(agent.requests)
    assert any(request.rpc_method for request in agent.requests)


@parametrize_from_dict(
    {
        "https": {"configured": "https://agent.example.test/a2a", "offered": "https://agent.example.test:443/rpc"},
        "http": {"configured": "http://gateway.test/a2a", "offered": "http://gateway.test:80/rpc"},
        "mixed-case scheme": {"configured": URL, "offered": "HTTPS://agent.example.test:443/rpc"},
    }
)
def test_a_default_port_is_the_same_origin(configured, offered) -> None:
    narrowed = same_origin_card(_card((offered, TransportProtocol.JSONRPC)), configured)

    assert [interface.url for interface in narrowed.supported_interfaces] == [offered]


@parametrize_from_dict(
    {
        "ipv6 zone id outside ascii": {"offered": "https://[fe80::1%éth0]/rpc"},
        "malformed punycode": {"offered": "https://xn--a.test/rpc"},
        "unparseable": {"offered": "https://[zz]/rpc"},
    }
)
def test_an_interface_without_an_origin_is_dropped_from_the_card(offered) -> None:
    card = _card((offered, TransportProtocol.JSONRPC), ("https://agent.example.test/rpc", TransportProtocol.JSONRPC))

    narrowed = same_origin_card(card, URL)

    assert [interface.url for interface in narrowed.supported_interfaces] == ["https://agent.example.test/rpc"]
    with pytest.raises(A2aCardRefused):
        same_origin_card(_card((offered, TransportProtocol.JSONRPC)), URL)


def test_a_card_with_only_off_origin_json_rpc_interfaces_is_refused() -> None:
    card = _card(
        ("https://user:pw@other.example.test:8443/rpc?k=v", TransportProtocol.JSONRPC),
        ("https://agent.example.test/rest", TransportProtocol.HTTP_JSON),
    )

    with pytest.raises(A2aCardRefused) as caught:
        same_origin_card(card, URL)

    message = str(caught.value)
    assert "https://agent.example.test" in message
    assert "https://other.example.test:8443" in message
    for fragment in ("/a2a", "/rpc", "/rest", "k=v", "user:pw"):
        assert fragment not in message


def test_an_agent_whose_card_points_elsewhere_is_skipped_naming_origins_only(monkeypatch, caplog) -> None:
    elsewhere = build_fake_agent("elsewhere")
    elsewhere.card.supported_interfaces[0].url = "https://user:pw@other.example.test:8443/rpc?k=v"
    live = build_fake_agent("live")
    install_fake_transport(monkeypatch, elsewhere, live)
    _, _, dependencies = _stores()

    async def scenario() -> list[str]:
        manager = A2aManager()
        await manager.initialize(
            [_entry(NoAuth(), elsewhere.url, "elsewhere"), _entry(NoAuth(), live.url, "live")],
            asyncio.get_running_loop(),
            True,
            dependencies,
        )
        try:
            return [connection.entry.id for connection in manager.connections()]
        finally:
            await manager.shutdown()

    with caplog.at_level(logging.WARNING):
        connected = asyncio.run(scenario())

    assert connected == ["live"]
    lines = [record.getMessage() for record in caplog.records if "elsewhere" in record.getMessage()]
    assert any(FAKE_HOST in line and "https://other.example.test:8443" in line for line in lines), lines
    for line in lines:
        for fragment in ("/rpc", "k=v", "user:pw", f"{FAKE_HOST}/"):
            assert fragment not in line


def test_the_fallback_card_addresses_the_configured_origin() -> None:
    narrowed = same_origin_card(fallback_agent_card(URL), URL)

    assert [(interface.url, interface.protocol_binding) for interface in narrowed.supported_interfaces] == [
        (f"{URL}/", TransportProtocol.JSONRPC)
    ]


def test_an_mtls_entry_builds_its_client_as_a_none_entry() -> None:
    entries = [_entry(NoAuth(), "http://gateway.test/a2a", "crm"), _entry(MTLSAuth(), "http://gateway.test/a2a", "crm")]
    authenticators = [
        authenticator_for(entry, UserTokenTargetKind.A2A_AGENT, null_egress_auth_dependencies()) for entry in entries
    ]
    clients = [
        build_http_client(entry, authenticator.forwarded_headers)
        for entry, authenticator in zip(entries, authenticators, strict=True)
    ]

    async def close() -> None:
        for client in clients:
            await client.aclose()

    shapes = [
        (dict(client.headers), len(client.event_hooks["request"]), client.follow_redirects, client.timeout)
        for client in clients
    ]
    asyncio.run(close())

    assert all(isinstance(authenticator, NoAuthenticator) for authenticator in authenticators)
    assert [static_headers(entry.auth) for entry in entries] == [{}, {}]
    assert shapes[0] == shapes[1]


def test_call_context_carries_headers_and_no_timeout() -> None:
    headers = {"X-API-Key": PERSONAL, "Cookie": f"sudir={SESSION}"}

    context = call_context(headers)

    assert context.service_parameters == headers
    assert context.timeout is None
    assert get_http_args(context) == {"headers": headers}
