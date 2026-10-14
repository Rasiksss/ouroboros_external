from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager

import pytest
from starlette.websockets import WebSocketDisconnect

from infra.security_context import (
    SecurityContextCollectorFactory,
    SecurityContextFactory,
    security_context,
    security_context_collector,
)
from infra.security_context.collector import DeclaredParametersCollector
from infra.security_context.interface import (
    ISecurityContextCollector,
    SecurityContext,
    SecurityParameter,
    SecurityParameterKey,
    SecurityParameterType,
)
from ouroboros.contracts.owner_scope import SINGLE_AGENT_ID, configured_owner_id
from ouroboros.egress.auth.authenticators import UserHeadersAuthenticator
from ouroboros.egress.auth.interfaces import EgressAuthRefused
from ouroboros.storage.settings.schema import UserHeadersAuth
from tests.helpers.agent_registry import FakeAgentRegistry
from tests.helpers.fake_gateway import FakeGateway, RecordingMemoryEvents
from tests.helpers.security_context import FixedSecurityContextStore, context_of, declaration_of
from tests.helpers.server_client import patched_server_client
from tests.helpers.shared import TEST_SCOPE, AllowAllSkillPermissionChecker, scope_with
from tests.helpers.supervisor import bootstrap_supervisor_for_tests
from transport.browser.identity import (
    CHAT_HEADER_NAME,
    OWNER_HEADER_NAME,
    ConfiguredOwnerIdentity,
    TrustedHeaderIdentity,
    browser_identity,
)
from transport.skill_bridge import SkillBridge

SUDIR = {"X-Sudir-Token": "t1-secret", "Cookie": "sudir=abc; theme=dark"}
SUDIR_TOKEN = SecurityParameterKey(SecurityParameterType.HEADER, "X-Sudir-Token")
SUDIR_COOKIE = SecurityParameterKey(SecurityParameterType.COOKIE, "sudir")
THEME_COOKIE = SecurityParameterKey(SecurityParameterType.COOKIE, "theme")
COOKIE_HEADER = SecurityParameterKey(SecurityParameterType.HEADER, "cookie")
OWNER_HEADER = SecurityParameterKey(SecurityParameterType.HEADER, OWNER_HEADER_NAME)


class _FailingCollector(ISecurityContextCollector):
    def collect(self, headers: Iterable[tuple[str, str]]) -> tuple[SecurityParameter, ...]:
        raise RuntimeError("the collector saw t1-secret")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _token(owner_id: str, agent_id: str = SINGLE_AGENT_ID) -> str:
    return scope_with(TEST_SCOPE, owner_id=owner_id, agent_id=agent_id).security_token


def _load(owner_id: str, *keys: SecurityParameterKey) -> SecurityContext:
    return _run(security_context.store.load(_token(owner_id), keys))


@contextmanager
def _ws_log_captured(caplog) -> Iterator[None]:
    ws_log = logging.getLogger("transport.browser.ws")
    ws_log.addHandler(caplog.handler)
    try:
        yield
    finally:
        ws_log.removeHandler(caplog.handler)


def _client(monkeypatch, tmp_path):
    bootstrap_supervisor_for_tests(tmp_path / "data")
    return patched_server_client(monkeypatch, tmp_path, browser_enabled=True, reset_browser_ws=True)


@pytest.fixture
def trusted_headers():
    browser_identity.initialize(TrustedHeaderIdentity(FakeAgentRegistry()))
    yield
    browser_identity.initialize(ConfiguredOwnerIdentity(FakeAgentRegistry()))


def test_an_accepted_handshake_records_its_headers_under_the_configured_owner(monkeypatch, tmp_path) -> None:
    with _client(monkeypatch, tmp_path) as client, client.websocket_connect("/ws", headers=SUDIR):
        pass

    context = _load(configured_owner_id(), SUDIR_TOKEN, SUDIR_COOKIE, THEME_COOKIE, COOKIE_HEADER)
    assert context.value(SUDIR_TOKEN) == "t1-secret"
    assert context.value(SUDIR_COOKIE) == "abc"
    assert context.value(THEME_COOKIE) == "dark"
    assert context.value(COOKIE_HEADER) is None


def test_an_accepted_handshake_records_its_headers_under_the_trusted_owner(
    monkeypatch, tmp_path, trusted_headers
) -> None:
    headers = {**SUDIR, OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "7"}

    with _client(monkeypatch, tmp_path) as client, client.websocket_connect("/ws", headers=headers):
        pass

    context = _load("alice", SUDIR_TOKEN, OWNER_HEADER)
    assert context.value(SUDIR_TOKEN) == "t1-secret"
    assert context.value(OWNER_HEADER) is None
    assert _load(configured_owner_id(), SUDIR_TOKEN, OWNER_HEADER) == SecurityContext({})


def test_a_newer_connection_replaces_the_context(monkeypatch, tmp_path) -> None:
    with _client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws", headers={"X-Sudir-Token": "first"}):
            pass
        with client.websocket_connect("/ws", headers={"X-Sudir-Token": "second"}):
            pass

    assert _load(configured_owner_id(), SUDIR_TOKEN).value(SUDIR_TOKEN) == "second"


def test_a_newer_connection_erases_a_header_it_did_not_send(monkeypatch, tmp_path) -> None:
    with _client(monkeypatch, tmp_path) as client:
        with client.websocket_connect("/ws", headers=SUDIR):
            pass
        with client.websocket_connect("/ws", headers={"X-Other": "unrelated"}):
            pass

    assert _load(configured_owner_id(), SUDIR_TOKEN, SUDIR_COOKIE, THEME_COOKIE) == SecurityContext({})


def test_a_collector_over_a_narrower_declaration_records_only_its_names(monkeypatch, tmp_path) -> None:
    store = FixedSecurityContextStore()
    security_context.initialize(store)
    security_context_collector.initialize(
        DeclaredParametersCollector(declaration_of(headers=("X-Sudir-Token",), cookies=("sudir",)))
    )

    with _client(monkeypatch, tmp_path) as client, client.websocket_connect("/ws", headers=SUDIR):
        pass

    assert store.contexts == {
        _token(configured_owner_id()): context_of(headers={"X-Sudir-Token": "t1-secret"}, cookies={"sudir": "abc"})
    }


def test_a_refused_handshake_records_nothing(monkeypatch, tmp_path, trusted_headers) -> None:
    store = FixedSecurityContextStore()
    security_context.initialize(store)

    with _client(monkeypatch, tmp_path) as client:
        with pytest.raises(WebSocketDisconnect) as refusal:
            client.websocket_connect("/ws", headers={**SUDIR, CHAT_HEADER_NAME: "7"}).__enter__()

    assert refusal.value.code == 1008
    assert store.saves == []


def test_a_failed_save_refuses_the_handshake_with_a_reason(monkeypatch, tmp_path, caplog) -> None:
    from transport.browser.ws import get_browser_ws_hub

    store = FixedSecurityContextStore(error=RuntimeError("the store is down with t1-secret"))
    security_context.initialize(store)

    with _ws_log_captured(caplog):
        with _client(monkeypatch, tmp_path) as client:
            with pytest.raises(WebSocketDisconnect) as refusal:
                client.websocket_connect("/ws", headers=SUDIR).__enter__()

    assert refusal.value.code == 1011
    assert "security context" in refusal.value.reason
    assert "t1-secret" not in refusal.value.reason
    assert any("security context was not saved" in record.getMessage() for record in caplog.records)
    assert all("t1-secret" not in record.getMessage() for record in caplog.records)
    assert len(store.saves) == 1
    assert not get_browser_ws_hub().has_clients()


def test_a_failing_collector_refuses_the_handshake_and_saves_nothing(monkeypatch, tmp_path, caplog) -> None:
    from transport.browser.ws import get_browser_ws_hub

    store = FixedSecurityContextStore()
    security_context.initialize(store)
    security_context_collector.initialize(_FailingCollector())

    with _ws_log_captured(caplog):
        with _client(monkeypatch, tmp_path) as client:
            with pytest.raises(WebSocketDisconnect) as refusal:
                client.websocket_connect("/ws", headers=SUDIR).__enter__()

    assert refusal.value.code == 1011
    assert "security context" in refusal.value.reason
    assert "t1-secret" not in refusal.value.reason
    assert any("security context was not saved" in record.getMessage() for record in caplog.records)
    assert all("t1-secret" not in record.getMessage() for record in caplog.records)
    assert store.saves == []
    assert not get_browser_ws_hub().has_clients()


def test_an_uninstalled_store_is_loud_at_the_handshake(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("transport.browser.ws.security_context", SecurityContextFactory())

    with _client(monkeypatch, tmp_path) as client:
        with pytest.raises(RuntimeError, match=r"security_context\.initialize\(\)"):
            client.websocket_connect("/ws", headers=SUDIR).__enter__()


def test_an_uninstalled_collector_is_loud_at_the_handshake(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("transport.browser.ws.security_context_collector", SecurityContextCollectorFactory())

    with _client(monkeypatch, tmp_path) as client:
        with pytest.raises(RuntimeError, match=r"security_context_collector\.initialize\(\)"):
            client.websocket_connect("/ws", headers=SUDIR).__enter__()


def test_a_history_request_records_nothing(monkeypatch, tmp_path) -> None:
    store = FixedSecurityContextStore()
    security_context.initialize(store)

    with _client(monkeypatch, tmp_path) as client:
        response = client.get("/api/chat/history", headers=SUDIR)

    assert response.status_code == 200
    assert store.saves == []


def _injected_scope(tmp_path, monkeypatch, delivery_source: str):
    gateway = FakeGateway()
    bridge = SkillBridge(
        tmp_path,
        gateway=gateway,
        session_id="sess-test",
        memory_events=RecordingMemoryEvents(),
        permission_checker=AllowAllSkillPermissionChecker(),
    )
    monkeypatch.setattr(bridge, "require_permission", lambda *_args: None)
    bridge.enqueue_skill_message("distribution:extension", "hi", delivery_source=delivery_source)
    (envelope,) = gateway.submitted
    return envelope.scope


def test_an_extension_injecting_for_the_browser_inherits_the_handshakes_context(monkeypatch, tmp_path) -> None:
    with _client(monkeypatch, tmp_path) as client, client.websocket_connect("/ws", headers=SUDIR):
        pass
    authenticator = UserHeadersAuthenticator(UserHeadersAuth(headers=["X-Sudir-Token"]), security_context.store)

    browser_run = _injected_scope(tmp_path, monkeypatch, "browser")
    bridge_run = _injected_scope(tmp_path, monkeypatch, "")

    assert _run(authenticator.request_headers(browser_run)) == {"X-Sudir-Token": "t1-secret"}
    with pytest.raises(EgressAuthRefused, match="skill-bridge"):
        _run(authenticator.request_headers(bridge_run))
