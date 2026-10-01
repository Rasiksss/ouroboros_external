"""PluginAPI registration for assignment delivery."""

from __future__ import annotations

import json
from typing import Any

from ouroboros.storage.settings.schema import AppSettings

from .delivery import deliver_assignment

_SEND_ASSIGNMENT_TIMEOUT_SEC = 360
_PARTIAL_FAILURE_MESSAGE = (
    "Delivery was only partially successful. Some targets may already have received the assignment. "
    "Do not retry automatically. Report the partial delivery to the user and wait for explicit instructions."
)

_SEND_ASSIGNMENT_DESCRIPTION = (
    "Send an assignment through configured assignment delivery channels. "
    "Use it for external-world actions the agent cannot perform directly: "
    "scheduling meetings and calendar entries, sending mail, reservations "
    "and bookings, purchases, or delegating an action to a human "
    "(Russian triggers include 'поставь встречу', 'запиши в календарь', "
    "'забронируй'). Delivery targets, credentials, dates, priority, and "
    "attachments are fixed by configuration and are not visible or "
    "configurable. Only a result with status=sent confirms delivery. "
    "A partial failure may already have delivered to some targets and must "
    "not be retried automatically."
)

# Exactly the SKILL.md env_from_settings keys; get_settings is the sanctioned,
# manifest-gated path to settings, so delivery never reads the settings store.
_SETTING_KEYS = (
    "ASSISTANT_ASSIGNMENT_SMTP_HOST",
    "ASSISTANT_ASSIGNMENT_SMTP_PORT",
    "ASSISTANT_ASSIGNMENT_SMTP_USERNAME",
    "ASSISTANT_ASSIGNMENT_SMTP_PASSWORD",
    "ASSISTANT_ASSIGNMENT_MAIL_FROM",
    "ASSISTANT_ASSIGNMENT_MAIL_TO",
    "ASSISTANT_ASSIGNMENT_SMTP_STARTTLS_REQUIRED",
    "ASSISTANT_ASSIGNMENT_SMTP_ROOT_CERTIFICATES",
    "ASSISTANT_ASSIGNMENT_SMTP_CLIENT_CERTIFICATE",
    "ASSISTANT_ASSIGNMENT_SMTP_TLS_VERSION",
    "ASSISTANT_ASSIGNMENT_SMTP_VERIFY_TLS",
    "ASSISTANT_ASSIGNMENT_SMTP_TIMEOUT_SEC",
    "ASSISTANT_ASSIGNMENT_MAX_ATTEMPTS",
    "ASSISTANT_ASSIGNMENT_RETRY_MIN_SECONDS",
    "ASSISTANT_ASSIGNMENT_RETRY_MAX_SECONDS",
    "ASSISTANT_ASSIGNMENT_DELIVERY_TIMEOUT_SEC",
    "DIALOGS_GRPC_ENDPOINT",
    "ASSISTANT_DIALOGS_BOT_TOKEN",
    "ASSISTANT_DIALOGS_BOT_GROUP_ID",
    "DIALOGS_SECURE",
    "DIALOGS_ROOT_CERTIFICATES",
)

_SEND_ASSIGNMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {
            "type": "string",
            "description": "Short assignment subject.",
        },
        "body": {
            "type": "string",
            "description": "Assignment body text.",
        },
    },
    "required": ["subject", "body"],
    "additionalProperties": False,
}


def _tool_error(message: str) -> str:
    return f"⚠️ TOOL_ERROR (send_assignment): {str(message).removeprefix('⚠️ ').strip()}"


def _send_assignment(
    api: Any,
    ctx: Any,
    subject: str = "",
    body: str = "",
    retry_assignment_id: str = "",
    **kwargs: Any,
) -> str:
    if kwargs:
        unsupported = ", ".join(sorted(str(key) for key in kwargs))
        return f"⚠️ ASSIGNMENT_ARG_ERROR: unsupported arguments: {unsupported}."

    clean_subject = str(subject or "").strip()
    clean_body = str(body or "").strip()
    if not clean_subject:
        return "⚠️ ASSIGNMENT_ARG_ERROR: subject is required."
    if not clean_body:
        return "⚠️ ASSIGNMENT_ARG_ERROR: body is required."
    if str(retry_assignment_id or "").strip():
        return (
            "⚠️ ASSIGNMENT_RETRY_UNAVAILABLE: assignment delivery state is not "
            "persisted, so channel-specific retry is unavailable."
        )

    values = api.get_settings(_SETTING_KEYS)
    try:
        settings = AppSettings(**{k: v for k, v in values.items() if v is not None})
    except Exception as exc:
        # Preserve the cause in server logs (field names only — messages may embed values).
        fields: list[str] = []
        errors = getattr(exc, "errors", None)
        if callable(errors):
            try:
                fields = sorted({str(err["loc"][0]) for err in errors() if err.get("loc")})
            except Exception:
                fields = []
        api.log("error", "assignment delivery settings rejected", error_type=type(exc).__name__, fields=fields)
        return _tool_error(f"ASSIGNMENT_CONFIG_ERROR: invalid assignment delivery settings: {type(exc).__name__}.")
    result = deliver_assignment(
        clean_subject,
        clean_body,
        repo_dir=ctx.repo_dir,
        drive_root=ctx.drive_root,
        settings=settings,
        task_id=str(ctx.task_id or ""),
    )
    public_result = _public_assignment_result(result)
    result_str = json.dumps(public_result, ensure_ascii=False, sort_keys=True)
    if public_result["status"] != "sent":
        result_str = _tool_error(result_str)
    # Safety allow-with-warning prefixing is applied by the core dispatcher now,
    # not here (#84): the plugin returns only the delivery result.
    return result_str


def _public_assignment_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return the stable LLM-visible delivery result without targets or message ids."""
    status = str(result.get("status") or "failed")
    public: dict[str, Any] = {"status": status}
    assignment_id = str(result.get("assignment_id") or "").strip()
    if assignment_id:
        public["assignment_id"] = assignment_id
    if status == "partial_failure":
        public["error"] = _PARTIAL_FAILURE_MESSAGE
    elif status != "sent":
        error = str(result.get("error") or "").strip()
        if error.startswith("ASSIGNMENT_"):
            public["error"] = error
        else:
            public["error"] = "delivery_failed"
    return public


def register(api: Any) -> None:
    def _handler(ctx: Any, **kwargs: Any) -> str:
        return _send_assignment(api, ctx, **kwargs)

    api.register_tool(
        "send_assignment",
        _handler,
        description=_SEND_ASSIGNMENT_DESCRIPTION,
        schema=_SEND_ASSIGNMENT_SCHEMA,
        timeout_sec=_SEND_ASSIGNMENT_TIMEOUT_SEC,
        log_redacted_args=["subject", "body"],
        # External delivery: the core dispatcher runs the safety check (#84).
        safety_policy="check",
        capability_class="external_network",
    )


__all__ = ["register"]
