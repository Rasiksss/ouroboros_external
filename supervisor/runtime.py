"""Supervisor background loop: ingress → orchestrator → store → executor."""

from __future__ import annotations

import logging
import pathlib
import queue as queue_mod
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ouroboros.storage.runtime_models import OwnerRuntimeState
from ouroboros.storage.settings.store import load_settings
from ouroboros.utils import utc_now_iso
from supervisor.commands import handle_agent_restart_request, handle_slash_command
from supervisor.orchestrator.envelope import InboundEnvelope
from supervisor.orchestrator.intent import TaskIntentKind
from supervisor.orchestrator.orchestrator import TaskOrchestrator
from supervisor.queue import TaskQueue
from supervisor.runtime_context import SupervisorRuntime, set_runtime
from supervisor.state import RuntimeStateRepository, rotate_chat_log_if_needed
from supervisor.workers import TaskExecutor

log = logging.getLogger(__name__)
_WORKING_RETRIEVAL_CLEANUP_KEY = "working_retrieval_cleanup"
_ATTACHMENT_CLEANUP_INTERVAL_SECONDS = 15 * 60
_ATTACHMENT_CLEANUP_JOIN_SECONDS = 1.0


def _coerce_int(value: Any, default: int = 1) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


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
        if not settings.OUROBOROS_CONTEXT_CLEANUP_ENABLED:
            return
        now = now_iso or utc_now_iso()
        state = ctx.store.load_owner_runtime_state() or {}
        checkpoint = state.get(_WORKING_RETRIEVAL_CLEANUP_KEY)
        last_run = checkpoint.get("last_run_at") if isinstance(checkpoint, dict) else ""
        interval = max(1, int(settings.OUROBOROS_CONTEXT_CLEANUP_INTERVAL_HOURS or 24))
        if _timestamp(now) - _timestamp(last_run) < interval * 3600:
            return
        removed = ctx.store.cleanup_expired_working_retrieval(
            now_iso=now,
            limit=int(settings.OUROBOROS_CONTEXT_CLEANUP_MAX_ROWS or 5000),
        )
        state[_WORKING_RETRIEVAL_CLEANUP_KEY] = {
            "last_run_at": now,
            "removed": removed,
        }
        ctx.store.save_owner_runtime_state(state)
    except Exception:
        log.debug("Working-memory cleanup skipped after error", exc_info=True)


def _cleanup_expired_attachments(drive_root: pathlib.Path) -> int:
    from ouroboros.storage.blob.factory import create_storage_provider
    from ouroboros.storage.chat_attachments import cleanup_expired_attachments

    return cleanup_expired_attachments(create_storage_provider(data_dir=drive_root))


def _attachment_cleanup_loop(ctx: Any, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            _cleanup_expired_attachments(pathlib.Path(ctx.drive_root))
        except Exception:
            log.warning("Attachment cleanup failed", exc_info=True)
        stop_event.wait(_ATTACHMENT_CLEANUP_INTERVAL_SECONDS)


def _build_attachment_cleanup_thread(
    ctx: Any,
    stop_event: threading.Event,
) -> threading.Thread:
    return threading.Thread(
        target=_attachment_cleanup_loop,
        args=(ctx, stop_event),
        name="attachment-cleanup",
        daemon=True,
    )


def _stop_attachment_cleanup_thread(
    thread: threading.Thread | None,
    stop_event: threading.Event,
) -> None:
    stop_event.set()
    if thread is not None:
        thread.join(timeout=_ATTACHMENT_CLEANUP_JOIN_SECONDS)
        if thread.is_alive():
            log.warning("Attachment cleanup thread is still running during shutdown")


@dataclass
class SupervisorContext:
    drive_root: pathlib.Path
    repo_dir: pathlib.Path
    bridge: Any
    orchestrator: TaskOrchestrator
    executor: TaskExecutor
    store: Any
    task_queue: TaskQueue
    state_repo: RuntimeStateRepository
    send_with_budget: Callable[..., Any]
    send_typing: Callable[..., Any]
    send_photo: Callable[..., Any]
    safe_restart: Callable[..., tuple[bool, str]]
    request_restart: Callable[[], None]
    cancel_task_by_id: Callable[[str], bool]
    enqueue_task: Callable[..., Any]
    update_usage_from_llm: Callable[[dict[str, Any]], None]
    append_jsonl: Callable[..., Any]
    soft_timeout: int = 600
    hard_timeout: int = 1800
    runtime_transport: Any = None
    dialogs_reply_tokens: dict[str, Any] = field(default_factory=dict)

    @property
    def DRIVE_ROOT(self) -> pathlib.Path:
        return self.drive_root

    @property
    def WORKERS(self) -> dict[int, Any]:
        return self.executor.worker_slots_dict()

    def kill_workers(self, force: bool = True, **kwargs: Any) -> None:
        del force
        self.executor.shutdown(**kwargs)

    def spawn_workers(self, n: int = 0) -> None:
        del n
        self.executor.ensure_started()

    def load_state(self) -> OwnerRuntimeState:
        return self.state_repo.load()

    def save_state(self, state: OwnerRuntimeState) -> None:
        self.state_repo.save(state)

    def get_chat_agent(self) -> Any:
        return self.executor.get_agent()


class SupervisorLoop:
    def __init__(
        self,
        *,
        ctx: SupervisorContext,
        bridge: Any,
        stop_event: threading.Event,
    ) -> None:
        self.ctx = ctx
        self.bridge = bridge
        self.stop_event = stop_event

    def process_bridge_updates(self, offset: int) -> int:
        updates = self.bridge.get_updates(offset=offset, timeout=1)
        for upd in updates:
            offset = int(upd["update_id"]) + 1
            msg = upd.get("message") or {}
            if not msg:
                continue

            envelope = InboundEnvelope.from_bridge_message(msg)
            chat_id: int | str = envelope.chat_id
            if not isinstance(chat_id, int):
                try:
                    chat_id = int(str(chat_id))
                except (TypeError, ValueError):
                    pass
            user_id: int | str = envelope.user_id or envelope.chat_id
            if not isinstance(user_id, int):
                try:
                    user_id = int(str(user_id))
                except (TypeError, ValueError):
                    pass
            log_text = envelope.effective_text()
            now_iso = utc_now_iso()

            with self.ctx.state_repo.mutate() as state:
                if state.owner_id is None:
                    state.owner_id = user_id
                    state.owner_chat_id = chat_id
                state.last_owner_message_at = now_iso

            from supervisor.message_bus import log_chat

            if not envelope.suppress_chat_log and not envelope.image_base64:
                log_chat(
                    "owner",
                    _coerce_int(chat_id),
                    _coerce_int(user_id),
                    log_text,
                    source=str(msg.get("source") or envelope.source),
                    sender_label=envelope.sender_label,
                    sender_session_id=envelope.sender_session_id,
                    client_message_id=envelope.client_message_id,
                    transport=envelope.transport,
                )

            if not log_text and not envelope.image_base64:
                continue

            result = self.ctx.orchestrator.submit(envelope)
            if result.intent == TaskIntentKind.SLASH_COMMAND:
                handle_slash_command(
                    result.slash_command,
                    chat_id=chat_id,
                    ctx=self.ctx,
                    source=envelope.source,
                    transport=envelope.transport,
                )
        return offset

    def run_tick(self, offset: int) -> int:
        from supervisor.events import dispatch_event

        rotate_chat_log_if_needed(self.ctx.drive_root)
        _maybe_cleanup_working_retrieval(self.ctx)
        self.ctx.executor.ensure_healthy()

        event_q = self.ctx.executor.get_event_q()
        while True:
            try:
                evt = event_q.get_nowait()
            except queue_mod.Empty:
                break
            if evt.get("type") == "restart_request":
                handle_agent_restart_request(evt, self.ctx)
                continue
            dispatch_event(evt, self.ctx)

        self.ctx.task_queue.enforce_task_timeouts(self.ctx.executor)
        self.ctx.executor.assign_next()
        return self.process_bridge_updates(offset)

    def run(self) -> None:
        offset = 0
        crash_count = 0
        while not self.stop_event.is_set():
            try:
                offset = self.run_tick(offset)
                crash_count = 0
                time.sleep(0.5)
            except Exception as exc:
                crash_count += 1
                log.error("Supervisor loop crash #%d: %s", crash_count, exc, exc_info=True)
                if crash_count >= 3:
                    log.critical("Supervisor exceeded max retries.")
                    return
                time.sleep(min(30, 2**crash_count))


def bootstrap_supervisor_repo(settings: dict, git_ops_module=None) -> tuple[bool, str]:
    del settings, git_ops_module
    return True, "OK: headless runtime has no managed git bootstrap"


def build_supervisor_context(
    *,
    settings: dict[str, Any],
    runtime: SupervisorRuntime,
    bridge: Any,
    request_restart: Callable[[], None],
    safe_restart: Callable[..., tuple[bool, str]],
) -> SupervisorContext:
    from supervisor.events import dispatch_event  # noqa: F401
    from supervisor.message_bus import send_with_budget
    from supervisor.queue import cancel_task_by_id, enqueue_task
    from supervisor.state import append_jsonl, update_usage_from_llm

    set_runtime(runtime)

    return SupervisorContext(
        drive_root=runtime.drive_root,
        repo_dir=runtime.repo_dir,
        bridge=bridge,
        orchestrator=runtime.orchestrator,
        executor=runtime.executor,
        store=runtime.store,
        task_queue=runtime.task_queue,
        state_repo=runtime.state_repo,
        send_with_budget=send_with_budget,
        send_typing=bridge.send_chat_action,
        send_photo=bridge.send_photo,
        safe_restart=safe_restart,
        request_restart=request_restart,
        cancel_task_by_id=cancel_task_by_id,
        enqueue_task=enqueue_task,
        update_usage_from_llm=update_usage_from_llm,
        append_jsonl=append_jsonl,
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
    request_restart: Callable[[], None],
    safe_restart: Callable[..., tuple[bool, str]],
    apply_settings_to_env: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    log.info(
        "Supervisor startup begin drive_root=%s repo_dir=%s",
        drive_root,
        repo_dir,
    )
    if apply_settings_to_env is not None:
        apply_settings_to_env(settings)
        log.info("Supervisor applied settings to process environment")

    runtime_transport: Any = None
    attachment_cleanup_stop = threading.Event()
    attachment_cleanup_thread: threading.Thread | None = None
    try:
        from supervisor.message_bus import LocalChatBridge

        bridge = LocalChatBridge(settings)
        log.info("Supervisor local chat bridge initialized")
        settings_with_bus = dict(settings)

        ok, msg = bootstrap_supervisor_repo(settings)
        if not ok:
            log.error("Supervisor bootstrap failed: %s", msg)
        else:
            log.info("Supervisor repo bootstrap complete: %s", msg)

        from supervisor.bootstrap import build_supervisor_runtime

        runtime = build_supervisor_runtime(
            settings=settings_with_bus,
            drive_root=drive_root,
            repo_dir=repo_dir,
            bridge=bridge,
        )
        log.info("Supervisor runtime components wired")

        ctx = build_supervisor_context(
            settings=settings,
            runtime=runtime,
            bridge=bridge,
            request_restart=request_restart,
            safe_restart=safe_restart,
        )
        log.info("Supervisor context built")
        attachment_cleanup_thread = _build_attachment_cleanup_thread(
            ctx,
            attachment_cleanup_stop,
        )
        attachment_cleanup_thread.start()

        from ouroboros.storage.settings.store import load_settings
        from supervisor.server_runtime import (
            attach_dialogs_transport_fanout,
            build_server_runtime_transport,
            dialogs_feature_enabled,
            start_dialogs_runtime_input_thread,
        )

        app_settings = load_settings()
        log.info("Supervisor app settings loaded for runtime transport")
        runtime_transport = build_server_runtime_transport(
            settings=app_settings,
            bridge=bridge,
            logger=log,
        )
        ctx.runtime_transport = runtime_transport
        dialogs_enabled = dialogs_feature_enabled(app_settings)
        log.info(
            "Supervisor runtime transport built dialogs_enabled=%s provider_active=%s startup_error=%s",
            dialogs_enabled,
            getattr(runtime_transport, "provider_active", False),
            getattr(runtime_transport, "startup_error", None),
        )
        if dialogs_enabled:
            ctx = attach_dialogs_transport_fanout(ctx, runtime_transport, ctx.load_state)
            log.info("Supervisor dialogs transport fanout attached")
            provider_state = ctx.load_state().provider_state or {}
            dialogs_state = provider_state.get("dialogs") if isinstance(provider_state, dict) else {}
            dialogs_cursor_ref = {"value": int((dialogs_state or {}).get("seq") or 0)}
            runtime_transport.dialogs_input_thread = start_dialogs_runtime_input_thread(
                runtime_transport=runtime_transport,
                dialogs_cursor_ref=dialogs_cursor_ref,
                ctx=ctx,
                logger=log,
                stop_event=stop_event,
            )
            log.info(
                "Supervisor dialogs input thread started cursor=%s thread=%s",
                dialogs_cursor_ref["value"],
                getattr(runtime_transport.dialogs_input_thread, "name", None),
            )
        else:
            log.info("Supervisor dialogs transport disabled (missing endpoint or bot token)")

        runtime.executor.set_media_senders(
            image=getattr(ctx, "send_image", bridge.send_image),
            file=getattr(ctx, "send_file", bridge.send_file),
        )
        runtime.executor.ensure_started()
        log.info("Supervisor task executor started")

        ready_event.set()
        log.info("Supervisor ready.")
        SupervisorLoop(ctx=ctx, bridge=bridge, stop_event=stop_event).run()
    except Exception as exc:
        on_error(f"Supervisor init failed: {exc}")
        log.critical("Supervisor initialization failed", exc_info=True)
        ready_event.set()
    finally:
        _stop_attachment_cleanup_thread(
            attachment_cleanup_thread,
            attachment_cleanup_stop,
        )
        close_transport = getattr(runtime_transport, "close", None)
        if callable(close_transport):
            close_transport()


__all__ = [
    "SupervisorContext",
    "SupervisorLoop",
    "bootstrap_supervisor_repo",
    "build_supervisor_context",
    "run_supervisor",
]
