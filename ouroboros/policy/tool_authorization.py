"""Tool Policy Engine authorization SSOT (allow / deny / sandbox-required)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from ouroboros.tools.catalog import spec_for_tool
from ouroboros.tools.spec import CapabilityClass
from ouroboros.utils import utc_now_iso

PolicyOutcome = Literal["allow", "deny", "confirm_pending"]

_HEADLESS_DENIED_CLASSES: frozenset[CapabilityClass] = frozenset({"shell", "git", "control"})

_SKILL_SANDBOX_FACTORY: dict[str, str] = {
    "skill_exec": "skill_default",
    "skill_preflight": "preflight_default",
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


def authorize_tool(
    tool_name: str,
    args: dict[str, Any] | None = None,
    ctx: Any | None = None,
    *,
    capability_class: str | None = None,
) -> PolicyDecision:
    """Classify a tool call and return the policy decision before safety/execute.

    ``capability_class`` is the class the tool's descriptor declared (#84). The
    name-keyed spec table cannot key a namespaced skill/extension name, so
    without it every such tool would classify as the default ``read_only``.
    Declarable classes are all allow-without-sandbox today, so for them the
    value is audit-only; the deny set and the sandbox branch stay core-keyed.
    """
    del args, ctx
    name = str(tool_name or "").strip()
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

    if capability == "skill":
        factory = _SKILL_SANDBOX_FACTORY.get(name)
        return PolicyDecision(
            outcome="allow",
            sandbox_required=True,
            sandbox_policy_factory=factory,
            deny_reason=None,
            capability_class=capability,
            audit_fields=_audit_fields(name, capability),
        )

    return PolicyDecision(
        outcome="allow",
        sandbox_required=False,
        sandbox_policy_factory=None,
        deny_reason=None,
        capability_class=capability,
        audit_fields=_audit_fields(name, capability),
    )


__all__ = [
    "PolicyDecision",
    "authorize_tool",
    "build_policy_tool_denied_event",
]
