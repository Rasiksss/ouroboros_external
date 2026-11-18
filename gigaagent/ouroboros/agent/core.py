"""Thin agent orchestrator around context, LLM loop, tools, memory, and review."""

from __future__ import annotations

import contextvars
import logging
import os
import pathlib
import queue
import shutil
import tempfile
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from new_core.loop.state import RunState
from ouroboros.observability.interfaces.audit_events import IAuditEventRepository

log = logging.getLogger(__name__)


from new_core.identity import OwnerScope
from new_core.loop import CancellationSignal, build_task_graph, max_steps_for
from new_core.skills.catalog import SkillCatalogSnapshot
from new_core.skills.interfaces import IExtensionRuntime
from new_core.skills.surface import SkillAvailability, SkillSurface
from new_core.tools.context import ToolContext
from new_core.tools.interfaces import ToolOutbound
from ouroboros.agent.context import build_llm_messages
from ouroboros.agent.interfaces.owner_session import IOwnerSessionRepository
from ouroboros.agent.interfaces.startup_usage import IStartupUsageRepository
from ouroboros.agent.startup_checks import (
    inject_crash_report,
    verify_system_state,
)
from ouroboros.agent.task_graph import build_task_run
from ouroboros.agent.task_pipeline import (
    build_review_context,
)
from ouroboros.agent_events.interfaces import (
    AgentEvent,
    IAgentEventPublisher,
    OutboundText,
    TaskHeartbeat,
    TypingStopped,
)
from ouroboros.contracts.attachments import ATTACHMENTS_ADAPTER
from ouroboros.llm import LLMClient
from ouroboros.observability import aef_tracing
from ouroboros.observability.event_record import emit_worker_log_event, record_event
from ouroboros.storage.core_memory.memory.domain.models import RecalledMemory
from ouroboros.storage.execution_memory.interfaces import IExecutionMemoryRepository
from ouroboros.storage.runtime_backend import STORE_FAILURES, ExecutionClaimUnavailable
from ouroboros.storage.runtime_models import PARKED_WORKSPACE_KEY
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.task_results import (
    HEARTBEAT_PERIOD_SEC,
    STATUS_FAILED,
    STATUS_RUNNING,
    TERMINAL_STATUSES,
    execution_claim,
    load_task_result,
    write_task_result,
)
from ouroboros.storage.tool_confirmation.interfaces import IToolConfirmationRepository
from ouroboros.utils import (
    safe_relpath,
    truncate_for_log,
    utc_now_iso,
)

_worker_boot_logged = False
_worker_boot_lock = threading.Lock()

_SLOW_STATUS_TEXT = "Думаю дольше обычного, продолжаю работать."
_SLOW_STATUS_REPEAT_TEXT = "Всё ещё работаю."
_SLOW_STATUS_REPEAT_SEC = 120  # floor on the gap between two of those lines
_HEARTBEAT_JOIN_TIMEOUT_SEC = 5


def _aef_agent_start_input(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": str(task.get("id") or ""),
        "task_type": str(task.get("type") or ""),
        "chat_id": task.get("chat_id"),
        "text": str(task.get("text") or ""),
    }


def _aef_agent_start_output(task: dict[str, Any], status: str, output: str) -> dict[str, Any]:
    return {
        "task_id": str(task.get("id") or ""),
        "task_type": str(task.get("type") or ""),
        "status": status,
        "output": output,
    }


def _task_scope(task: dict[str, Any]) -> OwnerScope:
    """The task's identity, parsed once. A task without one is a producer's bug."""
    try:
        return OwnerScope.from_dict(task.get("scope"))
    except ValueError as exc:
        raise ValueError(f"task {str(task.get('id') or '')!r} carries no usable scope: {exc}") from exc


def _parked_workspace(task: dict[str, Any], tmp_root: pathlib.Path) -> pathlib.Path | None:
    """The workspace a parked run left behind, when this instance still holds it."""
    stored = str(task.get(PARKED_WORKSPACE_KEY) or "").strip()
    if not stored:
        return None
    candidate = pathlib.Path(stored)
    if not candidate.is_dir():
        log.warning("The parked run's workspace is not on this instance; resuming with an empty one")
        return None
    if not candidate.resolve().is_relative_to(tmp_root.resolve()):
        log.warning("Ignoring a parked workspace outside the task tmp root")
        return None
    return candidate


def _runtime_session_id(scope: OwnerScope, repository: IOwnerSessionRepository) -> str:
    """Read the owner's AEF session without letting a tracing failure abort the run."""
    try:
        return repository.get_session_id(scope)
    except Exception:
        log.debug("Failed to read the session id for %s", scope.owner_id, exc_info=True)
        return ""


def _add_aef_agent_output(controller: Any, output: Any) -> None:
    try:
        controller.add_output_result(output=output)
    except Exception:
        log.debug("Failed to add AEF agent_start output", exc_info=True)


@dataclass(frozen=True)
class Env:
    repo_dir: pathlib.Path
    drive_root: pathlib.Path

    def repo_path(self, rel: str) -> pathlib.Path:
        return (self.repo_dir / safe_relpath(rel)).resolve()

    def drive_path(self, rel: str) -> pathlib.Path:
        return (self.drive_root / safe_relpath(rel)).resolve()


class OuroborosAgent:
    """Per-worker agent instance; long-term state lives on Drive."""

    def __init__(
        self,
        env: Env,
        *,
        skill_snapshot: SkillCatalogSnapshot,
        skill_availability: SkillAvailability,
        extension_runtime: IExtensionRuntime,
        session_repository: IOwnerSessionRepository,
        startup_usage_repository: IStartupUsageRepository,
        execution_memory_repository: IExecutionMemoryRepository,
        audit_event_repository: IAuditEventRepository,
        tool_confirmation_reader: IToolConfirmationRepository,
        events: IAgentEventPublisher | None = None,
        outbound: ToolOutbound | None = None,
    ):
        self.env = env
        self.skill_snapshot = skill_snapshot
        self.skill_availability = skill_availability
        self.extension_runtime = extension_runtime
        self.session_repository = session_repository
        self.startup_usage_repository = startup_usage_repository
        self.execution_memory_repository = execution_memory_repository
        self.audit_event_repository = audit_event_repository
        self.tool_confirmation_reader = tool_confirmation_reader
        self._pending_events: list[AgentEvent] = []
        #: What this run reports outward. One direction only: it cannot be read back.
        self._events: IAgentEventPublisher | None = events
        self._current_task_type: str | None = None
        self._current_task_id: str | None = None
        #: The running task's ``RunState``; the one place identity is read from.
        self._run_state: Any = None
        # The running task's usage totals, so model calls a *tool* makes outside the loop
        # (vlm_query) are counted against the task like the loop's own. Rebound to the
        # run's `RunState.accumulated_usage` by `build_task_run`; the standalone dict here
        # keeps an agent that never entered the graph from writing into nothing.
        self._task_usage: dict[str, Any] = {}
        #: The one outbound boundary this run hands its attachments to, given by whoever
        #: composed the run. ``None`` when nothing composed one.
        self._outbound = outbound

        self._incoming_messages: queue.Queue = queue.Queue()
        self._last_progress_ts: float = 0.0
        self._task_started_ts: float = 0.0

        self.llm = LLMClient()

        self._task_graph = build_task_graph()

        self._log_worker_boot_once()

    def inject_message(
        self,
        text: str,
        image_data: tuple[str, str] | tuple[str, str, str] | None = None,
    ) -> None:
        """Thread-safe: inject a user message into the active conversation."""
        if image_data:
            payload: dict[str, Any] = {
                "text": text,
                "image_base64": image_data[0],
                "image_mime": image_data[1],
            }
            if len(image_data) > 2 and image_data[2]:
                payload["image_caption"] = image_data[2]
            self._incoming_messages.put(payload)
            return
        self._incoming_messages.put(text)

    def _emit_live_log(self, scope: OwnerScope, event_type: str, **fields: Any) -> None:
        """Report one session-only live log line for the live UI."""
        emit_worker_log_event(
            self._events,
            {"type": event_type, "ts": utc_now_iso(), **fields},
            scope=scope,
            log_label="agent live",
        )

    def _log_worker_boot_once(self) -> None:
        """The process's startup checks, run once and finished before a second agent exists.

        The whole block is inside the lock, not just the flag: agents are built per run, so
        the flag alone would let a second thread start a task while the first was still
        verifying the system state it is about to use.
        """
        global _worker_boot_logged
        try:
            with _worker_boot_lock:
                if _worker_boot_logged:
                    return
                _worker_boot_logged = True
                record_event(
                    "events",
                    "worker_boot",
                    {
                        "ts": utc_now_iso(),
                        "pid": os.getpid(),
                    },
                    audit_event_repository=self.audit_event_repository,
                )
                verify_system_state(
                    self.env,
                    startup_usage_repository=self.startup_usage_repository,
                    audit_event_repository=self.audit_event_repository,
                )
                inject_crash_report(self.env, audit_event_repository=self.audit_event_repository)
        except Exception:
            log.warning("Worker boot logging failed", exc_info=True)
            return

    def _build_tool_context(
        self,
        task: dict[str, Any],
        task_workspace: pathlib.Path | None,
        *,
        root_task_id: str,
        scope: OwnerScope,
        skill_surface: SkillSurface,
    ) -> ToolContext:
        """Assemble the task's ToolContext from the task and the agent's session state.

        Pure assembly: no IO, no events emitted, and no registry install (``prepare_tools``
        owns that). ``root_task_id`` is resolved by the caller because the running
        status write needs the same value.

        The events channel is *built* here rather than assigned onto the context later: the
        queue, the drive root, and the task identity it binds are all already in hand, and a
        context that leaves this method is complete.
        """
        from ouroboros.tools.events import PublishedToolEvents

        task_id = str(task.get("id") or "")
        raw_attachments = task.get("attachments") or []
        attachment_values = [item for item in raw_attachments if isinstance(item, dict)]
        attachments = ATTACHMENTS_ADAPTER.validate_python(attachment_values)
        return ToolContext(
            repo_dir=self.env.repo_dir,
            drive_root=self.env.drive_root,
            scope=scope,
            skill_surface=skill_surface,
            extension_runtime=self.extension_runtime,
            pending_events=self._pending_events,
            current_task_type=self._current_task_type,
            transport=dict(task.get("transport") or {}),
            outbound=self._outbound,
            emit_progress_fn=self._progress_emitter(scope, task.get("transport") or {}, task_id),
            events=PublishedToolEvents(
                self._events,
                task_id=task_id,
                task_type=self._current_task_type or "task",
                task_usage=self._task_usage,
                scope=scope,
                audit_event_repository=self.audit_event_repository,
            ),
            task_id=task_id or None,
            job_id=str(task.get("job_id") or task_id),
            detached=bool(task.get("detached")),
            execution_claim=str(task.get("execution_claim") or ""),
            inherited_context_seed=str(task.get("inherited_context_seed") or ""),
            committed_summary_snapshot=str(task.get("committed_summary_snapshot") or ""),
            through_message_id=str(task.get("through_message_id") or ""),
            root_task_id=root_task_id or None,
            trace_id=str(task.get("trace_id") or ""),
            task_depth=int(task.get("depth", 0)),
            task_workspace=task_workspace,
            attachments=attachments,
        )

    def _assemble_opening_transcript(
        self,
        task: dict[str, Any],
        ctx: ToolContext,
        scope: OwnerScope,
        skill_surface: SkillSurface,
        *,
        cancellation: CancellationSignal,
    ) -> list[dict[str, Any]]:
        """Build a fresh run's opening transcript: the prompt soft cap, memory recall and the prompt build."""
        task_id = str(task.get("id") or "")
        self._emit_live_log(
            scope,
            "context_building_started",
            task_id=task_id,
            task_type=str(task.get("type") or ""),
        )

        from ouroboros.storage.settings.store import load_settings

        settings = load_settings()
        _use_local = str(settings.USE_LOCAL_MAIN or "").lower() in ("true", "1")
        # Reserve 55% of the model context window for the system prompt;
        # the remaining 45% covers tool history growth during the task.
        from ouroboros.llm.provider_models import get_model_context_tokens

        _ctx_tokens = get_model_context_tokens()
        _soft_cap = int(settings.OUROBOROS_SYSTEM_PROMPT_TOKENS or str(int(_ctx_tokens * 0.55)))
        if _use_local:
            _local_ctx = int(settings.LOCAL_MODEL_CONTEXT_LENGTH or 0)
            if _local_ctx <= 0:
                _local_ctx = 16384
            _soft_cap = max(2048, _local_ctx // 2)

        # One header listing for the whole build: recall selects from it and both
        # memory blocks are rendered from it. Reading it per section was three
        # scans of the same table for one prompt.
        memory_headers = None
        try:
            from ouroboros.storage.core_memory.memory import get_memory

            memory_headers = get_memory().list_prompt_headers(scope)
        except Exception:
            log.debug("Failed to list memory headers, sections will read their own", exc_info=True)

        recalled_memories: list[RecalledMemory] = []
        try:
            from ouroboros.agent.memory_recall import recall_relevant_memories
            from ouroboros.storage.core_memory.memory import get_memory

            recalled_memories = recall_relevant_memories(
                query=str(task.get("text", "")),
                store=get_memory(),
                scope=scope,
                llm=self.llm,
                events=self._events,
                task_id=task_id or "",
                headers=memory_headers,
                cancellation=cancellation,
            )
        except Exception:
            log.debug("Memory recall failed, continuing without recalled memories", exc_info=True)

        # The soft cap's ``info`` is dropped: its only observable effect is a warning
        # logged inside `context.py`, and nothing downstream ever read the value.
        messages, _cap_info = build_llm_messages(
            execution_memory_repository=self.execution_memory_repository,
            env=self.env,
            task=task,
            scope=scope,
            skill_surface=skill_surface,
            review_context_builder=lambda: build_review_context(self.env),
            soft_cap_tokens=_soft_cap,
            recalled_memories=recalled_memories or None,
            memory_headers=memory_headers,
            tool_ctx=ctx,
        )

        self._emit_live_log(
            scope,
            "context_building_finished",
            task_id=task_id,
            task_type=str(task.get("type") or ""),
            message_count=len(messages),
        )
        return messages

    def handle_task(
        self,
        task: dict[str, Any],
        *,
        cancellation: CancellationSignal,
    ) -> list[AgentEvent]:
        """Run one task end to end; ``cancellation`` is the caller's stop signal for it."""
        # One scope around the whole task, so the identifier is released on every exit: the
        # return, the crash path, and a failure in the setup that runs before the agent span.
        # This thread is long-lived and picks up the next task after this one, so an identifier
        # left established labels that gap — and everything logged or sent inside it — with a
        # task that has already ended. Resolved here rather than passed to `trace_scope` bare:
        # its own fallback is the ambient identifier, and a task runs under its own or a fresh
        # one, never under whatever the thread happened to be carrying.
        with aef_tracing.trace_scope(aef_tracing.ensure_trace_id(task.get("trace_id"))):
            task["trace_id"] = aef_tracing.current_trace_id()
            return self._run_task(task, cancellation=cancellation)

    def _run_task(
        self,
        task: dict[str, Any],
        *,
        cancellation: CancellationSignal,
    ) -> list[AgentEvent]:
        """`handle_task`'s body, run inside the task's trace scope."""
        start_time = time.time()
        self._task_started_ts = start_time
        self._last_progress_ts = time.monotonic()
        self._pending_events = []
        scope = _task_scope(task)
        self._current_task_type = str(task.get("type") or "")
        self._current_task_id = str(task.get("id") or "") or None
        self._emit_live_log(
            scope,
            "task_started",
            task_id=self._current_task_id or "",
            task_type=self._current_task_type,
            task_text=str(task.get("text") or "")[:200],
        )

        drive_logs = self.env.drive_path("logs")

        _tmp_root = self.env.drive_root / "tmp"
        _task_workspace = _parked_workspace(task, _tmp_root)
        if _task_workspace is None:
            try:
                _tmp_root.mkdir(parents=True, exist_ok=True)
                _task_workspace = pathlib.Path(tempfile.mkdtemp(prefix="ouroboros-task-", dir=_tmp_root))
            except Exception:
                log.debug("Failed to create task workspace", exc_info=True)

        text = ""
        span_status = "error"
        heartbeat: tuple[threading.Event, threading.Thread] | None = None
        # Bound before the try so the finally's teardown works even if build_task_run
        # raises: without it, cleanup would trip on an unbound name and mask the cause.

        state: RunState[ToolContext] | None = None
        claim_context = execution_claim.set(
            (str(task.get("id") or ""), str(task.get("execution_claim") or ""), cancellation.is_stopped)
            if task.get("detached")
            else None
        )
        try:
            aef_tracing.set_session_id(_runtime_session_id(scope, self.session_repository))
            with aef_tracing.agent_start(
                input=aef_tracing.mask_trace_payload(_aef_agent_start_input(task))
            ) as agent_span:
                # Per-task context/state, built up front (DA8): the loop adapters
                # resolve the ToolContext lazily, so the whole GraphContext is
                # ready before the prepare_context node sets that context. Built
                # before the heartbeat starts, so every lifecycle path below has a
                # run state to read its identity from.
                graph_ctx, state = build_task_run(
                    self,
                    task,
                    scope=scope,
                    task_workspace=_task_workspace,
                    start_time=start_time,
                    drive_logs=drive_logs,
                    cancellation=cancellation,
                )
                self._run_state = state
                heartbeat = self._start_task_heartbeat_loop(
                    str(task.get("id") or ""),
                    scope,
                    task.get("transport") or {},
                    detached=bool(task.get("detached")),
                    claim_token=str(task.get("execution_claim") or ""),
                )
                try:
                    # The whole lifecycle is the graph: open_transcript assembles
                    # the transcript, the loop runs, and the emit node publishes
                    # the result (empty-text fallback + emit_task_results) through
                    # emit_result. handle_task only invokes and reads back events.
                    try:
                        self._task_graph.invoke(state, graph_ctx, max_steps=max_steps_for(graph_ctx.config.max_rounds))
                        span_status = "ok"
                        text = state.final_content or ""
                    except Exception as e:
                        # A node raised (the engine has no finally/compensation, so
                        # it propagates out of invoke): record it. If the terminal
                        # emit node had already begun publishing, the failure is in
                        # publication itself — don't overwrite the result with FAILED
                        # or emit a second time (that would double the user's reply).
                        span_status = "failed"
                        tb = traceback.format_exc()
                        record_event(
                            "events",
                            "task_error",
                            {
                                "ts": utc_now_iso(),
                                "task_id": task.get("id"),
                                "error": repr(e),
                                "traceback": truncate_for_log(tb, 2000),
                            },
                            audit_event_repository=self.audit_event_repository,
                        )
                        if getattr(graph_ctx.events, "result_emitted", False):
                            text = state.final_content or ""
                        else:
                            text = f"⚠️ Error during processing: {type(e).__name__}: {e}"
                            if not cancellation.is_stopped():
                                try:
                                    existing_task = (
                                        load_task_result(self.env.drive_root, str(task.get("id") or "")) or {}
                                    )
                                    failed_parent_id = task.get("parent_task_id") or existing_task.get("parent_task_id")
                                    failed_root_id = (
                                        task.get("root_task_id")
                                        or existing_task.get("root_task_id")
                                        or failed_parent_id
                                        or task.get("id")
                                    )
                                    failed_chat_id = state.scope.chat_id
                                    # An already terminal record was closed by someone else, whose
                                    # reason stands. Failing it here is this run's own doing, so the
                                    # error text below is not a late answer to a stop.
                                    if str(existing_task.get("status") or "") not in TERMINAL_STATUSES:
                                        write_task_result(
                                            self.env.drive_root,
                                            str(task.get("id") or ""),
                                            STATUS_FAILED,
                                            scope=state.scope,
                                            parent_task_id=failed_parent_id,
                                            root_task_id=failed_root_id,
                                            chat_id=failed_chat_id,
                                            description=task.get("description"),
                                            context=task.get("context"),
                                            trace_id=str(task.get("trace_id") or existing_task.get("trace_id") or ""),
                                            result=text,
                                            execution_claim=str(task.get("execution_claim") or ""),
                                        )
                                        graph_ctx.events.self_failed = True
                                except STORE_FAILURES as exc:
                                    if task.get("detached"):
                                        raise ExecutionClaimUnavailable(str(task.get("id") or "")) from exc
                                except Exception:
                                    pass
                            graph_ctx.events.emit_result(
                                text=text,
                                usage=state.accumulated_usage,
                                trace=state.llm_trace,
                                tool_ctx=state.tool_ctx,
                            )
                    return list(self._pending_events)
                finally:
                    _add_aef_agent_output(
                        agent_span,
                        aef_tracing.mask_trace_payload(_aef_agent_start_output(task, span_status, str(text or ""))),
                    )

        finally:
            execution_claim.reset(claim_context)
            if heartbeat is not None:
                # Stop first: the teardown below can take seconds, and a tick
                # landing in it would reassure about an already-answered task.
                heartbeat[0].set()
            # Loop teardown that used to live in run_llm_loop's finally: with the
            # task graph invoked directly, handle_task owns it so it still runs on
            # every exit path (normal, round limit, exhausted recovery, crash).
            self._cleanup_task_loop_state(
                str(task.get("id") or ""),
                getattr(state, "scope", None) or scope,
                getattr(state, "tool_ctx", None),
            )
            if not task.get("detached"):
                self._emit_typing_stop(getattr(state, "scope", None) or scope)
            if _task_workspace is not None and (state is None or not state.suspended):
                # A parked run keeps its workspace: the call the owner is being asked about may
                # act on a file written there, and the answer arrives in a new run.
                shutil.rmtree(_task_workspace, ignore_errors=True)
            if heartbeat is not None:
                # Join so an in-flight tick sees this task's fields, not the next task's.
                heartbeat_thread = heartbeat[1]
                heartbeat_thread.join(timeout=_HEARTBEAT_JOIN_TIMEOUT_SEC)
                if heartbeat_thread.is_alive():
                    log.warning("Task heartbeat thread outlived its join window (task_id=%s)", task.get("id"))
            self._current_task_type = None
            self._current_task_id = None
            self._run_state = None

    def _cleanup_task_loop_state(
        self,
        task_id: str,
        owner_scope: OwnerScope,
        tool_ctx: ToolContext | None = None,
    ) -> None:
        """Browser/mailbox/tmp-artifact teardown (mirrors run_llm_loop's finally).

        With handle_task invoking the task graph directly instead of calling
        run_llm_loop, this scaffolding preserves those side effects on every task
        exit path. ``tool_ctx`` is this task's context, passed in rather than fetched
        off a shared registry.
        """
        if tool_ctx is not None:
            try:
                from ouroboros.tools.browser import cleanup_browser

                cleanup_browser(tool_ctx)
            except Exception:
                log.debug("Failed to cleanup browser state", exc_info=True)
        drive_root = self.env.drive_root
        if drive_root is not None and task_id:
            try:
                from ouroboros.agent.owner_inject import cleanup_task_mailbox

                cleanup_task_mailbox(owner_scope, drive_root, task_id)
            except Exception:
                log.debug("Failed to cleanup task mailbox", exc_info=True)
            try:
                from ouroboros.agent.tool_receipts import cleanup_tmp_tool_artifacts

                cleanup_tmp_tool_artifacts(drive_root, task_id=task_id)
            except Exception:
                log.debug("Failed to cleanup tmp tool artifacts", exc_info=True)

    def _progress_emitter(self, scope: OwnerScope, transport: dict[str, Any], task_id: str) -> Callable[[str], None]:
        """One task's progress callback, closed over that task's scope and transport hints.

        ``transport`` rides along beside the scope because it is delivery detail, not
        identity: it carries the dialogs ``target_uid`` and the message a reply anchors
        to, so a progress line lands in the same place as the answer that follows it.
        ``task_id`` is captured too: the heartbeat thread can call this past its own
        task's end, when the live fields already belong to the next task.
        """
        transport = dict(transport)

        def emit_progress(text: str) -> None:
            # Monotonic: a wall-clock step backwards would mute the slow-status notice.
            self._last_progress_ts = time.monotonic()
            if self._events is None:
                return
            try:
                self._events.publish(
                    OutboundText(
                        scope=scope,
                        text=f"💬 {text}",
                        fmt="markdown",
                        is_progress=True,
                        task_id=task_id,
                        source=str(scope.surface_id),
                        transport=dict(transport),
                        trace_id=aef_tracing.current_trace_id(),
                        ts=utc_now_iso(),
                    )
                )
            except Exception:
                log.warning("Failed to report progress", exc_info=True)

        return emit_progress

    def _emit_typing_stop(self, scope: OwnerScope) -> None:
        if self._events is None:
            return
        try:
            self._events.publish(
                TypingStopped(
                    scope=scope,
                    source=str(scope.surface_id),
                    trace_id=aef_tracing.current_trace_id(),
                    ts=utc_now_iso(),
                )
            )
        except Exception:
            log.warning("Failed to report the typing stop", exc_info=True)

    def _emit_task_heartbeat(self, scope: OwnerScope, task_id: str, phase: str, trace_id: str = "") -> None:
        """Forward the beat to live logs. Observability: losing one costs a log line."""
        if self._events is None:
            return
        try:
            self._events.publish(
                TaskHeartbeat(
                    scope=scope,
                    task_id=task_id,
                    task_type=self._current_task_type or "",
                    phase=phase,
                    trace_id=trace_id or aef_tracing.current_trace_id(),
                    ts=utc_now_iso(),
                )
            )
        except Exception:
            log.warning("Failed to report the task heartbeat", exc_info=True)

    def _beat(self, scope: OwnerScope, task_id: str, phase: str, trace_id: str, claim_token: str = "") -> None:
        """Renew this run's lease, then tell the live logs about it.

        The write comes first and never leaves this method: a transient database error
        used to cost a log line, and letting it out of the loop below would now cost four
        beats and a reclaim of a perfectly live run.
        """
        try:
            claim = execution_claim.get()
            if claim is not None and claim[2]():
                return
            beat = runtime_persistence.current.tasks_repo.heartbeat_task(task_id, phase=phase, claim_token=claim_token)
            if claim_token and beat is None:
                return
        except (Exception, ExecutionClaimUnavailable):
            log.warning("Failed to write the lease for task %s", task_id, exc_info=True)
            if claim_token:
                return
        self._emit_task_heartbeat(scope, task_id, phase, trace_id)

    def _still_running(self, task_id: str) -> bool:
        """Whether the supervisor still considers this task alive.

        Nothing can interrupt a running task thread, so a killed, cancelled or
        restarted task keeps working — and its heartbeat would keep reassuring
        the user about work the supervisor has already reported as over. The
        stored status is the one signal that covers all three.
        """
        try:
            record = load_task_result(self.env.drive_root, task_id) or {}
        except Exception:
            log.debug("Failed to read task status for the slow-progress notice", exc_info=True)
            return True
        return str(record.get("status") or STATUS_RUNNING) == STATUS_RUNNING

    def _slow_status_line(self, task_id: str, spoken: bool) -> str | None:
        """The line to say now, or ``None`` to stay quiet.

        The clock is ``_last_progress_ts`` — when the user last heard anything —
        so a tool's own progress postpones this instead of racing it.
        """
        from ouroboros.storage.settings.schema import get_defaults
        from ouroboros.storage.settings.store import load_settings

        settings = load_settings()
        if not settings.OUROBOROS_SLOW_LLM_STATUS_ENABLED:
            return None
        first_after = settings.OUROBOROS_SLOW_LLM_STATUS_SEC
        if first_after < 1:
            # 0 is both an operator's "off" and what a typo coerces to; neither
            # should become the chattiest setting.
            first_after = get_defaults().OUROBOROS_SLOW_LLM_STATUS_SEC
        wait_for = max(first_after, _SLOW_STATUS_REPEAT_SEC) if spoken else first_after
        if time.monotonic() - self._last_progress_ts < wait_for:
            return None
        if self._current_task_id != task_id or not self._still_running(task_id):
            return None
        return _SLOW_STATUS_REPEAT_TEXT if spoken else _SLOW_STATUS_TEXT

    def _start_task_heartbeat_loop(
        self,
        task_id: str,
        scope: OwnerScope,
        transport: dict[str, Any],
        *,
        detached: bool = False,
        claim_token: str = "",
    ) -> tuple[threading.Event, threading.Thread] | None:
        """Renew this run's lease, and break a long silence for the user.

        The lease is the reason the loop runs at all, so it starts whether or not there
        is anywhere to report to — a run whose beats stop is reclaimed by the sweep. The
        delivery target is captured once, here: reading the agent's live fields at emit
        time would race the next task, since this thread can be descheduled past its own
        checks.
        """
        if not task_id.strip():
            return None
        stop = threading.Event()
        trace_id = aef_tracing.current_trace_id()
        emit_progress = (lambda _text: None) if detached else self._progress_emitter(scope, transport, task_id)
        self._beat(scope, task_id, "start", trace_id, claim_token)

        def _loop() -> None:
            # Loop-local, so a leaked heartbeat cannot touch the next task's state.
            spoken = False
            while not stop.wait(HEARTBEAT_PERIOD_SEC):
                self._beat(scope, task_id, "running", trace_id, claim_token)
                try:
                    text = self._slow_status_line(task_id, spoken)
                    if text is None or stop.is_set():
                        continue
                    spoken = True
                    # This thread starts with an empty context; carry the trace over.
                    with aef_tracing.trace_scope(trace_id):
                        emit_progress(text)
                except Exception:
                    # A cosmetic notice must never take the heartbeat down.
                    log.debug("Failed to emit slow-progress notice", exc_info=True)

        thread = threading.Thread(target=contextvars.copy_context().run, args=(_loop,), daemon=True)
        thread.start()
        return stop, thread


def make_agent(
    repo_dir: str,
    drive_root: str,
    *,
    skill_snapshot: SkillCatalogSnapshot,
    skill_availability: SkillAvailability,
    extension_runtime: IExtensionRuntime,
    session_repository: IOwnerSessionRepository,
    startup_usage_repository: IStartupUsageRepository,
    execution_memory_repository: IExecutionMemoryRepository,
    audit_event_repository: IAuditEventRepository,
    tool_confirmation_reader: IToolConfirmationRepository,
    events: IAgentEventPublisher | None = None,
    outbound: ToolOutbound | None = None,
) -> OuroborosAgent:
    env = Env(repo_dir=pathlib.Path(repo_dir), drive_root=pathlib.Path(drive_root))
    return OuroborosAgent(
        env,
        skill_snapshot=skill_snapshot,
        skill_availability=skill_availability,
        extension_runtime=extension_runtime,
        session_repository=session_repository,
        startup_usage_repository=startup_usage_repository,
        execution_memory_repository=execution_memory_repository,
        tool_confirmation_reader=tool_confirmation_reader,
        events=events,
        outbound=outbound,
        audit_event_repository=audit_event_repository,
    )
