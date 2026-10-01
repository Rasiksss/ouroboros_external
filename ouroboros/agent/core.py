"""Thin agent orchestrator around context, LLM loop, tools, memory, and review."""

from __future__ import annotations

import logging
import os
import pathlib
import queue
import shutil
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

from new_core.loop import CancellationSignal, max_steps_for
from new_core.tools.context import ToolContext
from ouroboros.agent.context import build_llm_messages
from ouroboros.agent.startup_checks import (
    inject_crash_report,
    verify_system_state,
)
from ouroboros.agent.task_graph import build_task_graph, build_task_run
from ouroboros.agent.task_pipeline import (
    build_review_context,
)
from ouroboros.contracts.task_constraint import normalize_task_constraint
from ouroboros.llm import LLMClient
from ouroboros.observability import aef_tracing
from ouroboros.observability.event_record import emit_worker_log_event, record_event
from ouroboros.storage.task_results import STATUS_RUNNING, load_task_result, write_task_result
from ouroboros.utils import (
    safe_relpath,
    sanitize_task_for_event,
    truncate_for_log,
    utc_now_iso,
)

_worker_boot_logged = False
_worker_boot_lock = threading.Lock()


def _aef_agent_start_input(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": str(task.get("id") or ""),
        "task_type": str(task.get("type") or ""),
        "chat_id": task.get("chat_id"),
        "text": str(task.get("text") or ""),
        "direct_chat": bool(task.get("_is_direct_chat")),
    }


def _aef_agent_start_output(task: dict[str, Any], status: str, output: str) -> dict[str, Any]:
    return {
        "task_id": str(task.get("id") or ""),
        "task_type": str(task.get("type") or ""),
        "status": status,
        "output": output,
    }


def _aef_task_session_id(task: dict[str, Any]) -> str:
    return str(task.get("session_id") or task.get("conversation_id") or "").strip()


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

    def __init__(self, env: Env, event_queue: Any = None, media_senders: dict[str, Any] | None = None):
        self.env = env
        self._pending_events: list[dict[str, Any]] = []
        self._event_queue: Any = event_queue
        self._current_chat_id: int | str | None = None
        self._current_task_type: str | None = None
        self._current_task_source = ""
        self._current_task_id: str | None = None
        self._current_transport: dict[str, Any] = {}
        # The running task's usage totals, so model calls a *tool* makes outside the loop
        # (vlm_query) are counted against the task like the loop's own. Rebound to the
        # run's `RunState.accumulated_usage` by `build_task_run`; the standalone dict here
        # keeps an agent that never entered the graph from writing into nothing.
        self._task_usage: dict[str, Any] = {}
        self._media_senders = dict(media_senders or {})

        self._incoming_messages: queue.Queue = queue.Queue()
        self._busy = False
        self._last_progress_ts: float = 0.0
        self._task_started_ts: float = 0.0

        self.llm = LLMClient()

        # Build the whole-task graph once: context assembly, the LLM-with-tools
        # loop, and result emission as one static, stateless topology (DA2). Its
        # glue nodes close over this Agent; only invoke runs per task, with a
        # per-task GraphContext/RunState from build_task_run.
        self._task_graph = build_task_graph(self)

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

    def _emit_live_log(self, event_type: str, **fields: Any) -> None:
        """Send a session-only live log event to supervisor/UI."""
        emit_worker_log_event(
            self._event_queue,
            {"type": event_type, "ts": utc_now_iso(), **fields},
            blocking=True,
            log_label="agent live",
        )

    def _log_worker_boot_once(self) -> None:
        global _worker_boot_logged
        try:
            with _worker_boot_lock:
                if _worker_boot_logged:
                    return
                _worker_boot_logged = True
            record_event(
                self.env.drive_root,
                "events",
                "worker_boot",
                {
                    "ts": utc_now_iso(),
                    "pid": os.getpid(),
                },
            )
            verify_system_state(self.env)
            inject_crash_report(self.env)
        except Exception:
            log.warning("Worker boot logging failed", exc_info=True)
            return

    def _build_tool_context(
        self,
        task: dict[str, Any],
        task_workspace: pathlib.Path | None,
        *,
        root_task_id: str,
    ) -> ToolContext:
        """Assemble the task's ToolContext from the task and the agent's session state.

        Pure assembly: no IO, no events emitted, and no registry install (``prepare_tools``
        owns that). ``root_task_id`` is resolved by the caller because the running
        status write needs the same value.

        The events channel is *built* here rather than assigned onto the context later: the
        queue, the drive root, and the task identity it binds are all already in hand, and a
        context that leaves this method is complete.
        """
        from ouroboros.agent.memory_owner import resolve_memory_owner_id
        from ouroboros.tools.events import QueueToolEvents

        task_id = str(task.get("id") or "")
        return ToolContext(
            repo_dir=self.env.repo_dir,
            drive_root=self.env.drive_root,
            pending_events=self._pending_events,
            current_chat_id=self._current_chat_id,
            current_task_type=self._current_task_type,
            current_task_source=self._current_task_source,
            transport=dict(task.get("transport") or {}),
            media_senders=self._media_senders,
            emit_progress_fn=self._emit_progress,
            events=QueueToolEvents(
                self._event_queue,
                self.env.drive_root,
                task_id=task_id,
                task_type=self._current_task_type or "task",
                task_usage=self._task_usage,
            ),
            task_id=task_id or None,
            root_task_id=root_task_id or None,
            conversation_id=str(
                task.get("conversation_id")
                or (f"chat:{self._current_chat_id}" if self._current_chat_id is not None else "")
            ),
            trace_id=str(task.get("trace_id") or ""),
            task_depth=int(task.get("depth", 0)),
            is_direct_chat=bool(task.get("_is_direct_chat")),
            memory_owner_id=resolve_memory_owner_id(task),
            task_constraint=normalize_task_constraint(task.get("task_constraint")),
            task_workspace=task_workspace,
            attachments=[dict(value) for value in (task.get("attachments") or []) if isinstance(value, dict)],
        )

    def _prepare_task_context(
        self,
        task: dict[str, Any],
        task_workspace: pathlib.Path | None = None,
    ) -> tuple[ToolContext, list[dict[str, Any]], dict[str, Any]]:
        """Open the task, build its ToolContext and transcript: (ctx, messages, cap_info).

        Context assembly only: nothing here touches the tool registry. Installing
        the returned context and deriving the initial tool schemas from it is the
        ``prepare_tools`` node's job, so the two responsibilities stay separable.
        The ToolContext itself is assembled by ``_build_tool_context``; what is left
        here is the task-opening bookkeeping and the prompt build.
        """
        drive_logs = self.env.drive_path("logs")
        task_id = str(task.get("id") or "")
        try:
            existing_task = load_task_result(self.env.drive_root, task_id) if task_id else {}
        except Exception:
            log.debug("Failed to load existing task status for %s", task_id, exc_info=True)
            existing_task = {}
        parent_task_id = (
            str(task.get("parent_task_id") or (existing_task or {}).get("parent_task_id") or "").strip() or None
        )
        root_task_id = str(task.get("root_task_id") or (existing_task or {}).get("root_task_id") or "").strip()
        if not root_task_id:
            root_task_id = parent_task_id or task_id
        sanitized_task = sanitize_task_for_event(task, drive_logs)
        record_event(
            self.env.drive_root,
            "events",
            "task_received",
            {"ts": utc_now_iso(), "task": sanitized_task},
        )
        try:
            write_task_result(
                self.env.drive_root,
                task_id,
                STATUS_RUNNING,
                parent_task_id=parent_task_id,
                root_task_id=root_task_id or None,
                chat_id=self._current_chat_id,
                description=task.get("description"),
                context=task.get("context"),
                trace_id=str(task.get("trace_id") or ""),
                result="Task is running.",
            )
        except Exception:
            log.debug("Failed to persist running task status", exc_info=True)
        self._emit_live_log(
            "context_building_started",
            task_id=str(task.get("id") or ""),
            task_type=str(task.get("type") or ""),
        )

        ctx = self._build_tool_context(task, task_workspace, root_task_id=root_task_id)

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
                try:
                    from ouroboros.local_model import get_manager

                    _local_ctx = get_manager().get_context_length()
                except Exception:
                    _local_ctx = 0
            if _local_ctx <= 0:
                _local_ctx = 16384
            _soft_cap = max(2048, _local_ctx // 2)

        # The prompt renders memory for the same owner the tools read and write as.
        _owner_id = ctx.memory_owner_id

        # One header listing for the whole build: recall selects from it and both
        # memory blocks are rendered from it. Reading it per section was three
        # scans of the same table for one prompt.
        memory_headers = None
        try:
            from ouroboros.storage.core_memory.memory import get_memory

            memory_headers = get_memory().list_prompt_headers()
        except Exception:
            log.debug("Failed to list memory headers, sections will read their own", exc_info=True)

        recalled_memories: list[tuple[str, float, float]] = []
        try:
            from ouroboros.agent.memory_recall import recall_relevant_memories
            from ouroboros.storage.core_memory.memory import get_memory

            recalled_memories = recall_relevant_memories(
                query=str(task.get("text", "")),
                store=get_memory(),
                llm=self.llm,
                event_queue=self._event_queue,
                task_id=task_id or "",
                headers=memory_headers,
            )
        except Exception:
            log.debug("Memory recall failed, continuing without recalled memories", exc_info=True)

        messages, cap_info = build_llm_messages(
            env=self.env,
            task=task,
            review_context_builder=lambda: build_review_context(self.env),
            soft_cap_tokens=_soft_cap,
            recalled_memories=recalled_memories or None,
            owner_id=_owner_id,
            memory_headers=memory_headers,
        )

        self._emit_live_log(
            "context_building_finished",
            task_id=str(task.get("id") or ""),
            task_type=str(task.get("type") or ""),
            message_count=len(messages),
        )
        # The transcript is bound here, where it is built, rather than by a later node:
        # the list is bound once and only mutated in place afterwards, so the binding is
        # stable for the whole run. Its single reader is the safety gate in
        # `ouroboros/tools/dispatch.py`, which needs the conversation for its check.
        ctx.messages = messages
        return ctx, messages, cap_info

    def handle_task(
        self,
        task: dict[str, Any],
        *,
        cancellation: CancellationSignal,
    ) -> list[dict[str, Any]]:
        """Run one task end to end; ``cancellation`` is the caller's stop signal for it."""
        self._busy = True
        # Seeds the trace id for the events emitted below. The token is not kept:
        # the scope that owns reset is opened further down, around the agent span.
        aef_tracing.set_trace_id(task.get("trace_id"))
        task["trace_id"] = aef_tracing.current_trace_id()
        start_time = time.time()
        self._task_started_ts = start_time
        self._last_progress_ts = start_time
        self._pending_events = []
        # Preserve chat_id=0; it is a real session, not missing.
        _raw_chat = task.get("chat_id")
        if _raw_chat is None or _raw_chat == "":
            self._current_chat_id = None
        else:
            try:
                self._current_chat_id = int(_raw_chat)
            except (TypeError, ValueError):
                self._current_chat_id = str(_raw_chat)
        self._current_task_type = str(task.get("type") or "")
        self._current_task_source = str(task.get("source") or "")
        self._current_task_id = str(task.get("id") or "") or None
        self._current_transport = dict(task.get("transport") or {})
        self._emit_live_log(
            "task_started",
            task_id=self._current_task_id or "",
            task_type=self._current_task_type,
            task_text=str(task.get("text") or "")[:200],
            direct_chat=bool(task.get("_is_direct_chat")),
        )

        drive_logs = self.env.drive_path("logs")
        heartbeat_stop = self._start_task_heartbeat_loop(str(task.get("id") or ""))

        _tmp_root = self.env.drive_root / "tmp"
        _task_workspace: pathlib.Path | None = None
        try:
            _tmp_root.mkdir(parents=True, exist_ok=True)
            _task_workspace = pathlib.Path(tempfile.mkdtemp(prefix="ouroboros-task-", dir=_tmp_root))
        except Exception:
            log.debug("Failed to create task workspace", exc_info=True)

        text = ""
        span_status = "error"
        trace_token: Any | None = None
        # Bound before the try so the finally's teardown works even if build_task_run
        # raises: without it, cleanup would trip on an unbound name and mask the cause.
        state: Any = None
        try:
            aef_tracing.set_session_id(_aef_task_session_id(task))
            trace_id = aef_tracing.ensure_trace_id(task.get("trace_id"))
            task["trace_id"] = trace_id
            trace_token = aef_tracing.set_trace_id(trace_id)
            with aef_tracing.agent_start(
                input=aef_tracing.mask_trace_payload(_aef_agent_start_input(task))
            ) as agent_span:
                # Per-task context/state, built up front (DA8): the loop adapters
                # resolve the ToolContext lazily, so the whole GraphContext is
                # ready before the prepare_context node sets that context.
                graph_ctx, state = build_task_run(
                    self,
                    task,
                    task_workspace=_task_workspace,
                    start_time=start_time,
                    drive_logs=drive_logs,
                    cancellation=cancellation,
                )
                try:
                    # The whole lifecycle is the graph: prepare_context assembles
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
                            self.env.drive_root,
                            "events",
                            "task_error",
                            {
                                "ts": utc_now_iso(),
                                "task_id": task.get("id"),
                                "error": repr(e),
                                "traceback": truncate_for_log(tb, 2000),
                            },
                        )
                        if getattr(graph_ctx.events, "result_emitted", False):
                            text = state.final_content or ""
                        else:
                            text = f"⚠️ Error during processing: {type(e).__name__}: {e}"
                            try:
                                from ouroboros.storage.task_results import (
                                    STATUS_FAILED,
                                    TERMINAL_STATUSES,
                                    write_task_result,
                                )

                                existing_task = load_task_result(self.env.drive_root, str(task.get("id") or "")) or {}
                                failed_parent_id = task.get("parent_task_id") or existing_task.get("parent_task_id")
                                failed_root_id = (
                                    task.get("root_task_id")
                                    or existing_task.get("root_task_id")
                                    or failed_parent_id
                                    or task.get("id")
                                )
                                failed_chat_id = self._current_chat_id
                                if failed_chat_id in (None, ""):
                                    failed_chat_id = existing_task.get("chat_id")
                                # An already terminal record was closed by someone else, whose
                                # reason stands. Failing it here is this run's own doing, so the
                                # error text below is not a late answer to a stop.
                                if str(existing_task.get("status") or "") not in TERMINAL_STATUSES:
                                    write_task_result(
                                        self.env.drive_root,
                                        str(task.get("id") or ""),
                                        STATUS_FAILED,
                                        parent_task_id=failed_parent_id,
                                        root_task_id=failed_root_id,
                                        chat_id=failed_chat_id,
                                        description=task.get("description"),
                                        context=task.get("context"),
                                        trace_id=str(task.get("trace_id") or existing_task.get("trace_id") or ""),
                                        result=text,
                                    )
                                    graph_ctx.events.self_failed = True
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
            # Loop teardown that used to live in run_llm_loop's finally: with the
            # task graph invoked directly, handle_task owns it so it still runs on
            # every exit path (normal, round limit, exhausted recovery, crash).
            self._cleanup_task_loop_state(str(task.get("id") or ""), getattr(state, "tool_ctx", None))
            self._emit_typing_stop()
            if trace_token is not None:
                aef_tracing.reset_trace_id(trace_token)
            self._busy = False
            if _task_workspace is not None:
                shutil.rmtree(_task_workspace, ignore_errors=True)
            while not self._incoming_messages.empty():
                try:
                    self._incoming_messages.get_nowait()
                except queue.Empty:
                    break
            if heartbeat_stop is not None:
                heartbeat_stop.set()
            self._current_task_type = None
            self._current_task_source = ""
            self._current_task_id = None
            self._current_transport = {}

    def _cleanup_task_loop_state(self, task_id: str, tool_ctx: ToolContext | None = None) -> None:
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

                cleanup_task_mailbox(drive_root, task_id)
            except Exception:
                log.debug("Failed to cleanup task mailbox", exc_info=True)
            try:
                from ouroboros.agent.tool_receipts import cleanup_tmp_tool_artifacts

                cleanup_tmp_tool_artifacts(drive_root, task_id=task_id)
            except Exception:
                log.debug("Failed to cleanup tmp tool artifacts", exc_info=True)

    def _emit_progress(self, text: str) -> None:
        self._last_progress_ts = time.time()
        if self._event_queue is None or self._current_chat_id is None:
            return
        try:
            self._event_queue.put(
                {
                    "type": "send_message",
                    "chat_id": self._current_chat_id,
                    "text": f"💬 {text}",
                    "format": "markdown",
                    "is_progress": True,
                    "task_id": self._current_task_id or "",
                    "source": self._current_task_source,
                    "transport": dict(self._current_transport),
                    "trace_id": aef_tracing.current_trace_id(),
                    "ts": utc_now_iso(),
                }
            )
        except Exception:
            log.warning("Failed to emit progress event", exc_info=True)

    def _emit_typing_stop(self) -> None:
        if self._event_queue is None or self._current_chat_id is None:
            return
        try:
            self._event_queue.put(
                {
                    "type": "typing_stop",
                    "chat_id": self._current_chat_id,
                    "source": self._current_task_source,
                    "trace_id": aef_tracing.current_trace_id(),
                    "ts": utc_now_iso(),
                }
            )
        except Exception:
            log.warning("Failed to emit typing stop event", exc_info=True)

    def _emit_task_heartbeat(self, task_id: str, phase: str, trace_id: str = "") -> None:
        if self._event_queue is None:
            return
        try:
            self._event_queue.put(
                {
                    "type": "task_heartbeat",
                    "task_id": task_id,
                    "phase": phase,
                    "trace_id": trace_id or aef_tracing.current_trace_id(),
                    "ts": utc_now_iso(),
                }
            )
        except Exception:
            log.warning("Failed to emit task heartbeat event", exc_info=True)

    def _start_task_heartbeat_loop(self, task_id: str) -> threading.Event | None:
        if self._event_queue is None or not task_id.strip():
            return None
        interval = 30
        stop = threading.Event()
        trace_id = aef_tracing.current_trace_id()
        self._emit_task_heartbeat(task_id, "start", trace_id)

        def _loop() -> None:
            while not stop.wait(interval):
                self._emit_task_heartbeat(task_id, "running", trace_id)

        threading.Thread(target=_loop, daemon=True).start()
        return stop


def make_agent(
    repo_dir: str, drive_root: str, event_queue: Any = None, media_senders: dict[str, Any] | None = None
) -> OuroborosAgent:
    env = Env(repo_dir=pathlib.Path(repo_dir), drive_root=pathlib.Path(drive_root))
    return OuroborosAgent(env, event_queue=event_queue, media_senders=media_senders)
