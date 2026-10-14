"""Supervisor background loop: ingress → orchestrator → store, and the reports back.

What it does with a task after enqueuing it is reads and writes of the task record. It
claims nothing, starts no run, holds no lane and imports nothing that does.
"""

from __future__ import annotations

import contextvars
import logging
import pathlib
import random
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any

from background_memory.infra.events import memory_event_publisher_factory
from background_memory.interfaces import MemoryDistillationEvent, MemoryTaskRef
from new_core.coordination.interfaces import ITaskStopPublisher, IWorkSignal
from new_core.identity import OwnerScope, ServiceJob
from new_core.loop import CancellationSignal
from ouroboros.agent_events.interfaces import IAgentEventConsumer
from ouroboros.contracts.owner_scope import service_identity
from ouroboros.observability import aef_tracing
from ouroboros.observability.health import LoopHeartbeat
from ouroboros.storage.blob.factory import get_storage_provider
from ouroboros.storage.chat_attachments import cleanup_expired_attachments
from ouroboros.storage.chat_store import mark_chat_message_processed
from ouroboros.storage.conversations.interfaces import IConversationRepository
from ouroboros.storage.execution_memory.interfaces import IExecutionMemoryRepository
from ouroboros.storage.file_catalog import FileTransferPending
from ouroboros.storage.result_delivery.interfaces import IResultDeliveryRepository
from ouroboros.storage.runtime_backend import STORE_FAILURES, TERMINAL_CLAIM_MAX_BATCH, store_deadline
from ouroboros.storage.runtime_models import OwnerRuntimeState, TaskRecord, is_dialogs_source
from ouroboros.storage.settings.store import load_settings
from ouroboros.storage.task_episodes import store_task_episode
from ouroboros.storage.tasks.interfaces import ITaskRepository
from ouroboros.storage.tool_confirmation.interfaces import IToolConfirmationRepository
from ouroboros.utils import utc_now_iso
from supervisor.commands import handle_confirmation_reply, handle_slash_command
from supervisor.interfaces.cleanup_checkpoint import CleanupCheckpoint, ICleanupCheckpointRepository
from supervisor.interfaces.usage import IUsageRepository
from supervisor.interfaces.working_retrieval import IWorkingRetrievalRepository
from supervisor.message_bus import LocalChatBridge
from supervisor.orchestrator.intent import SubmissionMode, TaskIntentKind
from supervisor.orchestrator.orchestrator import (
    SubmissionCommitUnknown,
    SubmitResult,
    TaskOrchestrator,
    legacy_image_envelope,
    stored_owner_message,
)
from supervisor.queue import TaskQueue
from supervisor.runtime_context import SupervisorRuntime, set_runtime
from supervisor.state import RuntimeStateRepository, rotate_chat_log_if_needed
from transport.interfaces import IInbox, IMessageGateway, InboundEnvelope, routed_chat_id, routed_value

if TYPE_CHECKING:
    from ouroboros.scheduling.dispatch import ScheduleDispatcher

log = logging.getLogger(__name__)
# Bounded so a flood on the inbox cannot starve task assignment or the event queue.
_INBOX_DRAIN_LIMIT = 100
# The reporting seam is drained under two bounds, because it runs long in two different
# ways and either one alone leaves the tick unbounded.
#
# The count stops a fast *producer*: every agent-runtime instance in the deployment reports
# onto this one seam, so "poll until it returns nothing" is a condition several busy
# producers keep false indefinitely.
#
# The budget stops a slow *handler*: a `SubtaskRequested` runs the duplicate check, which
# calls a model on this very thread under its own 15 s deadline (`supervisor/events.py`), so
# a hundred of those would hold one tick for the better part of half an hour — no timeout
# enforced, no lease reclaimed, no inbox drained, and the process still answering ready.
#
# Whichever is reached first ends the drain; the budget is checked after a handler returns,
# so one handler may overrun it by its own deadline and `TICK_STOP_BUDGET_SEC` accounts for
# exactly that.
_AGENT_EVENT_DRAIN_LIMIT = 100
_AGENT_EVENT_DRAIN_BUDGET_SEC = 5.0
_TERMINAL_TICK_BUDGET_SEC = 5.0
_LEGACY_RECOVERY_FAILURES: tuple[type[Exception], ...] = (ValueError, RuntimeError, *STORE_FAILURES)

# A drain that stops at a bound reports itself, at most this often. It is a state rather
# than an event — it persists until the producers slow down — so a report per tick would be
# a line every second for as long as the condition lasts.
_DRAIN_SATURATION_REPORT_SEC = 60.0

#: What a composition root must give a stopping loop before it abandons the thread: the
#: drain's budget, one handler overrunning it on its own deadline, one inbox poll and the
#: sweeps. A root that joined for less would leave a daemon thread mid-write.
#: `tests/supervisor/test_supervisor_regressions.py` pins it against the numbers it is derived from, so
#: a step that grows cannot silently outlive the join.
TICK_STOP_BUDGET_SEC = 45.0
#: Silence liveness calls a hung loop: one tick under its stop budget, and the same again for
#: the crash backoff and the probe's own sampling. Derived, so a step that grows cannot stall.
LOOP_STALL_SEC = 2 * TICK_STOP_BUDGET_SEC
# The tick's only blocking wait, so it paces the loop.
_INBOX_POLL_TIMEOUT_SEC = 1.0
_WORKING_CLEANUP_TICK_BUDGET_SEC = 1.0
_ATTACHMENT_CLEANUP_INTERVAL_SECONDS = 15 * 60
#: The boundary label the cleanup sweep names when it originates its own operation.
_TRACE_SOURCE = "attachment-cleanup"
_ATTACHMENT_CLEANUP_JOIN_SECONDS = TICK_STOP_BUDGET_SEC
_SCHEDULE_TRACE_SOURCE = "schedule-dispatch"
_SCHEDULE_DISPATCH_JOIN_SECONDS = 1.0


class SupervisorShutdownError(RuntimeError):
    """Consumers remain alive; their clients must stay open until process termination."""


#: The local operator console: one fixed chat id, no strangers on it.
_OPERATOR_CONSOLE_SOURCE = "browser"
#: Sources a real owner speaks through, beside the dialogs family.
_OWNER_CAPABLE_SOURCES: frozenset[str] = frozenset({_OPERATOR_CONSOLE_SOURCE})
_COMMAND_REFUSAL = "⛔ Runtime commands are accepted only from the owner's own chat."


def is_owner_capable_source(source: str) -> bool:
    """Whether a transport can carry the owner in person.

    An a2a peer and a ``skill:*`` caller speak through the same inbox but are
    never the owner, so neither may claim the identity every notice is routed by.
    """
    text = str(source or "").strip().lower()
    return is_dialogs_source(text) or text in _OWNER_CAPABLE_SOURCES


def _same_source(left: Any, right: Any) -> bool:
    """``dialogs`` and ``sberchat`` are one transport under two labels."""
    first = str(left or "").strip().lower()
    second = str(right or "").strip().lower()
    if not first or not second:
        return False
    return first == second or (is_dialogs_source(first) and is_dialogs_source(second))


def may_run_runtime_command(source: Any, chat_id: Any, *, owner_chat_id: Any, owner_chat_source: Any) -> bool:
    """Who may run the commands the runtime owns.

    The operator console is allowed on its own: it holds one fixed chat id and is
    the only surface a browser-only install has, so tying it to the owner slot
    would lock a dialogs-owned install out of its own commands. Every other
    owner-capable transport also carries strangers, so there the chat has to be
    the owner's.
    """
    text = str(source or "").strip().lower()
    if not is_owner_capable_source(text):
        return False
    if text == _OPERATOR_CONSOLE_SOURCE:
        return True
    return _same_source(text, owner_chat_source) and str(chat_id) == str(owner_chat_id)


def _timestamp(value: Any) -> float:
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _maybe_cleanup_working_retrieval(
    ctx: Any,
    *,
    now_iso: str | None = None,
) -> None:
    try:
        settings = load_settings()
        now = now_iso or utc_now_iso()
        # The instance's own retention sweep has no author, so the widening is spelled out:
        # its checkpoint is a row like any other and has to say whose it is.
        sweep_scope = service_identity(job=ServiceJob.CONTEXT_CLEANUP).as_scope()
        with ctx.cleanup_checkpoint_repository.try_claim() as claimed:
            if not claimed:
                return
            last_run = ctx.cleanup_checkpoint_repository.get_last_run_at(sweep_scope)
            interval = max(1, int(settings.OUROBOROS_CONTEXT_CLEANUP_INTERVAL_HOURS or 24))
            if _timestamp(now) - _timestamp(last_run) < interval * 3600:
                return
            deadline = time.monotonic() + _WORKING_CLEANUP_TICK_BUDGET_SEC
            removed: dict[str, int] = {}
            while True:
                batch = ctx.working_retrieval_repository.cleanup_expired_working_retrieval(
                    now_iso=now,
                    limit=int(settings.OUROBOROS_CONTEXT_CLEANUP_MAX_ROWS or 5000),
                    cleanup_retrieval=settings.OUROBOROS_CONTEXT_CLEANUP_ENABLED,
                )
                for table, count in batch.items():
                    removed[table] = removed.get(table, 0) + count
                if not any(batch.values()):
                    ctx.cleanup_checkpoint_repository.save_checkpoint(
                        CleanupCheckpoint(last_run_at=now, removed=removed), sweep_scope
                    )
                    return
                if time.monotonic() >= deadline:
                    return
    except Exception:
        log.warning("Working-memory cleanup deferred after error", exc_info=True)


class _CleanupCancellation(CancellationSignal):
    def __init__(self, event: threading.Event) -> None:
        self.event = event

    def is_stopped(self) -> bool:
        return self.event.is_set()

    def request_stop(self) -> None:
        self.event.set()


def _worker_loop(
    body: Callable[[], object],
    stop_event: threading.Event,
    *,
    label: str,
    trace_source: str,
    interval: float,
    jitter: float = 0.0,
) -> None:
    while not stop_event.is_set():
        try:
            # One pass, one operation: a worker has no caller to inherit a trace from, and a
            # pass is the unit of work — unscoped, its storage requests carry no `X-Trace-Id`.
            with aef_tracing.trace_scope(aef_tracing.accept_trace_id("", source=trace_source)):
                body()
        except Exception:
            log.warning("%s failed", label, exc_info=True)
        stop_event.wait(interval + random.uniform(0.0, jitter))


def _build_worker_thread(
    body: Callable[[], object],
    stop_event: threading.Event,
    *,
    name: str,
    label: str,
    trace_source: str,
    interval: float,
    jitter: float = 0.0,
) -> threading.Thread:
    return threading.Thread(
        target=_worker_loop,
        args=(body, stop_event),
        kwargs={"label": label, "trace_source": trace_source, "interval": interval, "jitter": jitter},
        name=name,
        daemon=True,
    )


def _stop_worker_thread(
    thread: threading.Thread | None,
    stop_event: threading.Event,
    *,
    label: str,
    timeout: float,
) -> bool:
    stop_event.set()
    if thread is not None:
        thread.join(timeout=timeout)
        if thread.is_alive():
            log.warning("%s thread is still running during shutdown", label)
            return False
    return True


class SchedulerDeliveryError(RuntimeError):
    """A scheduled reminder reached no transport; the slot owes its owner an outcome."""


def _build_schedule_dispatcher(
    ctx: Any,
    gateway: IMessageGateway,
    *,
    batch_limit: int,
    detached_tasks: bool = False,
    should_stop: Callable[[], bool] = lambda: False,
) -> ScheduleDispatcher:
    """Reminders go through `send_with_budget`: it writes history first, so an offline owner keeps them."""
    from config_registry.chat_registry.infra.repositories.repo import SqlChatRegistryStore
    from ouroboros.scheduling.dispatch import ScheduleDispatcher
    from ouroboros.scheduling.store import default_schedule_store

    def send_message(text: str, *, scope: OwnerScope, transport: dict[str, Any]) -> None:
        published = ctx.send_with_budget(text, scope=scope, source=str(scope.surface_id), transport=dict(transport))
        if not published:
            raise SchedulerDeliveryError("reminder was written to history but no transport will deliver it")

    def submit_task(text: str, *, scope: OwnerScope, client_message_id: str, transport: dict[str, Any]) -> None:
        gateway.inbox.submit(
            InboundEnvelope(
                scope=scope,
                user_id=transport.get("target_uid") or routed_chat_id(scope),
                text=text,
                sender_label="Task Scheduler",
                client_message_id=client_message_id,
                # The tick's own trace: a contextvar does not cross the inbox, which in the
                # multi-user mode is another process.
                trace_id=aef_tracing.current_trace_id(),
                # Carries `target_uid`, so the answer reaches the participant who asked
                # rather than the whole group.
                transport=dict(transport),
                # Nobody is waiting in the chat for this one, so it must not hold the owner's
                # foreground lane: `root_claim_sql` serializes non-detached roots per agent.
                detached=detached_tasks,
                # …and for the same reason it must not answer the owner that it was accepted:
                # nobody typed it, and a five-minute schedule would say so 288 times a day.
                suppress_acceptance_notice=True,
            )
        )

    return ScheduleDispatcher(
        default_schedule_store(),
        chats=SqlChatRegistryStore(),
        send_message=send_message,
        submit_task=submit_task,
        batch_limit=batch_limit,
        should_stop=should_stop,
    )


@dataclass
class SupervisorContext:
    drive_root: pathlib.Path
    repo_dir: pathlib.Path
    bridge: LocalChatBridge
    orchestrator: TaskOrchestrator
    agent_events: IAgentEventConsumer
    task_queue: TaskQueue
    state_repo: RuntimeStateRepository
    usage_repository: IUsageRepository
    cleanup_checkpoint_repository: ICleanupCheckpointRepository
    working_retrieval_repository: IWorkingRetrievalRepository
    execution_memory_repository: IExecutionMemoryRepository
    conversations_repo: IConversationRepository
    result_repo: IResultDeliveryRepository
    tool_confirmation_reader: IToolConfirmationRepository
    tasks_repo: ITaskRepository
    send_with_budget: Callable[..., Any]
    send_typing: Callable[..., Any]
    safe_restart: Callable[[], tuple[bool, str]]
    request_restart: Callable[[], None]
    cancel_task_by_id: Callable[[str, OwnerScope], bool]
    enqueue_task: Callable[..., Any]
    update_usage_from_llm: Callable[..., None]
    task_stop_publisher: ITaskStopPublisher
    soft_timeout: int = 600
    hard_timeout: int = 1800

    @property
    def DRIVE_ROOT(self) -> pathlib.Path:
        return self.drive_root

    def load_state(self) -> OwnerRuntimeState:
        return self.state_repo.load()

    def save_state(self, state: OwnerRuntimeState) -> None:
        self.state_repo.save(state)


@dataclass(frozen=True)
class AgentEventDrain:
    """What one pass over the reporting seam did, and whether it left work behind.

    ``saturated`` is the tick's own answer to "is this deployment reporting faster than I
    can drain it", which is why it is returned rather than logged and dropped: it decides
    whether the tick pauses on the inbox afterwards, and it is the only signal an operator
    gets before the seam starts discarding droppable reports.
    """

    handled: int
    saturated: bool


class SupervisorLoop:
    def __init__(
        self,
        *,
        ctx: SupervisorContext,
        stop_event: threading.Event,
        inbox: IInbox,
        heartbeat: LoopHeartbeat,
    ) -> None:
        self.ctx = ctx
        self.stop_event = stop_event
        self.inbox = inbox
        self.heartbeat = heartbeat
        self._pending_inbound: InboundEnvelope | None = None
        self._inbound_result: SubmitResult | None = None
        #: The gate the saturation report is on; monotonic, so it survives a clock change.
        self._next_saturation_report = 0.0
        self._publication_pool: ThreadPoolExecutor | None = None
        self._publication: Future[None] | None = None
        self._publication_wakeup = threading.Event()
        self._publication_deadline = 0.0
        self._handoff_cursor = ""

    def handle_inbound(self, envelope: InboundEnvelope) -> None:
        """The one inbound path: adopt the owner, log the message, submit it.

        Nothing here branches on which transport the message came from, and
        nothing here holds transport state — a cursor, a provider session, a
        transport's tracing spans all belong on the far side of the seam.
        """
        with aef_tracing.trace_scope(aef_tracing.ensure_trace_id(envelope.trace_id)):
            chat_id: int | str = routed_chat_id(envelope.scope)
            raw_user_id = envelope.user_id if envelope.user_id not in (None, "") else chat_id
            user_id: int | str = raw_user_id if isinstance(raw_user_id, int) else routed_value(str(raw_user_id))
            log_text = envelope.effective_text()

            # The row this message's owner holds, not the instance's: adoption and the activity
            # stamp belong to the person who sent it.
            with self.ctx.state_repo.for_scope(envelope.scope).mutate() as state:
                if is_owner_capable_source(envelope.source):
                    # `1/1` is the placeholder a local install starts with; a real chat
                    # claims ownership over it. The whole scope is stored, not a chat id
                    # and a source beside it: this is the one place the owner's own scope
                    # is in hand, and every notice addressed to the owner reads it back.
                    # An upgraded row has an owner and no scope, so its chat reads back as
                    # `None` — still the placeholder, and still claimable.
                    placeholder = state.owner_id == 1 and state.owner_chat_id in ("1", None)
                    # A row written before the scope was stored has an owner and no scope.
                    # "Not yet claimed" has to mean claimable, or an upgraded install would
                    # never address its owner again: no notice, and no runtime command. The
                    # stored owner reclaims it; anyone else is a takeover and is refused,
                    # exactly as it would be for a row that already carries one.
                    unclaimed = state.owner_scope is None and str(state.owner_id) == str(user_id)
                    if state.owner_id is None or placeholder or unclaimed:
                        state.owner_id = user_id
                        state.owner_scope = envelope.scope.to_dict()
                    elif str(state.owner_chat_id) == str(chat_id):
                        # A stale source strands every notice routed by it, so the
                        # owner's own message repairs the stored scope on the way past.
                        state.owner_scope = envelope.scope.to_dict()
                state.last_owner_message_at = utc_now_iso()
                owner_chat_id = state.owner_chat_id
                owner_chat_source = state.owner_chat_source

            from supervisor.message_bus import log_chat

            if not envelope.suppress_chat_log and not envelope.image_base64:
                # Harmless when the transport already persisted it: the stored
                # identity is derived from client_message_id plus actor and scope,
                # and it hashes the chat id as given — coercing it here would write a
                # second row under a chat nobody addressed.
                log_chat(
                    "owner",
                    user_id,
                    log_text,
                    scope=envelope.scope,
                    source=envelope.source,
                    sender_label=envelope.sender_label,
                    sender_session_id=envelope.sender_session_id,
                    client_message_id=envelope.client_message_id,
                    transport=envelope.transport,
                )

            if not log_text and not envelope.image_base64 and not envelope.confirmation_decision:
                return

            result = self.ctx.orchestrator.submit(envelope)
            if result.intent == TaskIntentKind.CONFIRMATION_REPLY:
                # No owner-chat gate: the question was asked in this conversation, and a request
                # is found only under the scope it was opened for.
                handle_confirmation_reply(
                    result.answer,
                    ctx=self.ctx,
                    scope=envelope.scope,
                    source=envelope.source,
                    transport=envelope.transport,
                )
                return
            if result.intent == TaskIntentKind.SLASH_COMMAND:
                if not may_run_runtime_command(
                    envelope.source, chat_id, owner_chat_id=owner_chat_id, owner_chat_source=owner_chat_source
                ):
                    log.warning(
                        "Refused runtime command %r from chat %s on %s", result.slash_command, chat_id, envelope.source
                    )
                    self.ctx.send_with_budget(
                        _COMMAND_REFUSAL,
                        scope=envelope.scope,
                        source=envelope.source,
                        transport=dict(envelope.transport),
                    )
                    return
                command_result = handle_slash_command(
                    result.slash_command,
                    ctx=self.ctx,
                    scope=envelope.scope,
                    source=envelope.source,
                    transport=envelope.transport,
                    envelope=envelope,
                )
                if not isinstance(command_result, SubmitResult):
                    return
                result = command_result
            self._inbound_result = result
            self._send_submission_reply(envelope, result)
            if envelope.image_base64 and result.mode == SubmissionMode.QUEUE:
                message = stored_owner_message(self.ctx.drive_root, envelope)
                if message is None:
                    raise RuntimeError("legacy image journal is absent")
                mark_chat_message_processed(self.ctx.drive_root, message.message_id)

    def _send_submission_reply(
        self, envelope: InboundEnvelope, result: SubmitResult, *, persist_only: bool = False
    ) -> None:
        if not result.message:
            return
        with aef_tracing.trace_scope(result.trace_id or envelope.trace_id):
            if (
                self.ctx.send_with_budget(
                    result.message,
                    fmt="markdown" if result.replay_result else "",
                    scope=envelope.scope,
                    source=envelope.source,
                    transport=result.transport
                    if result.reply_id and not result.replay_result
                    else dict(envelope.transport),
                    task_id=result.task_id,
                    client_message_id=(
                        f"stored-result:{result.task_id}:{envelope.correlation_id}"
                        if result.replay_result
                        else result.reply_id
                    ),
                    persist_only=persist_only,
                    repeat_accepted=envelope.source == "a2a",
                    delivery_only=result.replay_result,
                )
                is False
            ):
                raise RuntimeError(f"Admission handoff deferred for {result.task_id}")

    def process_inbox(self, limit: int = _INBOX_DRAIN_LIMIT, *, timeout: float = _INBOX_POLL_TIMEOUT_SEC) -> int:
        """Retry the held envelope before polling, which settles only completed handling."""
        if self._pending_inbound is not None and self.stop_event.wait(max(0.0, timeout)):
            return 0
        handled = 0
        while handled < limit and not self.stop_event.is_set():
            envelope = self._pending_inbound or self.inbox.poll(timeout if handled == 0 else 0.0)
            if envelope is None:
                break
            envelope = replace(envelope, trace_id=aef_tracing.ensure_trace_id(envelope.trace_id))
            self._pending_inbound = envelope
            self._inbound_result = None
            handled += 1
            with aef_tracing.trace_scope(envelope.trace_id):
                try:
                    self.handle_inbound(envelope)
                except FileTransferPending:
                    log.debug("Inbound file transfer deferred chat=%s source=%s", envelope.chat_id, envelope.source)
                    break
                except Exception as exc:
                    if isinstance(exc, SubmissionCommitUnknown):
                        log.error(
                            "Inbound submission commit is unknown chat=%s source=%s client_message_id=%s task=%s",
                            envelope.chat_id,
                            envelope.source,
                            envelope.client_message_id,
                            exc.task_id,
                        )
                    else:
                        log.error(
                            "Inbound handling deferred chat=%s source=%s client_message_id=%s",
                            envelope.chat_id,
                            envelope.source,
                            envelope.client_message_id,
                            exc_info=True,
                        )
                    try:
                        result = self._inbound_result or self.ctx.orchestrator.resolve_failed_inbound(envelope)
                        self._send_submission_reply(envelope, result, persist_only=True)
                    except Exception:
                        log.error("Failed to persist inbound outcome", exc_info=True)
                        break
            self._pending_inbound = None
        return handled

    def dispatch_agent_events(
        self,
        *,
        limit: int | None = None,
        budget_sec: float | None = None,
    ) -> AgentEventDrain:
        """Hand what runs reported to their handlers, under both of the tick's bounds.

        The defaults are read here rather than bound as argument defaults, so the module's
        numbers are the ones in force and a caller may narrow either for one call.

        The budget is checked *after* a handler returns: it bounds how long the rest of the
        tick waits, and a handler that blocks past it does so under a deadline of its own.
        """
        from supervisor.events import dispatch_event

        ceiling = _AGENT_EVENT_DRAIN_LIMIT if limit is None else limit
        spend_until = time.monotonic() + (_AGENT_EVENT_DRAIN_BUDGET_SEC if budget_sec is None else budget_sec)
        handled = 0
        while handled < ceiling:
            event = self.ctx.agent_events.poll()
            if event is None:
                return AgentEventDrain(handled=handled, saturated=False)
            handled += 1
            dispatch_event(event, self.ctx)
            if time.monotonic() >= spend_until:
                return AgentEventDrain(handled=handled, saturated=True)
        return AgentEventDrain(handled=handled, saturated=True)

    def _report_saturation(self, drain: AgentEventDrain) -> None:
        """Say that reports arrive faster than one tick drains them; on its own gate.

        The one signal that this is happening. What a bound leaves behind is not lost, but a
        producer that stays ahead of the drain fills the seam — and there a droppable report
        is discarded while every other kind blocks the run holding the owner's answer.
        """
        if not drain.saturated:
            return
        now = time.monotonic()
        if now < self._next_saturation_report:
            return
        self._next_saturation_report = now + _DRAIN_SATURATION_REPORT_SEC
        log.warning(
            "Agent-event drain stopped at its bound after %d reports: runs are reporting faster than "
            "this tick drains them",
            drain.handled,
        )

    def run_tick(self) -> int:
        rotate_chat_log_if_needed(self.ctx.drive_root)
        _maybe_cleanup_working_retrieval(self.ctx)
        self._publish_terminal_jobs()
        drain = self.dispatch_agent_events()
        self._report_saturation(drain)

        self.ctx.task_queue.enforce_task_timeouts()
        # After the timeout sweep: a record this instance is about to reclaim may equally
        # be one the timeout just closed, and a terminal record is no longer stale.
        # Neither sweep needs a runtime: both address records by what the record says, which
        # is what lets them outlive whichever runtime was holding the work.
        self.ctx.task_queue.reclaim_stale_leases()
        # Last of the three: a record the reclaim just closed is one whose question this pass
        # then closes, rather than leaving it open for a tick.
        self.ctx.task_queue.reconcile_parked_tasks(self.ctx.tool_confirmation_reader)
        # A drain that stopped at a bound left reports behind, so this tick must come back
        # for them rather than pause on an empty inbox first: the poll's timeout is what
        # paces an *idle* loop, and a loop with work waiting is not idle. Without this the
        # drain's bound would double as a throughput cap of one drain per poll timeout.
        return self.process_inbox(timeout=0.0 if drain.saturated else _INBOX_POLL_TIMEOUT_SEC)

    def _publish_terminal_jobs(self) -> None:
        if self.stop_event.is_set() or (self._publication is not None and not self._publication.done()):
            return
        deadline = time.monotonic() + _TERMINAL_TICK_BUDGET_SEC
        self._publication_deadline = deadline
        if self._publication_pool is None:
            self._publication_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="terminal-publication")
        self._publication = self._publication_pool.submit(
            contextvars.copy_context().run, self._publish_terminal_batch, deadline
        )
        self._publication.add_done_callback(lambda _future: self._publication_wakeup.set())
        while not self._publication.done():
            remaining = self._publication_deadline - time.monotonic()
            if remaining <= 0:
                log.warning("Terminal publication tick or lease budget exhausted")
                return
            self._publication_wakeup.wait(remaining)
            self._publication_wakeup.clear()
        self._publication.result()

    def _publish_terminal_batch(self, deadline: float) -> None:
        token = store_deadline.set(deadline)
        try:
            recovery_deadline = min(deadline, time.monotonic() + 1)
            store_deadline.set(recovery_deadline)
            try:
                self._recover_message_handoffs(recovery_deadline)
            except Exception:
                log.warning("Message handoff scan failed", exc_info=True)
            finally:
                store_deadline.set(deadline)
            records = self.ctx.result_repo.claim_terminal_jobs(limit=TERMINAL_CLAIM_MAX_BATCH)
            retention_days = int(load_settings().OUROBOROS_EPISODE_RETENTION_DAYS or 90)
            for record in records:
                if self.stop_event.is_set() or time.monotonic() >= deadline:
                    break
                self._publish_terminal_job(record)
                self._publish_terminal_memory(record, retention_days=retention_days)
            for record in self.ctx.tasks_repo.list_terminal_memory(limit=TERMINAL_CLAIM_MAX_BATCH - len(records)):
                if self.stop_event.is_set() or time.monotonic() >= deadline:
                    break
                self._publish_terminal_memory(record, retention_days=retention_days)
        except Exception:
            log.warning("Terminal reconciliation failed", exc_info=True)
        finally:
            store_deadline.reset(token)

    def _recover_message_handoffs(self, deadline: float) -> None:
        messages = self.ctx.conversations_repo.list_message_handoffs(after_id=self._handoff_cursor, limit=100)
        if not messages:
            self._handoff_cursor = ""

        def before_publish() -> None:
            if self.stop_event.is_set() or time.monotonic() >= deadline:
                raise TimeoutError("message recovery stopped")

        for message in messages:
            if self.stop_event.is_set() or time.monotonic() >= deadline:
                break
            self._handoff_cursor = message.message_id
            if message.actor_type == "owner":
                if message.metadata.get("legacy_image") is True:
                    try:
                        envelope = legacy_image_envelope(message)
                        result = self.ctx.orchestrator.submit(envelope)
                        self._send_submission_reply(envelope, result, persist_only=True)
                        if result.mode == SubmissionMode.QUEUE:
                            mark_chat_message_processed(self.ctx.drive_root, message.message_id)
                    except _LEGACY_RECOVERY_FAILURES:
                        log.warning("Legacy image recovery failed for %s", message.message_id, exc_info=True)
                continue
            if not message.metadata.get("handoff_scope") or message.metadata.get("handoff_accepted"):
                continue
            try:
                self.ctx.send_with_budget(
                    message.content,
                    scope=OwnerScope.from_dict(message.metadata["handoff_scope"]),
                    source=message.channel,
                    trace_id=str(message.metadata.get("trace_id") or ""),
                    transport=dict(message.metadata.get("transport") or {}),
                    task_id=str(message.metadata.get("task_id") or ""),
                    ts=message.created_at,
                    fmt=str(message.metadata.get("format") or ""),
                    client_message_id=message.source_message_id,
                    delivery_only=bool(message.metadata.get("delivery_only")),
                    before_publish=before_publish,
                )
            except Exception:
                log.warning("Message handoff recovery failed for %s", message.message_id, exc_info=True)

    def _publish_terminal_job(self, record: TaskRecord) -> None:
        trace_id = aef_tracing.ensure_trace_id(record.input.get("trace_id") or record.correlation_id)
        with aef_tracing.trace_scope(trace_id):
            claim = str(record.terminal_publish_claim or "")

            def before_publish() -> None:
                deadline = store_deadline.get()
                if self.stop_event.is_set() or deadline is None or time.monotonic() >= deadline:
                    raise TimeoutError("terminal publication stopped")
                started = time.monotonic()
                remaining = self.ctx.result_repo.terminal_publication_remaining(record.task_id, claim=claim)
                if remaining <= 0:
                    raise TimeoutError("terminal publication claim expired")
                self._publication_deadline = min(deadline, started + remaining)
                store_deadline.set(self._publication_deadline)
                self._publication_wakeup.set()

            try:
                before_publish()
                scope_data = record.input.get("scope") or record.stored_scope()
                if not scope_data:
                    raise ValueError("terminal job has no stored scope")
                scope = OwnerScope.from_dict(scope_data)
                if record.status == "completed":
                    text = record.result
                elif record.status == "rejected_duplicate":
                    text = "Не удалось запустить фоновую задачу: достигнут лимит активных задач."  # noqa: RUF001
                elif record.status == "cancelled":
                    text = f"🛑 Фоновая задача {record.job_id} остановлена."
                else:
                    text = (
                        f"Фоновая задача {record.job_id} завершилась с ошибкой. "  # noqa: RUF001
                        f"Код: {record.error_code or 'execution_failed'}."
                    )
                accepted = bool(
                    self.ctx.send_with_budget(
                        text,
                        scope=scope,
                        source=str(record.source),
                        trace_id=trace_id,
                        transport=dict(record.input.get("transport") or {}),
                        task_id=record.job_id,
                        ts=record.finished_at or None,
                        publication=record,
                        before_publish=before_publish,
                        client_message_id=f"durable-job-terminal:{record.owner_id}:{record.job_id}",
                    )
                )
                attachments_done = self.ctx.bridge.send_terminal_attachments(record, before_publish)
                before_publish()
                self.ctx.result_repo.finish_terminal_publication(
                    record.task_id, claim=claim, accepted=accepted and attachments_done
                )
            except Exception:
                log.warning("Terminal job publication failed for %s", record.job_id, exc_info=True)
                try:
                    self.ctx.result_repo.finish_terminal_publication(record.task_id, claim=claim, accepted=False)
                except Exception:
                    log.warning("Terminal job publication deferral failed for %s", record.job_id, exc_info=True)

    def _publish_terminal_memory(self, record: TaskRecord, *, retention_days: int) -> None:
        if record.status != "completed" or record.metadata.get("terminal_memory_done") or self.stop_event.is_set():
            return
        trace_id = aef_tracing.ensure_trace_id(record.input.get("trace_id") or record.correlation_id)
        with aef_tracing.trace_scope(trace_id):
            filed = False
            try:
                scope = OwnerScope.from_dict(record.input.get("scope") or record.stored_scope())
                trace = dict(record.metadata.get("llm_trace") or {})
                store_task_episode(
                    record.to_agent_input(),
                    record.result,
                    trace,
                    scope=scope,
                    search_terms=record.metadata.get("episode_search_terms") or [],
                    execution_memory_repository=self.ctx.execution_memory_repository,
                    retention_days=retention_days,
                    created_at=record.finished_at,
                )
                filed = True
                if not self.ctx.tasks_repo.finish_terminal_memory(record.task_id, filed=True):
                    if self.stop_event.is_set() or time.monotonic() >= (store_deadline.get() or 0):
                        return
                    memory_event_publisher_factory.memory_publisher.publish(
                        MemoryDistillationEvent(
                            scope=scope,
                            trace_id=trace_id,
                            llm_trace=trace,
                            response_text=record.result,
                            task=MemoryTaskRef(
                                job_id=record.job_id,
                                task_id=record.task_id,
                                task_type=record.task_type,
                                text=str(record.input.get("text") or record.input.get("description") or ""),
                                recorded_at=_timestamp(record.finished_at),
                            ),
                        )
                    )
            except Exception:
                log.warning("Terminal job memory handoff failed for %s", record.job_id, exc_info=True)
                try:
                    self.ctx.tasks_repo.finish_terminal_memory(record.task_id, filed=filed)
                except Exception:
                    log.warning("Terminal memory deferral failed for %s", record.job_id, exc_info=True)

    def run(self) -> None:
        """Tick until stopped, backing off after ordinary tick failures."""
        try:
            settings = load_settings()
            max_delay = settings.OUROBOROS_SUPERVISOR_RETRY_MAX_DELAY_SEC
            initial_delay = min(settings.OUROBOROS_SUPERVISOR_RETRY_INITIAL_DELAY_SEC, max_delay)
            retry_delay = initial_delay
            while not self.stop_event.is_set():
                # At the top rather than after the tick: a crashed tick and its backoff are the
                # loop coming round, a tick that never returns is not.
                self.heartbeat.beat()
                try:
                    self.run_tick()
                    retry_delay = initial_delay
                except Exception as exc:
                    log.error("Supervisor tick failed; retrying in %gs: %s", retry_delay, exc, exc_info=True)
                    self.stop_event.wait(retry_delay)
                    retry_delay = min(max_delay, retry_delay * 2)
        finally:
            if self._publication_pool is not None:
                self._publication_pool.shutdown(wait=False, cancel_futures=True)
                if self._publication is not None and wait([self._publication], timeout=TICK_STOP_BUDGET_SEC).not_done:
                    raise SupervisorShutdownError("Terminal publication did not stop; shared clients retained")


def bootstrap_supervisor_repo(settings: dict, git_ops_module=None) -> tuple[bool, str]:
    del settings, git_ops_module
    return True, "OK: headless runtime has no managed git bootstrap"


def build_supervisor_context(
    *,
    settings: dict[str, Any],
    runtime: SupervisorRuntime,
    bridge: LocalChatBridge,
    request_restart: Callable[[], None],
    safe_restart: Callable[[], tuple[bool, str]],
    task_stop_publisher: ITaskStopPublisher,
) -> SupervisorContext:
    from supervisor.events import dispatch_event  # noqa: F401
    from supervisor.message_bus import send_with_budget
    from supervisor.queue import cancel_task_by_id, enqueue_task
    from supervisor.state import update_usage_from_llm

    set_runtime(runtime)

    return SupervisorContext(
        result_repo=runtime.result_repo,
        conversations_repo=runtime.conversations_repo,
        drive_root=runtime.drive_root,
        repo_dir=runtime.repo_dir,
        bridge=bridge,
        orchestrator=runtime.orchestrator,
        agent_events=runtime.agent_events,
        task_queue=runtime.task_queue,
        state_repo=runtime.state_repo,
        usage_repository=runtime.usage_repository,
        cleanup_checkpoint_repository=runtime.cleanup_checkpoint_repository,
        working_retrieval_repository=runtime.working_retrieval_repository,
        execution_memory_repository=runtime.execution_memory_repository,
        tasks_repo=runtime.tasks_repo,
        tool_confirmation_reader=runtime.tool_confirmation_reader,
        send_with_budget=send_with_budget,
        send_typing=bridge.send_chat_action,
        safe_restart=safe_restart,
        request_restart=request_restart,
        cancel_task_by_id=cancel_task_by_id,
        enqueue_task=enqueue_task,
        update_usage_from_llm=update_usage_from_llm,
        task_stop_publisher=task_stop_publisher,
        soft_timeout=runtime.soft_timeout,
        hard_timeout=runtime.hard_timeout,
    )


def run_supervisor(
    settings: dict[str, Any],
    *,
    drive_root: pathlib.Path,
    repo_dir: pathlib.Path,
    stop_event: threading.Event,
    ready_event: threading.Event,
    on_error: Callable[[str], None],
    heartbeat: LoopHeartbeat,
    request_restart: Callable[[], None],
    safe_restart: Callable[[], tuple[bool, str]],
    apply_settings_to_env: Callable[[dict[str, Any]], None] | None = None,
    gateway: IMessageGateway,
    bridge: LocalChatBridge,
    agent_events: IAgentEventConsumer,
    work_signal: IWorkSignal,
    task_stop_publisher: ITaskStopPublisher,
) -> None:
    log.info(
        "Supervisor startup begin drive_root=%s repo_dir=%s",
        drive_root,
        repo_dir,
    )
    if apply_settings_to_env is not None:
        apply_settings_to_env(settings)
        log.info("Supervisor applied settings to process environment")

    attachment_cleanup_thread: threading.Thread | None = None
    schedule_dispatch_thread: threading.Thread | None = None
    try:
        ok, msg = bootstrap_supervisor_repo(settings)
        if not ok:
            log.error("Supervisor bootstrap failed: %s", msg)
        else:
            log.info("Supervisor repo bootstrap complete: %s", msg)

        from supervisor.bootstrap import build_supervisor_runtime

        runtime = build_supervisor_runtime(
            settings=settings,
            drive_root=drive_root,
            repo_dir=repo_dir,
            bridge=bridge,
            gateway=gateway,
            agent_events=agent_events,
            work_signal=work_signal,
        )
        log.info("Supervisor runtime components wired")

        ctx = build_supervisor_context(
            settings=settings,
            runtime=runtime,
            bridge=bridge,
            request_restart=request_restart,
            safe_restart=safe_restart,
            task_stop_publisher=task_stop_publisher,
        )
        log.info("Supervisor context built")
        attachment_cleanup_thread = _build_worker_thread(
            partial(cleanup_expired_attachments, get_storage_provider(), cancellation=_CleanupCancellation(stop_event)),
            stop_event,
            name="attachment-cleanup",
            label="Attachment cleanup",
            trace_source=_TRACE_SOURCE,
            interval=_ATTACHMENT_CLEANUP_INTERVAL_SECONDS,
        )
        attachment_cleanup_thread.start()
        scheduler_settings = load_settings()
        schedule_dispatch_thread = _build_worker_thread(
            _build_schedule_dispatcher(
                ctx,
                gateway,
                batch_limit=scheduler_settings.OUROBOROS_SCHEDULER_DUE_BATCH_LIMIT,
                # A detached root is refused outright while the subsystem is off, and a schedule
                # has to fire either way; where it is on, this is what keeps the owner's chat free.
                detached_tasks=scheduler_settings.OUROBOROS_DURABLE_JOBS_ENABLED,
                should_stop=stop_event.is_set,
            ).run_pass,
            stop_event,
            name="schedule-dispatch",
            label="Schedule dispatch",
            trace_source=_SCHEDULE_TRACE_SOURCE,
            interval=scheduler_settings.OUROBOROS_SCHEDULER_POLL_INTERVAL_SEC,
            # De-syncs replicas' passes; correctness never depends on it — the slot claim does that.
            jitter=scheduler_settings.OUROBOROS_SCHEDULER_POLL_JITTER_SEC,
        )
        schedule_dispatch_thread.start()

        ready_event.set()
        log.info("Supervisor ready.")
        SupervisorLoop(
            ctx=ctx,
            stop_event=stop_event,
            inbox=runtime.gateway.inbox,
            heartbeat=heartbeat,
        ).run()
    except SupervisorShutdownError:
        raise
    except Exception as exc:
        on_error(f"Supervisor init failed: {exc}")
        log.critical("Supervisor initialization failed", exc_info=True)
        ready_event.set()
    finally:
        # Returning is the supervisor's death, whatever the path: the stop event
        # is what tells the composition root to stop the transports, which would
        # otherwise keep accepting messages nobody drains.
        stop_event.set()
        cleanup_stopped = _stop_worker_thread(
            attachment_cleanup_thread,
            stop_event,
            label="Attachment cleanup",
            timeout=_ATTACHMENT_CLEANUP_JOIN_SECONDS,
        )
        schedules_stopped = _stop_worker_thread(
            schedule_dispatch_thread,
            stop_event,
            label="Schedule dispatch",
            timeout=_SCHEDULE_DISPATCH_JOIN_SECONDS,
        )
        if not (cleanup_stopped and schedules_stopped):
            raise SupervisorShutdownError("Supervisor workers did not stop; shared clients retained")


__all__ = [
    "LOOP_STALL_SEC",
    "TICK_STOP_BUDGET_SEC",
    "AgentEventDrain",
    "SupervisorContext",
    "SupervisorLoop",
    "bootstrap_supervisor_repo",
    "build_supervisor_context",
    "is_owner_capable_source",
    "run_supervisor",
]
