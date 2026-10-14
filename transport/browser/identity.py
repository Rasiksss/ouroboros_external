"""Who a browser connection belongs to, decided once at composition.

The deployment's mode declaration says whether this port is fronted by a proxy that
authenticates the caller and overwrites the identity headers. That is the only input to
the decision: nothing in a request — no source address, no bearer token, no absent header
— is evidence that a caller is or is not the proxy. The composition root builds the
resolver the mode implies and installs it here; the request paths ask the resolver and
never learn which one they hold.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from config_registry.agent_registry.domain import AgentEntity
from config_registry.agent_registry.interfaces import IAgentRegistryRepo
from new_core.identity import OwnerScope, SurfaceId
from ouroboros.contracts.chat_id_policy import is_internal_chat_id
from ouroboros.contracts.owner_scope import (
    AGENT_HEADER_NAME,
    CHAT_HEADER_NAME,
    OWNER_HEADER_NAME,
    configured_owner_id,
)

__all__ = [
    "AGENT_HEADER_NAME",
    "BROWSER_CHAT_ID",
    "CHAT_HEADER_NAME",
    "MAX_AGENT_ID_LENGTH",
    "MAX_CHAT_ID_LENGTH",
    "OWNER_HEADER_NAME",
    "TRANSPORT_NAME",
    "BrowserIdentityFactory",
    "ConfiguredOwnerIdentity",
    "IBrowserIdentity",
    "IdentityRefused",
    "TrustedHeaderIdentity",
    "browser_identity",
    "selected_agent_id",
    "trusted_chat_id",
    "trusted_owner_id",
]

BROWSER_CHAT_ID = 1
TRANSPORT_NAME = "browser"

MAX_CHAT_ID_LENGTH = 256

MAX_AGENT_ID_LENGTH = 64

log = logging.getLogger(__name__)


class IdentityRefused(Exception):
    """The connection presented no usable identity, and is refused rather than defaulted.

    Narrow on purpose: it is caught at the handshake and at the history route, and
    nowhere else. Its message is the reason the caller is told.
    """

    def __init__(self, reason: str, *, status: int = 403) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _header_value(headers: Any, name: str) -> str:
    """One header, looked up case-insensitively.

    Starlette's mapping already is; a plain dict is not, and the resolvers below are
    called directly by their tests.
    """
    wanted = name.lower()
    for key, value in dict(headers or {}).items():
        if str(key).lower() == wanted:
            return str(value or "")
    return ""


def trusted_owner_id(headers: Any) -> str:
    """The owner the proxy authenticated, as an opaque non-empty string.

    No format is imposed: a login, a subject claim and an internal id are all the same
    thing here — a storage key this instance did not choose.
    """
    value = _header_value(headers, OWNER_HEADER_NAME).strip()
    if not value:
        raise IdentityRefused(f"{OWNER_HEADER_NAME} is absent or empty")
    return value


def trusted_chat_id(headers: Any) -> str:
    """The conversation the request names, as an opaque non-empty string.

    One refusal beyond emptiness, and it is the chat-id policy's rather than a parse:
    the negative range belongs to the skill bridge and to A2A derivation, so a value
    there is a routing claim rather than a conversation, and clamping one would let a
    caller aim at a synthetic range and land on a real conversation. Emptiness stays this
    transport's own check, because the policy answers "not synthetic" for `""`, which is
    true and useless.

    Nothing else is imposed and nothing else is normalized: surrounding whitespace is
    stripped and the value is then compared byte for byte, so two spellings of one number
    are two conversations. The axis is at once a storage key and a delivery match key, and
    folding it at one of the two would split a conversation from its own history.
    """
    raw = _header_value(headers, CHAT_HEADER_NAME).strip()
    if not raw:
        raise IdentityRefused(f"{CHAT_HEADER_NAME} is absent or empty")
    if len(raw) > MAX_CHAT_ID_LENGTH:
        raise IdentityRefused(f"{CHAT_HEADER_NAME} is longer than {MAX_CHAT_ID_LENGTH} characters")
    if is_internal_chat_id(raw):
        raise IdentityRefused(f"{CHAT_HEADER_NAME} names a synthetic conversation, not a human one")
    return raw


def _selected_chat_id(headers: Any) -> str:
    if not _header_value(headers, CHAT_HEADER_NAME).strip():
        return str(BROWSER_CHAT_ID)
    return trusted_chat_id(headers)


def selected_agent_id(headers: Any) -> str:
    value = _header_value(headers, AGENT_HEADER_NAME).strip()
    if len(value) > MAX_AGENT_ID_LENGTH:
        raise IdentityRefused(f"{AGENT_HEADER_NAME} is longer than {MAX_AGENT_ID_LENGTH} characters")
    return value


class IBrowserIdentity(ABC):
    """The identity one browser connection or request presents."""

    @abstractmethod
    async def scope_for(self, headers: Any) -> OwnerScope:
        """The scope of one connection or request, minted from its own boundary."""

    @property
    @abstractmethod
    def delivers_unattributed_output(self) -> bool:
        """Whether output carrying no owner may be fanned out to every connection.

        A runtime log line and an extension lifecycle event name nobody. With one owner
        they are that owner's; with several there is no connection they belong to, and a
        diagnostic channel would republish one owner's output to another.
        """


async def _resolve_default_agent(registry: IAgentRegistryRepo, owner_id: str) -> AgentEntity:
    try:
        return await registry.get_or_create_default_agent(owner_id)
    except Exception:
        log.warning("The agent registry could not resolve the default agent for an owner", exc_info=True)
        raise IdentityRefused("the agent registry could not resolve this owner's agent", status=503) from None


async def _lookup_agent(registry: IAgentRegistryRepo, owner_id: str, agent_id: str) -> AgentEntity | None:
    try:
        return await registry.get_agent(owner_id, agent_id)
    except Exception:
        log.warning("The agent registry could not be read for a named agent", exc_info=True)
        raise IdentityRefused("the agent registry could not resolve this owner's agent", status=503) from None


async def _resolve_scoped_agent(registry: IAgentRegistryRepo, owner_id: str, agent_id: str) -> AgentEntity:
    if not agent_id:
        default = await _resolve_default_agent(registry, owner_id)
        if not default.is_active:
            raise IdentityRefused("this owner's default agent is no longer active")
        return default
    agent = await _lookup_agent(registry, owner_id, agent_id)
    if agent is None:
        raise IdentityRefused(f"{AGENT_HEADER_NAME} names no agent of this owner")
    if not agent.is_active:
        raise IdentityRefused(f"{AGENT_HEADER_NAME} names an agent that is no longer active")
    return agent


class ConfiguredOwnerIdentity(IBrowserIdentity):
    """One owner from configuration, and whichever of its agents and conversations is named.

    The owner is never read from a header; the other two axes are, because with one owner a
    header can only select within it. See `openspec/specs/trusted-identity-headers/spec.md`.
    """

    def __init__(self, agent_registry: IAgentRegistryRepo) -> None:
        self._agent_registry = agent_registry

    async def scope_for(self, headers: Any) -> OwnerScope:
        owner_id = configured_owner_id()
        chat_id = _selected_chat_id(headers)
        agent = await _resolve_scoped_agent(self._agent_registry, owner_id, selected_agent_id(headers))
        return OwnerScope(
            owner_id=owner_id,
            agent_id=agent.agent_id,
            surface_id=SurfaceId(TRANSPORT_NAME),
            chat_id=chat_id,
        )

    @property
    def delivers_unattributed_output(self) -> bool:
        return True


class TrustedHeaderIdentity(IBrowserIdentity):
    """The owner the proxy authenticated, and the agent and conversation the request names.

    The order in `scope_for` is the safety property: the owner is settled first, and the agent
    is then looked up inside it.
    """

    def __init__(self, agent_registry: IAgentRegistryRepo) -> None:
        self._agent_registry = agent_registry

    async def scope_for(self, headers: Any) -> OwnerScope:
        owner_id = trusted_owner_id(headers)
        chat_id = trusted_chat_id(headers)
        agent = await _resolve_scoped_agent(self._agent_registry, owner_id, selected_agent_id(headers))
        return OwnerScope(
            owner_id=owner_id,
            agent_id=agent.agent_id,
            surface_id=SurfaceId(TRANSPORT_NAME),
            chat_id=chat_id,
        )

    @property
    def delivers_unattributed_output(self) -> bool:
        return False


class BrowserIdentityFactory:
    """Holds the process's resolver, built once by the composition root.

    Code that cannot be handed the resolver as an argument reads it here. There is no
    default: a process that may serve several owners must not fall back to resolving every
    caller to the configured one, and a default is exactly how a composition root that
    forgot to install a resolver keeps working — with the wrong identity. Reading before
    installation raises, which is the failure a second composition root needs to be loud.
    """

    def __init__(self) -> None:
        self._resolver: IBrowserIdentity | None = None

    def initialize(self, resolver: IBrowserIdentity) -> None:
        self._resolver = resolver

    def reset_for_tests(self) -> None:
        """Put the holder back to uninstalled; production has no such transition."""
        self._resolver = None

    @property
    def resolver(self) -> IBrowserIdentity:
        resolver = self._resolver
        if resolver is None:
            raise RuntimeError(
                "the browser identity resolver is not installed; a composition root must call "
                "browser_identity.initialize() before anything serves"
            )
        return resolver


browser_identity = BrowserIdentityFactory()
