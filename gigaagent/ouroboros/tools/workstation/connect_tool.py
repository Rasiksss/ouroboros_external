"""``workstation_connect`` — explicitly create or resume a WorkStation session for this conversation.

The five ``file_*``/``code_exec``/``apply_patch`` tools are gated on
``ctx.workstation``; this tool is the only way to set it.  Calling it
again in the same turn is a no-op that returns the existing session info.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema

if TYPE_CHECKING:
    from ouroboros.egress.workstation.handle import WorkstationHandle

log = logging.getLogger(__name__)

_SESSION_TIMEOUT_SEC = 8

_SCHEMA = ToolSchema(
    name="workstation_connect",
    description=(
        "Create or resume an isolated AEF WorkStation sandbox session for this "
        "conversation. Must be called before using code_exec, file_read, "
        "file_write, file_edit, or apply_patch. Repeated calls in the same "
        "turn are no-ops that return the existing session info."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
)


class WorkstationUnavailable(Exception):
    """No session could be attached; the message is what the model is told."""


def attach_session(ctx: ToolContext) -> WorkstationHandle:
    """Put a session on ``ctx.workstation`` and return the handle; a resumed run reattaches through it too."""
    from ouroboros.storage.settings.store import load_settings

    settings = load_settings()
    base_url = str(getattr(settings, "WORKSTATION_BASE_URL", "") or "").strip()
    if not base_url:
        raise WorkstationUnavailable(
            "⚠️ WORKSTATION_UNAVAILABLE: WORKSTATION_BASE_URL is not configured. "
            "WorkStation tools (code_exec, file_read, file_write, file_edit, "
            "apply_patch) are not available in this environment."
        )

    from ouroboros.egress.workstation import scope_key_for, workstation_manager
    from ouroboros.egress.workstation.client import WorkstationConfig

    config = WorkstationConfig.from_settings(settings)
    if config is None:
        raise WorkstationUnavailable("⚠️ WORKSTATION_UNAVAILABLE: could not build WorkStation config from settings.")

    scope_key = scope_key_for(ctx.scope.owner_id, ctx.scope.chat_id, ctx.scope.agent_id)

    try:
        handle = workstation_manager.get_or_create(
            scope_key=scope_key,
            config=config,
            timeout_sec=_SESSION_TIMEOUT_SEC,
        )
    except Exception as exc:
        log.debug("WorkStation session connect failed", exc_info=True)
        raise WorkstationUnavailable(
            "⚠️ WORKSTATION_UNAVAILABLE: failed to create or resume a sandbox "
            "session. The WorkStation service may be temporarily unreachable."
        ) from exc

    ctx.workstation = handle

    try:
        from ouroboros.egress.workstation.attachments_sync import sync_turn_attachments

        sync_turn_attachments(handle, ctx)
    except Exception:
        log.debug("WorkStation attachment sync failed", exc_info=True)

    return handle


def _handle_workstation_connect(ctx: ToolContext, **_kwargs: Any) -> str:
    if ctx.workstation is not None:
        return f"OK: WorkStation session already active (session={ctx.workstation.record.session_id})."

    try:
        handle = attach_session(ctx)
    except WorkstationUnavailable as exc:
        return str(exc)

    return (
        f"OK: WorkStation session active (session={handle.record.session_id}). "
        f"Workspace: {handle.config.workspace_root}. "
        f"Access level: {handle.config.access_level}. "
        f"You can now use code_exec, file_read, file_write, file_edit, "
        f"and apply_patch."
    )


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="workstation_connect",
            schema=_SCHEMA,
            handler=_handle_workstation_connect,
            timeout_sec=30,
        ),
    ]


__all__ = ["attach_session", "get_tools"]
