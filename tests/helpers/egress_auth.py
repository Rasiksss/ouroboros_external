from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pydantic import SecretStr

from ouroboros.egress.a2a.connection import A2aRemoteAgentConnection
from ouroboros.egress.a2a.dependencies import A2aEgressDependencies
from ouroboros.egress.a2a.task_handles import A2aTaskHandles
from ouroboros.egress.auth.interfaces import EgressAuthDependencies
from ouroboros.storage.settings.schema import A2aRemoteAgentEntry
from tests.helpers.security_context import TEST_CREDENTIALS_KEY, FixedSecurityContextStore, NullSecurityContextStore
from tests.helpers.user_tokens import NullUserTokenStore, unread_user_token_store

A2A_TASK_HANDLE_RE = re.compile(r"[^\s\"'=():;,]+~[0-9a-f]{32}")


def unread_egress_auth_dependencies() -> EgressAuthDependencies:
    return EgressAuthDependencies(
        security_context_store=FixedSecurityContextStore(
            error=AssertionError("a server without user authorization read the security context store")
        ),
        user_token_store=unread_user_token_store(),
    )


def null_egress_auth_dependencies() -> EgressAuthDependencies:
    return EgressAuthDependencies(
        security_context_store=NullSecurityContextStore(),
        user_token_store=NullUserTokenStore(),
    )


def a2a_egress_dependencies(auth: EgressAuthDependencies) -> A2aEgressDependencies:
    return A2aEgressDependencies(
        auth=auth,
        task_handles=A2aTaskHandles(encryption_key=SecretStr(TEST_CREDENTIALS_KEY)),
    )


@asynccontextmanager
async def connected_a2a_agent(
    entry: A2aRemoteAgentEntry, dependencies: A2aEgressDependencies
) -> AsyncIterator[A2aRemoteAgentConnection]:
    connection = A2aRemoteAgentConnection(entry, dependencies)
    await connection.connect()
    try:
        yield connection
    finally:
        await connection.aclose()


def reported_task_handle(result: str) -> str:
    found = A2A_TASK_HANDLE_RE.search(result)
    if found is None:
        raise AssertionError(f"the result reports no task handle: {result}")
    return found.group(0)


__all__ = [
    "A2A_TASK_HANDLE_RE",
    "a2a_egress_dependencies",
    "connected_a2a_agent",
    "null_egress_auth_dependencies",
    "reported_task_handle",
    "unread_egress_auth_dependencies",
]
