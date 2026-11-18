"""Session lifecycle state machine."""

from __future__ import annotations

from app.utils.exceptions.common import InvalidStateException

_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "provisioning": {"ready", "failed", "terminating"},
    "ready": {"terminating", "failed"},
    "terminating": {"terminated", "failed"},
    "terminated": {"provisioning"},
    "failed": {"terminating", "provisioning"},
}

_ACTION_ALLOWED_STATES: dict[str, set[str]] = {
    "get": {"provisioning", "ready", "terminating", "terminated", "failed"},
    "terminate": {"provisioning", "ready", "terminating", "failed", "terminated"},
    "exec": {"ready"},
    "upload": {"ready"},
    "download": {"ready"},
    "list_files": {"ready"},
}


def ensure_transition(current_status: str, next_status: str) -> None:
    allowed = _ALLOWED_TRANSITIONS.get(current_status, set())
    if next_status not in allowed:
        raise InvalidStateException(
            f"transition {current_status!r} -> {next_status!r} is not allowed",
            details={"from": current_status, "to": next_status},
        )


def ensure_action_allowed(current_status: str, action: str) -> None:
    allowed = _ACTION_ALLOWED_STATES.get(action, set())
    if current_status not in allowed:
        raise InvalidStateException(
            f"action {action!r} is not allowed for status {current_status!r}",
            details={"status": current_status, "action": action},
        )
