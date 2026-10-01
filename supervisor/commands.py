"""Owner slash-command handlers for the supervisor loop."""

from __future__ import annotations

import logging
import pathlib
import uuid
from typing import Any

from supervisor.runtime_commands import runtime_slash_command
from supervisor.state import status_text

log = logging.getLogger(__name__)

_STOP_OWNER_MESSAGE = (
    "Stop current work. Treat this as the user's priority instruction: abandon the old plan and "
    "take no further action on it. Use `recent_tasks` to find unfinished child tasks; request "
    "needed cancellations with `cancel_task`. Do not cancel the current foreground task; finish "
    "this flow. Reply in your own words with completed work, unfinished work, requested "
    "cancellations, and irreversible actions. Never say cancellation was confirmed when it was "
    "only requested."
)


def handle_slash_command(
    command: str,
    *,
    chat_id: Any,
    ctx: Any,
    source: str = "",
    transport: dict[str, Any] | None = None,
) -> bool:
    """Run a slash command. Returns True if handled."""
    cmd = runtime_slash_command(command)
    if cmd == "/stop":
        return _handle_stop(chat_id, ctx, source=source, transport=transport)

    if cmd == "/restart":
        return _handle_restart(chat_id, ctx, source=source, transport=transport)

    if cmd == "/status":
        executor = getattr(ctx, "executor", None)
        workers_view = executor if executor is not None else getattr(ctx, "WORKERS", {})
        soft_timeout = int(getattr(ctx, "soft_timeout", 600))
        hard_timeout = int(getattr(ctx, "hard_timeout", 1800))
        status = status_text(workers_view, soft_timeout, hard_timeout)
        ctx.send_with_budget(
            chat_id,
            status,
            force_budget=True,
            **_send_kwargs(source, transport),
        )
        return True

    return False


def _handle_stop(
    chat_id: Any,
    ctx: Any,
    *,
    source: str = "",
    transport: dict[str, Any] | None = None,
) -> bool:
    slot = ctx.executor.get_slot(0)
    if slot is not None and slot.busy_task_id:
        agent = ctx.orchestrator._agent_getter()
        if agent is not None and getattr(agent, "_busy", False):
            agent.inject_message(_STOP_OWNER_MESSAGE)
            return True

    ctx.send_with_budget(
        chat_id,
        "Активной задачи нет.",
        **_send_kwargs(source, transport),
    )
    return True


def _handle_restart(
    chat_id: Any,
    ctx: Any,
    *,
    source: str = "",
    transport: dict[str, Any] | None = None,
) -> bool:
    send_kwargs = _send_kwargs(source, transport)
    ctx.send_with_budget(chat_id, "♻️ Restarting.", **send_kwargs)
    ok, restart_msg = ctx.safe_restart(reason="owner_restart", unsynced_policy="rescue_and_reset")
    if not ok:
        ctx.send_with_budget(chat_id, f"⚠️ Restart cancelled: {restart_msg}", **send_kwargs)
        return True

    drive_root = pathlib.Path(getattr(ctx, "drive_root", "") or getattr(ctx, "DRIVE_ROOT", ""))
    state_dir = drive_root / "state"
    owner_restart_flag = state_dir / "owner_restart_no_resume.flag"
    stable_skip_flag = state_dir / "panic_stop.flag"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        owner_restart_flag.write_text("owner_restart", encoding="utf-8")
        stable_skip_flag.write_text("owner_restart_no_resume", encoding="utf-8")
    except Exception:
        owner_restart_flag.unlink(missing_ok=True)
        stable_skip_flag.unlink(missing_ok=True)
        log.warning("Failed to write owner restart no-resume flag", exc_info=True)
        ctx.send_with_budget(chat_id, "⚠️ Restart cancelled: could not write restart state.", **send_kwargs)
        return True

    try:
        ctx.executor.shutdown(
            result_status="cancelled",
            result_reason="Owner restart stopped this task before process restart.",
        )
    except Exception:
        owner_restart_flag.unlink(missing_ok=True)
        stable_skip_flag.unlink(missing_ok=True)
        log.warning("Restart cancelled because worker shutdown failed", exc_info=True)
        try:
            ctx.send_with_budget(chat_id, "⚠️ Restart cancelled: failed to stop workers.", **send_kwargs)
        except Exception:
            pass
        return True

    try:
        ctx.send_with_budget(
            chat_id,
            "Stopping active task. New settings apply to the next message.",
            **send_kwargs,
        )
    except Exception:
        log.warning("Failed to send owner restart stop notice; continuing restart", exc_info=True)

    ctx.request_restart()
    return True


def _send_kwargs(source: str, transport: dict[str, Any] | None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"source": source} if source else {}
    if transport:
        kwargs["transport"] = dict(transport)
    return kwargs


def handle_agent_restart_request(evt: dict[str, Any], ctx: Any) -> None:
    state = ctx.state_repo.load()
    send_kwargs = _send_kwargs(str(getattr(state, "owner_chat_source", "") or ""), None)
    if state.owner_chat_id is not None:
        ctx.send_with_budget(
            state.owner_chat_id,
            f"♻️ Restart requested by agent: {evt.get('reason')}",
            **send_kwargs,
        )
    ok, msg = ctx.safe_restart(reason="agent_restart_request", unsynced_policy="rescue_and_reset")
    if not ok:
        if state.owner_chat_id is not None:
            ctx.send_with_budget(state.owner_chat_id, f"⚠️ Restart skipped: {msg}", **send_kwargs)
        return
    ctx.executor.shutdown()
    with ctx.state_repo.mutate() as state:
        state.session_id = uuid.uuid4().hex
    ctx.state_repo.sync_runtime_session(ctx.store)
    ctx.request_restart()


__all__ = ["handle_agent_restart_request", "handle_slash_command"]
