"""Owner slash-command handlers for the supervisor loop."""

from __future__ import annotations

import logging
from typing import Any

from config_registry.user_settings.reader import REASONING_PROGRESS_KEY, user_settings_factory
from new_core.identity import SURFACES_WITH_A_PERSON, OwnerScope
from ouroboros.app.task_stop_notifications import TaskStopNotifier
from ouroboros.contracts.chat_commands import (
    REASONING_COMMAND,
    TYPED_ANSWER_SURFACES,
    ConfirmationAnswer,
    reasoning_visibility,
    retired_slash_command,
    runtime_command_parts,
)
from ouroboros.contracts.tool_confirmation import (
    CONFIRMATION_APPROVED,
    CONFIRMATION_DECISIONS,
)
from ouroboros.storage.runtime_backend import STORE_FAILURES
from ouroboros.storage.runtime_models import ACTIVE_STATUSES
from ouroboros.storage.task_results import TERMINAL_STATUSES, job_summary
from ouroboros.storage.tasks.interfaces import ITaskRepository
from ouroboros.storage.tool_confirmation.interfaces import IToolConfirmationRepository
from ouroboros.utils import utc_now_iso
from supervisor.orchestrator.intent import SubmissionMode, TaskIntentKind
from supervisor.orchestrator.orchestrator import SubmitResult
from supervisor.state import status_text
from transport.interfaces import InboundEnvelope

log = logging.getLogger(__name__)

_CONFIRMATION_USAGE = "Ответ не распознан: нужно /confirm <id> да или нет."
_CONFIRMATION_ON_CARD = "Подтвердите или отклоните действие кнопкой на карточке запроса."
_CONFIRMATION_NOTHING_OPEN = "Нечего подтверждать: активного запроса нет."
_CONFIRMATION_NOT_THIS_ONE = "Этот запрос уже не ждёт ответа."
_CONFIRMATION_RUN_GONE = "Задача, ждавшая ответа, уже завершилась — действие не выполнено."
_CONFIRMATION_ALREADY_DECIDED = "Этот запрос уже решён."
_CONFIRMATION_TOO_LATE = "Время на подтверждение истекло — действие не выполнено."
_CONFIRMATION_NOT_RECORDED = "Ответ не записан: хранилище недоступно. Ответьте ещё раз."
_CONFIRMATION_APPROVED_REPLY = "Подтверждено."
_CONFIRMATION_DENIED_REPLY = "Отклонено."

_REASONING_HIDDEN = "Скрываю размышления. Подействует на следующей задаче, вернуть — /reasoning on."
_REASONING_SHOWN = "Показываю размышления. Подействует на следующей задаче."
_REASONING_USAGE = "Нужен аргумент: /reasoning on или /reasoning off."

_RETIRED_COMMAND = "Команда {command} не поддерживается."

_STOP_NO_ACTIVE_TASK = "Активной задачи нет."
#: A stop is honoured at the run's next check rather than at once, so the reply says so
#: instead of claiming the task is already over.
_STOP_REQUESTED = "🛑 Остановка запрошена. Задача завершится на ближайшей безопасной точке."
_STOP_ALREADY_REQUESTED = "🛑 Остановка уже запрошена — задача завершается."
_JOB_STATUS_LABELS = {
    "queued": "в очереди",
    "running": "выполняется",
    "stopping": "останавливается",
    "succeeded": "выполнена",
    "failed": "завершилась с ошибкой",  # noqa: RUF001
    "cancelled": "остановлена",
}


def handle_slash_command(
    command: str,
    *,
    ctx: Any,
    scope: OwnerScope,
    source: str,
    transport: dict[str, Any] | None = None,
    envelope: InboundEnvelope | None = None,
) -> bool | SubmitResult:
    """Run a slash command; return its admission result or whether it was handled.

    ``scope`` is the commanding message's own, carried down from the envelope: a command
    is answered in the conversation it was issued in.
    """
    cmd, argument = runtime_command_parts(command)
    if cmd == "/new":
        return (
            ctx.orchestrator.submit_detached(envelope, argument)
            if envelope is not None
            else SubmitResult(
                TaskIntentKind.AGENT_CHAT,
                SubmissionMode.NONE,
                message="После команды /new укажите задачу.",
            )
        )
    if cmd == "/jobs":
        rows = ctx.tasks_repo.list_current_jobs(scope=scope, limit=21, active_only=True)
        lines = [
            f"{row['job_id']} — {_job_status_label(row['status'])}; создана: {row['created_at']}; "
            f"запущена: {row['started_at'] or 'ещё нет'}; {row['preview']}"
            for row in (job_summary(record) for record in rows[:20])
        ]
        if len(rows) > 20:
            lines.append("…показаны не все задачи")
        ctx.send_with_budget(
            "\n".join(lines) if lines else "Активных задач нет.",
            **_send_kwargs(scope, source, transport),
        )
        return True
    if cmd == "/stop":
        if argument:
            job_id = argument.strip()
            rows = ctx.tasks_repo.stop_jobs(scope=scope, job_id=job_id)
            TaskStopNotifier(getattr(ctx, "task_stop_publisher", None)).notify(rows)
            if rows:
                root = next((row for row in rows if row.task_type == "task" and not row.parent_task_id), rows[0])
                job_status = job_summary(root)["status"]
                if any(row.status == "running" and row.stop_requested_at for row in rows):
                    reply = f"🛑 Остановка задачи {job_id} запрошена."
                elif any(row.status == "cancelled" for row in rows):
                    reply = f"🛑 Задача {job_id} остановлена."
                elif root.status in TERMINAL_STATUSES:
                    reply = f"Задача {job_id} уже завершена: {_job_status_label(job_status)}."
                else:
                    reply = f"🛑 Остановка задачи {job_id} запрошена."
            else:
                reply = "Задача не найдена."
            ctx.send_with_budget(reply, **_send_kwargs(scope, source, transport))
            return True
        return _handle_stop(ctx, scope=scope, source=source, transport=transport)

    if cmd == "/restart":
        return _handle_restart(ctx, scope=scope, source=source, transport=transport)

    if cmd == REASONING_COMMAND:
        return _handle_reasoning(ctx, command, scope=scope, source=source, transport=transport)

    if cmd == "/status":
        soft_timeout = int(getattr(ctx, "soft_timeout", 600))
        hard_timeout = int(getattr(ctx, "hard_timeout", 1800))
        status = status_text(
            soft_timeout, hard_timeout, scope=scope, tool_confirmation_reader=ctx.tool_confirmation_reader
        )
        ctx.send_with_budget(status, **_send_kwargs(scope, source, transport))
        return True

    retired = retired_slash_command(command)
    if retired:
        ctx.send_with_budget(_RETIRED_COMMAND.format(command=retired), **_send_kwargs(scope, source, transport))
        return True

    return False


def _job_status_label(status: str) -> str:
    return _JOB_STATUS_LABELS.get(str(status), "статус неизвестен")


def _handle_stop(
    ctx: Any,
    *,
    scope: OwnerScope,
    source: str,
    transport: dict[str, Any] | None = None,
) -> bool:
    """Stop every active job tree rooted in the commanding chat."""
    store = ctx.tasks_repo
    active = store.list_tasks(scope=scope, statuses=ACTIVE_STATUSES, chat_only=True, limit=None)
    unstopped_ids = {row.task_id for row in active if not row.stop_requested_at}
    stopped = store.stop_jobs(scope=scope)
    TaskStopNotifier(getattr(ctx, "task_stop_publisher", None)).notify(stopped)
    applied = any(
        row.task_id in unstopped_ids and (row.stop_requested_at or row.status == "cancelled") for row in stopped
    )
    still_stopping = any(row.status in {"running", "queued"} and row.stop_requested_at for row in stopped)
    ctx.send_with_budget(
        _STOP_REQUESTED if applied else (_STOP_ALREADY_REQUESTED if still_stopping else _STOP_NO_ACTIVE_TASK),
        **_send_kwargs(scope, source, transport),
    )
    return True


def handle_confirmation_reply(
    answer: ConfirmationAnswer | None,
    *,
    ctx: Any,
    scope: OwnerScope,
    source: str,
    transport: dict[str, Any] | None = None,
) -> None:
    """Record the owner's answer to the request it was read as answering; only a write that applied queues the run."""
    if answer is None:
        if scope.surface_id in TYPED_ANSWER_SURFACES:
            reply = _CONFIRMATION_USAGE
        elif scope.surface_id in SURFACES_WITH_A_PERSON:
            reply = _CONFIRMATION_ON_CARD
        else:
            reply = _CONFIRMATION_NOTHING_OPEN
        ctx.send_with_budget(reply, **_send_kwargs(scope, source, transport))
        return
    try:
        reply = _record_confirmation_reply(
            answer,
            scope=scope,
            tasks_repo=ctx.tasks_repo,
            tool_confirmation_reader=ctx.tool_confirmation_reader,
        )
    except STORE_FAILURES:
        # Said rather than raised: raising retries the envelope, while the person who owns the
        # decision is right there and can answer again.
        log.warning("Failed to record the owner's confirmation reply", exc_info=True)
        reply = _CONFIRMATION_NOT_RECORDED
    ctx.send_with_budget(reply, **_send_kwargs(scope, source, transport))


def _record_confirmation_reply(
    answer: ConfirmationAnswer,
    *,
    scope: OwnerScope,
    tasks_repo: ITaskRepository,
    tool_confirmation_reader: IToolConfirmationRepository,
) -> str:
    """Take the decision the answer carries, and return what the owner is told about it."""
    open_request = tool_confirmation_reader.read_open_tool_confirmation(scope)
    if open_request is None:
        return _CONFIRMATION_NOTHING_OPEN
    if not answer.confirmation_id or not open_request.confirmation_id.startswith(answer.confirmation_id):
        # Deciding the open request instead would allow a call this answer was never given for.
        return _CONFIRMATION_NOT_THIS_ONE

    applied = tool_confirmation_reader.resolve_tool_confirmation(
        open_request.confirmation_id,
        decision=answer.decision,
        now_iso=utc_now_iso(),
    )
    if not applied:
        if (
            tool_confirmation_reader.read_tool_confirmation_status(open_request.confirmation_id)
            in CONFIRMATION_DECISIONS
        ):
            return _CONFIRMATION_ALREADY_DECIDED
        stopped = tasks_repo.read_task_stop_state(open_request.task_id)
        return _CONFIRMATION_RUN_GONE if stopped.stop_requested or stopped.is_terminal else _CONFIRMATION_TOO_LATE

    log.info(
        "event=tool_confirmation_resolved task_id=%s tool=%s confirmation_id=%s decision=%s",
        open_request.task_id,
        open_request.tool_name,
        open_request.confirmation_id,
        answer.decision,
    )
    tool_confirmation_reader.requeue_parked_task(open_request.task_id)
    return _CONFIRMATION_APPROVED_REPLY if answer.decision == CONFIRMATION_APPROVED else _CONFIRMATION_DENIED_REPLY


def _handle_reasoning(
    ctx: Any,
    command: str,
    *,
    scope: OwnerScope,
    source: str,
    transport: dict[str, Any] | None = None,
) -> bool:
    send_kwargs = _send_kwargs(scope, source, transport)
    visible = reasoning_visibility(command)
    if visible is None:
        ctx.send_with_budget(_REASONING_USAGE, **send_kwargs)
        return True

    user_settings_factory.settings().set_override(scope, REASONING_PROGRESS_KEY, "true" if visible else "false")
    ctx.send_with_budget(_REASONING_SHOWN if visible else _REASONING_HIDDEN, **send_kwargs)
    return True


#: `/restart` means "do not resume mine", and a queued record left behind is claimed again
#: the moment the process is back — which is a restart that resumes. The instance drain does
#: not cover it either: it hands back what it holds and leaves the queue alone.
_RESTART_CANCELLED_STATUSES = list(ACTIVE_STATUSES)


def _cancel_own_runs(ctx: Any, *, scope: OwnerScope) -> None:
    """Close the asker's own running and not-yet-started records, and nobody else's."""
    store = ctx.tasks_repo
    for record in store.list_tasks(statuses=_RESTART_CANCELLED_STATUSES, scope=scope, limit=None):
        store.stop_jobs(scope=scope, job_id=record.job_id)
        store.cancel_task(record.task_id, result="Owner restart stopped this task before process restart.")


def _handle_restart(
    ctx: Any,
    *,
    scope: OwnerScope,
    source: str,
    transport: dict[str, Any] | None = None,
) -> bool:
    send_kwargs = _send_kwargs(scope, source, transport)
    ok, restart_msg = ctx.safe_restart()
    if not ok:
        ctx.send_with_budget(f"⚠️ Restart cancelled: {restart_msg}", **send_kwargs)
        return True
    ctx.send_with_budget("♻️ Restarting.", **send_kwargs)

    # No no-resume marker is written here. It was two files under `state/` on the local
    # filesystem — never through the storage provider, so the shared backend never held
    # them — that nothing in the tree ever read back, while a failure to write one
    # cancelled the restart the owner asked for. Under a fleet each instance would write
    # its own copy and none would read one. What makes the restart not resume is the
    # cancellation below, which is a record every instance shares.
    try:
        # The asker's own runs are cancelled rather than released: they asked for a restart
        # that does not resume, which is the opposite of handing the work to another
        # instance. Everything else the runtime holds belongs to other owners, and the
        # process-wide drain the restart request starts hands those back rather than
        # closing them on their behalf.
        _cancel_own_runs(ctx, scope=scope)
    except Exception:
        log.warning("Restart cancelled because the asker's runs could not be closed", exc_info=True)
        try:
            ctx.send_with_budget("⚠️ Restart cancelled: failed to stop the running tasks.", **send_kwargs)
        except Exception:
            pass
        return True

    try:
        ctx.send_with_budget(
            "Stopping active task. New settings apply to the next message.",
            **send_kwargs,
        )
    except Exception:
        log.warning("Failed to send owner restart stop notice; continuing restart", exc_info=True)

    ctx.request_restart()
    return True


def _send_kwargs(scope: OwnerScope, source: str, transport: dict[str, Any] | None) -> dict[str, Any]:
    # Identity and routing, side by side: the scope says whose conversation the reply
    # belongs to, the source says which transport carries it. Never omit the source —
    # the reading transport decides delivery from it, and an absent one means the answer
    # reaches nobody or everybody.
    resolved = str(source or "").strip() or "unknown"
    if resolved == "unknown":
        # No transport claims it, so this reply is about to vanish; say so once.
        log.warning("Command reply for chat %s has no deliverable source", scope.chat_id)
    kwargs: dict[str, Any] = {"scope": scope, "source": resolved}
    if transport:
        kwargs["transport"] = dict(transport)
    return kwargs


__all__ = [
    "handle_confirmation_reply",
    "handle_slash_command",
]
