from __future__ import annotations

import ast
import asyncio
import base64
import pathlib
import sys

import pytest
from pydantic import SecretStr

from infra.async_engine import create_async_runtime_engine
from infra.security_context import security_context, security_context_collector
from infra.security_context.collector import DeclaredParametersCollector
from infra.security_context.interface import SecurityContext, SecurityParameterKey, SecurityParameterType
from infra.security_context.sql import SqlSecurityContextStore
from ouroboros.app.egress_auth_composition import EgressAuthComposition, EgressList
from ouroboros.contracts.owner_scope import SINGLE_AGENT_ID
from ouroboros.storage.settings.schema import A2aRemoteAgentEntry, McpServerEntry, UserHeadersAuth
from security_config.auth.header_collector import header_collector_declaration
from tests.helpers.agent_registry import FakeAgentRegistry
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.security_context import (
    TEST_CHAT_ENCRYPTION_KEY,
    TEST_CREDENTIALS_KEY,
    install_fresh_security_context_collector,
    install_fresh_security_context_store,
    parameters_of,
)
from tests.helpers.server_client import patched_server_client
from tests.helpers.shared import TEST_SCOPE, scope_with
from tests.helpers.supervisor import bootstrap_supervisor_for_tests
from transport.browser.identity import (
    CHAT_HEADER_NAME,
    OWNER_HEADER_NAME,
    ConfiguredOwnerIdentity,
    TrustedHeaderIdentity,
    browser_identity,
)

HEADERS = {OWNER_HEADER_NAME: "alice", CHAT_HEADER_NAME: "7", "X-Sudir-Token": "t1-secret"}
SUDIR_TOKEN = SecurityParameterKey(SecurityParameterType.HEADER, "X-Sudir-Token")
OWNER_HEADER = SecurityParameterKey(SecurityParameterType.HEADER, OWNER_HEADER_NAME)
SERVER_PY = pathlib.Path(__file__).resolve().parents[2] / "server.py"


def _load_alice(runtime_db_url: str) -> SecurityContext:
    token = scope_with(TEST_SCOPE, owner_id="alice", agent_id=SINGLE_AGENT_ID).security_token
    engine = create_async_runtime_engine(runtime_db_url)
    loop = asyncio.get_event_loop()
    try:
        store = SqlSecurityContextStore(engine, encryption_key=SecretStr(TEST_CREDENTIALS_KEY))
        return loop.run_until_complete(store.load(token, {SUDIR_TOKEN, OWNER_HEADER}))
    finally:
        loop.run_until_complete(engine.dispose())


def _handshake_through_the_server(monkeypatch, tmp_path, engine_factory):
    bootstrap_supervisor_for_tests(tmp_path / "data")
    browser_identity.initialize(TrustedHeaderIdentity(FakeAgentRegistry()))
    try:
        with patched_server_client(monkeypatch, tmp_path, browser_enabled=True, reset_browser_ws=True) as client:
            store = sys.modules["server"]._compose_security_context(
                engine_factory.engine, SecretStr(TEST_CREDENTIALS_KEY), header_collector_declaration
            )
            with client.websocket_connect("/ws", headers=HEADERS):
                pass
    finally:
        browser_identity.initialize(ConfiguredOwnerIdentity(FakeAgentRegistry()))
    return store


def test_the_server_installs_the_sql_store_whose_handshake_another_store_loads(
    monkeypatch, tmp_path, engine_factory, runtime_db_url
) -> None:
    store = _handshake_through_the_server(monkeypatch, tmp_path, engine_factory)

    assert isinstance(store, SqlSecurityContextStore)
    assert security_context.store is store
    assert _load_alice(runtime_db_url) == SecurityContext({SUDIR_TOKEN: "t1-secret"})


@parametrize_from_dict(
    {
        "empty key": {"raw": ""},
        "short key": {"raw": base64.b64encode(b"short-s3cret").decode("ascii")},
        "a chat encryption key": {"raw": TEST_CHAT_ENCRYPTION_KEY},
    }
)
def test_the_server_refuses_to_compose_the_security_context_without_a_valid_key(engine_factory, raw) -> None:
    import server

    sentinel_store = install_fresh_security_context_store()
    sentinel_collector = install_fresh_security_context_collector()

    with pytest.raises(ValueError, match="OUROBOROS_CREDENTIALS_ENCRYPTION_KEY") as refused:
        server._compose_security_context(engine_factory.engine, SecretStr(raw), header_collector_declaration)

    if raw:
        assert raw not in str(refused.value)
    assert security_context.store is sentinel_store
    assert security_context_collector.collector is sentinel_collector


def test_composing_the_security_context_installs_the_declared_collector(monkeypatch, tmp_path, engine_factory) -> None:
    sentinel = install_fresh_security_context_collector()

    _handshake_through_the_server(monkeypatch, tmp_path, engine_factory)

    collector = security_context_collector.collector
    assert collector is not sentinel
    assert isinstance(collector, DeclaredParametersCollector)
    assert collector.collect(
        [("X-Sudir-Token", "t1"), (OWNER_HEADER_NAME, "alice"), ("Cookie", "sudir_session=s1; sudir=s2")]
    ) == parameters_of(headers={"X-Sudir-Token": "t1"}, cookies={"sudir_session": "s1"}, ttl_sec=3600)


class _Lifespan:
    def __init__(self) -> None:
        tree = ast.parse(SERVER_PY.read_text(encoding="utf-8"))
        self.tree = tree
        (self.lifespan,) = [
            node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
        ]
        self.calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]

    def called(self, name: str) -> list[ast.Call]:
        return [call for call in self.calls if isinstance(call.func, ast.Name) and call.func.id == name]

    def called_attribute(self, owner: str, attribute: str) -> list[ast.Call]:
        return [
            call
            for call in self.calls
            if isinstance(call.func, ast.Attribute)
            and call.func.attr == attribute
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == owner
        ]

    def within(self, root: ast.AST, nodes: list[ast.Call]) -> list[ast.Call]:
        return [node for node in ast.walk(root) if node in nodes]

    def assigned_name(self, call: ast.Call) -> str:
        (assignment,) = [
            node for node in ast.walk(self.lifespan) if isinstance(node, ast.Assign) and node.value is call
        ]
        (name,) = [target.id for target in assignment.targets if isinstance(target, ast.Name)]
        return name

    def rebinds(self, name: str) -> list[ast.AST]:
        return [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign | ast.NamedExpr)
            and any(
                isinstance(target, ast.Name) and target.id == name
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            )
        ]

    def keyword_names(self, call: ast.Call, keyword: str) -> list[str]:
        return [kw.value.id for kw in call.keywords if kw.arg == keyword and isinstance(kw.value, ast.Name)]


def test_the_lifespan_composes_the_security_context_once_with_the_declared_collector() -> None:
    source = _Lifespan()
    (composed,) = source.called("_compose_security_context")
    (compose,) = [
        node
        for node in ast.walk(source.tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_compose_security_context"
    ]

    assert source.within(source.lifespan, [composed]) == [composed]
    assert len(source.rebinds(source.assigned_name(composed))) == 1
    assert [arg.id for arg in composed.args if isinstance(arg, ast.Name)][-1] == "header_collector_declaration"
    for holder in ("security_context", "security_context_collector"):
        installs = source.called_attribute(holder, "initialize")
        assert len(installs) == 1
        assert source.within(compose, installs) == installs
    (collector,) = source.within(compose, source.called("DeclaredParametersCollector"))
    assert collector.args and isinstance(collector.args[0], ast.Name)
    assert len(source.within(compose, source.called("SqlSecurityContextStore"))) == 1
    (engine_initialized,) = source.called_attribute("async_engine_factory", "initialize_beside_store")
    assert engine_initialized.lineno < composed.lineno


def test_the_lifespan_hands_one_egress_auth_value_to_both_clients() -> None:
    source = _Lifespan()
    (composed,) = source.called("_compose_security_context")
    store_name = source.assigned_name(composed)
    (auth,) = source.called("EgressAuthDependencies")
    auth_name = source.assigned_name(auth)
    (user_token_store,) = source.called_attribute("EgressAuthComposition", "user_token_store")
    (a2a,) = source.called("A2aEgressDependencies")
    a2a_name = source.assigned_name(a2a)
    (task_handles,) = source.called("A2aTaskHandles")
    (mcp_provider,) = source.called("McpToolProvider")
    (a2a_provider,) = source.called("A2aToolProvider")
    (mcp_initialize,) = source.called_attribute("mcp_manager", "initialize")
    (a2a_initialize,) = source.called_attribute("a2a_manager", "initialize")
    tries = [node for node in ast.walk(source.lifespan) if isinstance(node, ast.Try)]

    assert source.keyword_names(auth, "security_context_store") == [store_name]
    assert source.within(auth, [user_token_store]) == [user_token_store]
    assert source.keyword_names(user_token_store, "engine_factory") == ["async_engine_factory"]
    assert [node.id for node in ast.walk(user_token_store.args[0]) if isinstance(node, ast.Name)] == [
        "connected_mcp_servers",
        "connected_a2a_agents",
    ]
    assert source.keyword_names(a2a, "auth") == [auth_name]
    assert source.within(a2a, [task_handles]) == [task_handles]
    assert not any(source.within(node, [auth, a2a]) for node in tries)
    assert len(source.rebinds(auth_name)) == 1
    assert len(source.rebinds(a2a_name)) == 1
    assert source.keyword_names(mcp_provider, "auth_dependencies") == [auth_name]
    assert [arg.id for arg in mcp_initialize.args if isinstance(arg, ast.Name)][-1] == auth_name
    assert source.keyword_names(a2a_provider, "dependencies") == [a2a_name]
    assert [arg.id for arg in a2a_initialize.args if isinstance(arg, ast.Name)][-1] == a2a_name
    assert a2a.lineno < a2a_initialize.lineno


def test_the_lifespan_seals_credentials_under_the_credentials_key_alone() -> None:
    source = _Lifespan()
    (composed,) = source.called("_compose_security_context")
    (user_token_store,) = source.called_attribute("EgressAuthComposition", "user_token_store")
    (task_handles,) = source.called("A2aTaskHandles")

    assert ast.unparse(composed.args[1]) == "settings.OUROBOROS_CREDENTIALS_ENCRYPTION_KEY"
    for call in (user_token_store, task_handles):
        assert [ast.unparse(kw.value) for kw in call.keywords if kw.arg == "encryption_key"] == [
            "settings.OUROBOROS_CREDENTIALS_ENCRYPTION_KEY"
        ]
    assert "OUROBOROS_CHAT_ENCRYPTION_KEY" not in SERVER_PY.read_text(encoding="utf-8")


_LISTS = {
    "EgressList.MCP": ("connected_mcp_servers", "mcp_servers if mcp_enabled else []", "mcp_enabled", "MCP_ENABLED"),
    "EgressList.A2A": (
        "connected_a2a_agents",
        "a2a_remote_agents if a2a_egress_enabled else []",
        "a2a_egress_enabled",
        "A2A_EGRESS_ENABLED",
    ),
}


def test_the_lifespan_refuses_undeclared_names_of_each_list_before_its_manager_connects() -> None:
    source = _Lifespan()
    refusals = source.called_attribute("EgressAuthComposition", "refuse_undeclared_user_headers")
    (mcp_initialize,) = source.called_attribute("mcp_manager", "initialize")
    (a2a_initialize,) = source.called_attribute("a2a_manager", "initialize")
    tries = [node for node in ast.walk(source.lifespan) if isinstance(node, ast.Try)]
    by_list = {ast.unparse(kw.value): refusal for refusal in refusals for kw in refusal.keywords if kw.arg == "source"}

    assert len(refusals) == 2
    assert sorted(by_list) == sorted(_LISTS)
    for list_name, (connected, gated, switch, setting) in _LISTS.items():
        refusal = by_list[list_name]
        (connected_binding,) = source.rebinds(connected)
        (switch_binding,) = source.rebinds(switch)
        assert source.within(source.lifespan, [refusal]) == [refusal]
        assert not any(source.within(node, [refusal]) for node in tries)
        assert [arg.id for arg in refusal.args if isinstance(arg, ast.Name)] == [
            connected,
            "header_collector_declaration",
        ]
        assert ast.unparse(connected_binding.value) == gated
        assert setting in ast.unparse(switch_binding.value)
        assert refusal.lineno < mcp_initialize.lineno
        assert refusal.lineno < a2a_initialize.lineno


@parametrize_from_dict(
    {
        "mcp": {
            "entries": [
                McpServerEntry(
                    id="hr", url="https://hr.internal/mcp", enabled=False, auth=UserHeadersAuth(headers=["X-Other"])
                )
            ],
            "source": EgressList.MCP,
        },
        "a2a": {
            "entries": [
                A2aRemoteAgentEntry(
                    id="research",
                    url="https://research.internal/a2a",
                    enabled=False,
                    auth=UserHeadersAuth(headers=["X-Other"]),
                )
            ],
            "source": EgressList.A2A,
        },
    }
)
def test_a_disabled_entry_forwarding_an_undeclared_name_does_not_refuse_the_root(entries, source) -> None:
    EgressAuthComposition.refuse_undeclared_user_headers(entries, header_collector_declaration, source=source)
