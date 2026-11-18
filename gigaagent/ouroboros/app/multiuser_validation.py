"""What the multi-user mode refuses, checked once before the process serves anything.

Each of these configurations fails later and less legibly: A2A and Dialogs hand every caller the
configured owner, and an MCP server or outbound A2A agent at an instance-local address is a
different peer per replica under a name that says one. Refusing at boot costs a restart;
discovering it at three in the morning costs an hour and a reading of the source.

Every refusal names what was found, why it cannot work, and what to change, and problems
of the same kind are listed together rather than one per restart.

**A skills tree carrying executable skills is not one of them.** It was, and a deployment
whose image ships such a tree then had to mask the whole directory to boot — which withheld
the documentation skills the mode does support along with the rest. What the image ships is
not a contradiction; discovery reads the part the mode supports and names the rest in the
log (`ouroboros/skills/loader.py`), and the tools that execute a skill are not composed
into the catalogue at all.

**The ingress rule covers an ingress with a sender to attribute, not a producer with
none.** A2A and Dialogs each stamp the instance's configured owner onto a message a person
sent, so the mode's central promise — a message is attributed to whoever sent it — is
already false for them. The in-process skill bridge builds the same ownership pair from the
same configuration and is deliberately *not* refused: an extension acts on the instance's
own behalf, has no author in its input, and says so where it builds the scope
(`transport/skill_bridge.py`). Refusing it would remove a working path to fix a defect it
does not have, and leaving the boundary unstated is what invites exactly that.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from collections.abc import Iterable
from typing import Protocol
from urllib.parse import SplitResult, urlsplit

from ouroboros.app.interfaces.deployment_keyspace import IDeploymentKeyspaceRepository
from ouroboros.storage.runtime_models import DeploymentKeyspace
from ouroboros.storage.settings.store import load_settings

log = logging.getLogger(__name__)

__all__ = [
    "MultiuserConfigurationError",
    "mode_refusals",
    "resolved_keyspace",
    "validate_deployment_keyspace",
    "validate_multiuser_configuration",
]


class MultiuserConfigurationError(RuntimeError):
    """The configuration contradicts the declared mode; the process must not start."""


def _a2a_refusal() -> str | None:
    from ouroboros.storage.settings.store import load_settings

    if not bool(load_settings().A2A_ENABLED):
        return None
    return (
        "A2A ingress is enabled (A2A_ENABLED); it mints the configured owner for every caller, so "
        "one owner's runs would carry another's identity; set A2A_ENABLED=false"
    )


def _dialogs_refusal() -> str | None:
    # Imported here rather than at module load: `ouroboros.app` may import `transport`,
    # but doing it at module scope would make every importer of this validator — including
    # roots that compose no transport at all — pull the whole dialogs module graph.
    from ouroboros.storage.settings.store import load_settings
    from transport.dialogs.transport import dialogs_feature_enabled

    if not dialogs_feature_enabled(load_settings()):
        return None
    return (
        "Dialogs ingress is configured (DIALOGS_GRPC_ENDPOINT); it builds the scope for every "
        "arriving chat from this instance's configuration, with no input from the message, so every "
        "sender would be stored and answered as the configured owner; clear DIALOGS_GRPC_ENDPOINT"
    )


def _split_url(url: str) -> SplitResult | None:
    try:
        return urlsplit(url)
    except ValueError:
        return None


def _as_ip_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except OSError:
        # Not numeric in any spelling: a name, which the caller compares as one.
        return None


def _is_instance_local_url(url: str) -> bool:
    parts = _split_url(url)
    if parts is None:
        return False
    host = (parts.hostname or "").strip().split("%", 1)[0].removesuffix(".")
    if not host:
        return False
    address = _as_ip_address(host)
    if address is None:
        return host == "localhost" or host.endswith(".localhost")
    return address.is_loopback or address.is_unspecified


def _address_of(url: str) -> str:
    parts = _split_url(url)
    if parts is None:
        return "<unparseable address>"
    host = (parts.hostname or "").strip()
    try:
        port = parts.port
    except ValueError:
        port = None
    return f"{host}:{port}" if port else host


class _AddressEntry(Protocol):
    id: str
    url: str
    enabled: bool


def _instance_local_offenders(entries: Iterable[_AddressEntry]) -> list[str]:
    return sorted(
        f"{entry.id} ({_address_of(entry.url)})"
        for entry in entries
        if entry.enabled and _is_instance_local_url(entry.url)
    )


def _mcp_address_refusal() -> str | None:
    from ouroboros.storage.settings.store import load_settings

    if not bool(load_settings().MCP_ENABLED):
        return None
    from security_config.mcp import mcp_servers

    offenders = _instance_local_offenders(mcp_servers)
    if not offenders:
        return None
    return (
        f"MCP servers are configured at instance-local addresses: {', '.join(offenders)}; the multi-user "
        "mode reaches one server over the network on every instance's behalf, and a loopback, "
        "localhost or 0.0.0.0 address resolves inside each instance's own container — so this is a "
        "different server per replica, "
        "or none, under a name that says one; give them an address every instance reaches in "
        "security_config/mcp.py, or set MCP_ENABLED=false"
    )


def _mcp_undeclared_user_headers_refusal() -> str | None:
    from ouroboros.storage.settings.store import load_settings

    if not bool(load_settings().MCP_ENABLED):
        return None
    from ouroboros.app.egress_auth_composition import EgressAuthComposition, EgressList
    from security_config.auth.header_collector import header_collector_declaration
    from security_config.mcp import mcp_servers

    return EgressAuthComposition.undeclared_user_headers_refusal(
        mcp_servers, header_collector_declaration, source=EgressList.MCP
    )


def _a2a_address_refusal() -> str | None:
    from ouroboros.storage.settings.store import load_settings

    if not bool(load_settings().A2A_EGRESS_ENABLED):
        return None
    from security_config.a2a import a2a_remote_agents

    offenders = _instance_local_offenders(a2a_remote_agents)
    if not offenders:
        return None
    return (
        f"A2A agents are configured at instance-local addresses: {', '.join(offenders)}; the multi-user "
        "mode reaches one remote agent over the network on every instance's behalf, and a loopback, "
        "localhost or 0.0.0.0 address resolves inside each instance's own container — so this is a "
        "different agent per replica, or none, under a name that says one, and a follow-up another replica "
        "serves reaches an agent that never received the task; give them an address every instance reaches "
        "in security_config/a2a.py, or set A2A_EGRESS_ENABLED=false"
    )


def _a2a_undeclared_user_headers_refusal() -> str | None:
    from ouroboros.storage.settings.store import load_settings

    if not bool(load_settings().A2A_EGRESS_ENABLED):
        return None
    from ouroboros.app.egress_auth_composition import EgressAuthComposition, EgressList
    from security_config.a2a import a2a_remote_agents
    from security_config.auth.header_collector import header_collector_declaration

    return EgressAuthComposition.undeclared_user_headers_refusal(
        a2a_remote_agents, header_collector_declaration, source=EgressList.A2A
    )


def mode_refusals() -> list[str]:
    """Every contradiction this configuration holds, in one place.

    A set rather than a function body, because the browser transport is its own process:
    a refusal that lives inside one composition root is a refusal the other process does
    not make, and it would then serve uploads under a configuration the mode refuses.
    Every root calls this; none re-implements a subset.

    No settings argument: each refusal resolves the configuration through the helper the
    runtime itself composes with, which is the rule that stopped one of them describing a
    database nobody set. A settings object threaded through would be honoured by the
    checks that take plain attributes and ignored by the ones that call a resolver — two
    answers to "what is configured" in one call.
    """
    return [
        refusal
        for refusal in (
            _runtime_db_refusal(),
            _a2a_refusal(),
            _dialogs_refusal(),
            _mcp_address_refusal(),
            _mcp_undeclared_user_headers_refusal(),
            _a2a_address_refusal(),
            _a2a_undeclared_user_headers_refusal(),
        )
        if refusal
    ]


def _runtime_db_refusal() -> str | None:
    """The database the runtime will actually open, resolved the way the runtime resolves it.

    Through ``collect_runtime_db_urls``, not a second read of one setting key: that helper
    *prefers* the numbered keys and returns nothing for the legacy one when any of them is
    set, so a check reading the legacy key alone describes a database nobody configured.
    Start-up refuses the database itself before the mode is consulted; what stays here is
    the resolver's own rejection, which the mode surfaces rather than letting it leave
    start-up as an unhandled error.
    """
    from ouroboros.storage.db_url import collect_runtime_db_urls
    from ouroboros.storage.settings.store import load_settings

    try:
        collect_runtime_db_urls(load_settings())
    except ValueError as rejection:
        # The resolver's own contradiction, which is exactly the kind the mode exists to
        # name. Any other exception is not ours to translate.
        return f"the runtime database configuration is rejected: {rejection}"
    return None


def resolved_keyspace() -> DeploymentKeyspace:
    """Where this process would write objects: backend, bucket and key prefix."""
    from ouroboros.storage.blob.factory import STORAGE_BACKEND_NAME
    from ouroboros.storage.blob.keys import storage_key_prefix

    settings = load_settings()
    return DeploymentKeyspace(
        backend=STORAGE_BACKEND_NAME,
        bucket=str(settings.OUROBOROS_S3_BUCKET or "").strip(),
        # The endpoint is part of where bytes land: two instances agreeing on a bucket and
        # a prefix at different endpoints are two buckets, with exactly the symptoms this
        # record exists to catch.
        endpoint=str(settings.OUROBOROS_S3_ENDPOINT or "").strip(),
        key_prefix=storage_key_prefix(),
    )


def validate_deployment_keyspace(repository: IDeploymentKeyspaceRepository) -> None:
    """Refuse a start whose keyspace disagrees with the one this deployment recorded."""
    if not bool(load_settings().OUROBOROS_MULTIUSER):
        return
    found = resolved_keyspace()
    recorded = repository.record_deployment_keyspace(found)
    if not found.differs_from(recorded):
        log.info("Deployment keyspace agrees with the recorded one (%s)", recorded.describe())
        return
    raise MultiuserConfigurationError(
        "OUROBOROS_MULTIUSER=true, but this instance would write objects somewhere else than the "
        "deployment recorded:\n"
        f"  - recorded: {recorded.describe()} (at {recorded.recorded_at})\n"
        f"  - this instance: {found.describe()}\n"
        "One of the two manifests is wrong; this check reports the disagreement and cannot say which. "
        "For a deliberate fleet-wide move, replace the record with `ouroboros keyspace-record --replace` "
        "in the same deployment step as the schema upgrade."
    )


def validate_multiuser_configuration() -> None:
    """Pass, or refuse the process with every contradiction the mode found."""
    from ouroboros.storage.settings.store import load_settings

    settings = load_settings()
    if not bool(settings.OUROBOROS_MULTIUSER):
        return
    refusals = list(mode_refusals())
    if not refusals:
        log.info("Multi-user mode configuration accepted")
        return
    raise MultiuserConfigurationError(
        "OUROBOROS_MULTIUSER=true, but this configuration cannot serve several owners:\n"
        + "\n".join(f"  - {refusal}" for refusal in refusals)
    )
