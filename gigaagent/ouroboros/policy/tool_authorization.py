"""Tool Policy Engine authorization SSOT (allow / deny / confirm / sandbox-required)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ouroboros.tools.catalog import spec_for_tool
from ouroboros.tools.spec import CapabilityClass
from ouroboros.utils import utc_now_iso
from security_config.confirmations import (
    confirmation_exempt_origins,
    confirmation_required_origins,
    confirmation_required_tools,
)

PolicyOutcome = Literal["allow", "deny", "confirm_pending"]

_HEADLESS_DENIED_CLASSES: frozenset[CapabilityClass] = frozenset({"shell", "git", "control"})

_SKILL_SANDBOX_FACTORY: dict[str, str] = {
    "skill_exec": "skill_default",
}


@dataclass(frozen=True)
class PolicyDecision:
    outcome: PolicyOutcome
    sandbox_required: bool
    sandbox_policy_factory: str | None
    deny_reason: str | None
    capability_class: str
    audit_fields: dict[str, str]


def build_policy_tool_denied_event(
    *,
    tool: str,
    capability_class: str,
    reason: str,
    ts: str | None = None,
) -> dict[str, str]:
    """Build audit payload for ``policy.tool_denied`` events."""
    return {
        "ts": ts or utc_now_iso(),
        "tool": tool,
        "capability_class": capability_class,
        "reason": reason,
    }


def _audit_fields(tool: str, capability_class: str) -> dict[str, str]:
    return {"tool": tool, "capability_class": capability_class}


def _confirmation_required(tool_name: str, origin: str) -> bool:
    if tool_name in confirmation_required_tools:
        return True
    if origin in confirmation_exempt_origins:
        return False
    return origin.startswith(confirmation_required_origins)


def authorize_tool(
    tool_name: str,
    *,
    confirmation_gate: bool,
    capability_class: str | None = None,
    origin: str = "",
) -> PolicyDecision:
    """Classify a tool call and return the policy decision before safety/execute.

    ``capability_class`` is the class the tool's descriptor declared (#84). The
    name-keyed spec table cannot key a namespaced skill/extension name, so
    without it every such tool would classify as the default ``read_only``.
    Declarable classes are all allow-without-sandbox today, so for them the
    value is audit-only; the deny set and the sandbox branch stay core-keyed.

    ``origin`` is the descriptor's, and gates the tools whose names are generated
    per deployment — a remote agent's, which no list can name in advance.
    """
    name = tool_name.strip()
    spec = spec_for_tool(name)
    capability = str(capability_class or "").strip() or spec.capability_class

    if spec.surface == "excluded":
        reason = f"Tool {name!r} is excluded from the headless production surface."
        return PolicyDecision(
            outcome="deny",
            sandbox_required=False,
            sandbox_policy_factory=None,
            deny_reason=reason,
            capability_class=capability,
            audit_fields=_audit_fields(name, capability),
        )

    if capability in _HEADLESS_DENIED_CLASSES:
        reason = f"Tool {name!r} has capability_class={capability!r} which is denied in headless production."
        return PolicyDecision(
            outcome="deny",
            sandbox_required=False,
            sandbox_policy_factory=None,
            deny_reason=reason,
            capability_class=capability,
            audit_fields=_audit_fields(name, capability),
        )

    sandboxed = capability == "skill"
    return PolicyDecision(
        outcome="confirm_pending" if confirmation_gate and _confirmation_required(name, origin) else "allow",
        sandbox_required=sandboxed,
        sandbox_policy_factory=_SKILL_SANDBOX_FACTORY.get(name) if sandboxed else None,
        deny_reason=None,
        capability_class=capability,
        audit_fields=_audit_fields(name, capability),
    )


__all__ = [
    "PolicyDecision",
    "authorize_tool",
    "build_policy_tool_denied_event",
]
