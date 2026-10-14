from __future__ import annotations

import asyncio

from infra.security_context.interface import ISecurityContextStore
from infra.user_tokens.interface import IUserTokenStore, UserTokenTarget, UserTokenTargetKind
from new_core.identity import OwnerScope, SurfaceId
from ouroboros.egress.a2a.dependencies import A2aEgressDependencies
from ouroboros.egress.auth.interfaces import EgressAuthDependencies
from ouroboros.storage.settings.schema import A2aRemoteAgentEntry, EgressAuth, UserHeadersAuth, UserTokenAuth
from tests.helpers.egress_auth import a2a_egress_dependencies, connected_a2a_agent
from tests.helpers.security_context import FixedSecurityContextStore, context_of
from tests.helpers.user_tokens import FixedUserTokenStore
from tests.test_helper.a2a import build_fake_agent, install_fake_transport
from tests.test_helper.a2a.scripted_agent import RecordedRequest, ScriptedAgent

AGENT = "crm"
TARGET = UserTokenTarget(UserTokenTargetKind.A2A_AGENT, AGENT)
STATIC = "static-secret-42"
ALICE_TOKEN = "alice-personal-17"
ALICE_SUDIR = "alice-sudir-7"
ALICE_SESSION = "alice-session-9"
BOB_SUDIR = "bob-sudir-3"
BOB_SESSION = "bob-session-5"
ALICE = OwnerScope(owner_id="alice", agent_id="main", surface_id=SurfaceId.BROWSER, chat_id="1")
BOB = OwnerScope(owner_id="bob", agent_id="main", surface_id=SurfaceId.BROWSER, chat_id="1")
TOKEN_AUTH = UserTokenAuth(auth_header="X-API-Key", auth_token=STATIC)
HEADERS_AUTH = UserHeadersAuth(headers=["X-Sudir-Token"], cookies=["sudir"])


def fake_crm_agent(monkeypatch) -> ScriptedAgent:
    agent = build_fake_agent(AGENT)
    install_fake_transport(monkeypatch, agent)
    return agent


def entry_for(agent: ScriptedAgent, auth: EgressAuth) -> A2aRemoteAgentEntry:
    return A2aRemoteAgentEntry(id=agent.agent_id, url=agent.url, auth=auth, poll_interval_sec=0.05)


def recorded_contexts() -> FixedSecurityContextStore:
    return FixedSecurityContextStore(
        {
            ALICE.security_token: context_of(headers={"x-sudir-token": ALICE_SUDIR}, cookies={"sudir": ALICE_SESSION}),
            BOB.security_token: context_of(headers={"x-sudir-token": BOB_SUDIR}, cookies={"sudir": BOB_SESSION}),
        }
    )


def stored_tokens() -> FixedUserTokenStore:
    return FixedUserTokenStore({(TARGET, "alice", "main"): ALICE_TOKEN})


def unread_contexts() -> FixedSecurityContextStore:
    return FixedSecurityContextStore(error=AssertionError("a user token agent read the security context store"))


def credential_dependencies(*, contexts: ISecurityContextStore, tokens: IUserTokenStore) -> A2aEgressDependencies:
    return a2a_egress_dependencies(EgressAuthDependencies(security_context_store=contexts, user_token_store=tokens))


def run_connected(agent: ScriptedAgent, auth: EgressAuth, dependencies: A2aEgressDependencies, body):
    async def scenario():
        async with connected_a2a_agent(entry_for(agent, auth), dependencies) as connection:
            return await body(connection)

    return asyncio.run(scenario())


def called(
    agent: ScriptedAgent, auth: EgressAuth, dependencies: A2aEgressDependencies, scope: OwnerScope, message: str
) -> str:
    return run_connected(
        agent, auth, dependencies, lambda connection: connection.call(scope, message, timeout_sec=10.0)
    )


def scope_in_another_chat(owner_id: str, agent_id: str, surface_id: SurfaceId) -> OwnerScope:
    return OwnerScope(owner_id=owner_id, agent_id=agent_id, surface_id=surface_id, chat_id="another-chat")


def sent_requests(agent: ScriptedAgent, rpc_method: str) -> list[RecordedRequest]:
    return [request for request in agent.requests if request.rpc_method == rpc_method]


__all__ = [
    "AGENT",
    "ALICE",
    "ALICE_SESSION",
    "ALICE_SUDIR",
    "ALICE_TOKEN",
    "BOB",
    "BOB_SESSION",
    "BOB_SUDIR",
    "HEADERS_AUTH",
    "STATIC",
    "TARGET",
    "TOKEN_AUTH",
    "called",
    "credential_dependencies",
    "entry_for",
    "fake_crm_agent",
    "recorded_contexts",
    "run_connected",
    "scope_in_another_chat",
    "sent_requests",
    "stored_tokens",
    "unread_contexts",
]
