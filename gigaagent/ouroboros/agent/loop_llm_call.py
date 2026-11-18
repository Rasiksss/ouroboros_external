"""
LLM call, retry, and usage-event logic for the main loop.

Handles per-call retry with backoff and real-time usage event emission.
Extracted from loop.py to keep the main loop orchestrator focused.
"""

from __future__ import annotations

import contextvars
import logging
import pathlib
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from functools import partial
from typing import Any, cast

from new_core.identity import OwnerScope
from new_core.llm.types import LLMFailure, LLMMessage
from new_core.loop import CancellationSignal
from new_core.tools.schema import ToolSchema
from ouroboros.agent_events.interfaces import IAgentEventPublisher
from ouroboros.llm import LLMCallCancelled, LLMClient, LocalContextTooLargeError, add_usage
from ouroboros.llm.circuit_breaker import (
    PROVIDER_OUTAGE_MESSAGE,
    LLMFailureCategory,
    classify_failure,
    provider_base_url_for_model,
    provider_circuit_is_open,
    provider_circuit_last_error,
    record_provider_failure,
    record_provider_success,
)
from ouroboros.llm.providers.base import ModelUnavailableError, is_empty_answer
from ouroboros.observability import aef_tracing
from ouroboros.observability.event_record import emit_worker_log_event, record_event
from ouroboros.observability.interfaces.audit_events import IAuditEventRepository
from ouroboros.observability.pricing import emit_llm_usage_event, infer_model_category
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

MAIN_LOOP_MAX_TOKENS = 65_536
_MODEL_WAIT_SLICE_SEC = 0.05


def _emit_live_log(events: IAgentEventPublisher | None, scope: OwnerScope, payload: dict[str, Any]) -> None:
    """Thin wrapper around the SSOT helper — keeps the call-site signature stable."""
    emit_worker_log_event(
        events,
        {"ts": utc_now_iso(), **payload},
        scope=scope,
        log_label="LLM live",
    )


def _short_error_text(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _add_aef_llm_result(controller: Any, *, response_headers: dict[str, Any], response_body: Any) -> None:
    try:
        controller.add_giga_llm_result(
            response_headers=aef_tracing.mask_trace_headers(response_headers),
            response_body=aef_tracing.make_aef_json_safe(response_body),
        )
    except Exception:
        log.debug("Failed to add AEF LLM result", exc_info=True)


def call_until_stopped(
    llm: LLMClient,
    kwargs: dict[str, Any],
    cancellation: CancellationSignal,
    executor: ThreadPoolExecutor,
    *,
    task_id: str,
    on_late_usage: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Wait for one synchronous provider call until its run is stopped."""
    if cancellation.is_stopped():
        return None
    future = executor.submit(
        contextvars.copy_context().run,
        partial(llm.chat, **cast(Any, kwargs), stop_requested=cancellation.is_stopped),
    )
    while True:
        try:
            return future.result(timeout=_MODEL_WAIT_SLICE_SEC)
        except FutureTimeoutError:
            if future.done():
                return future.result()
            if not cancellation.is_stopped():
                continue

            def _observe_late(outcome: Any) -> None:
                try:
                    _, usage = outcome.result()
                except LLMCallCancelled:
                    log.info("Stopped model call cancelled before dispatch (task_id=%s)", task_id)
                except BaseException as exc:
                    log.warning(
                        "Stopped model call failed after abandonment (task_id=%s error=%s)",
                        task_id,
                        type(exc).__name__,
                    )
                else:
                    if on_late_usage is not None:
                        on_late_usage(usage)
                    log.info("Stopped model call finished after abandonment (task_id=%s)", task_id)

            future.add_done_callback(_observe_late)
            return None


def _stop_during_wait(cancellation: CancellationSignal, seconds: float) -> bool:
    remaining = max(0.0, seconds)
    while remaining > 0:
        if cancellation.is_stopped():
            return True
        pause = min(_MODEL_WAIT_SLICE_SEC, remaining)
        time.sleep(pause)
        remaining -= pause
    return cancellation.is_stopped()


def call_llm_with_retry(
    llm: LLMClient,
    messages: list[dict[str, Any]],
    model: str,
    tools: list[ToolSchema] | None,
    effort: str,
    max_retries: int,
    drive_logs: pathlib.Path,
    task_id: str,
    round_idx: int,
    events: IAgentEventPublisher | None,
    accumulated_usage: dict[str, Any],
    task_type: str = "",
    use_local: bool = False,
    *,
    scope: OwnerScope,
    cancellation: CancellationSignal,
    audit_event_repository: IAuditEventRepository,
) -> LLMMessage | LLMFailure:
    """Return the response or failure after bounded retries, recording usage separately."""
    failure = LLMFailure("No model call attempted")
    if cancellation.is_stopped():
        return LLMFailure("Task stopped")
    base_url = provider_base_url_for_model(model)
    if provider_circuit_is_open(model, use_local=use_local, base_url=base_url):
        failure = LLMFailure(
            provider_circuit_last_error(
                model,
                use_local=use_local,
                base_url=base_url,
            )
            or PROVIDER_OUTAGE_MESSAGE
        )
        _emit_live_log(
            events,
            scope,
            {
                "type": "llm_provider_breaker_open",
                "task_id": task_id,
                "task_type": task_type,
                "round": round_idx,
                "model": model,
            },
        )
        return failure

    def report_usage(usage: dict[str, Any]) -> None:
        emit_llm_usage_event(
            events,
            task_id,
            f"{model} (local)" if use_local else str(usage.get("resolved_model") or model),
            usage,
            task_type if task_type in ("review", "summarize") else "task",
            provider="local" if use_local else str(usage.get("provider") or "openrouter"),
            source="loop",
            scope=scope,
        )

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm-call")
    try:
        for attempt in range(max_retries):
            if cancellation.is_stopped():
                return LLMFailure("Task stopped")
            try:
                _emit_live_log(
                    events,
                    scope,
                    {
                        "type": "llm_round_started",
                        "task_id": task_id,
                        "task_type": task_type,
                        "round": round_idx,
                        "attempt": attempt + 1,
                        "model": model,
                        "reasoning_effort": effort,
                        "use_local": bool(use_local),
                    },
                )
                kwargs = {
                    "messages": messages,
                    "model": model,
                    "reasoning_effort": effort,
                    "max_tokens": MAIN_LOOP_MAX_TOKENS,
                    "use_local": use_local,
                }
                if tools:
                    kwargs["tools"] = tools
                request_body: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "reasoning_effort": effort,
                    "max_tokens": MAIN_LOOP_MAX_TOKENS,
                    "use_local": bool(use_local),
                }
                if tools:
                    request_body["tools"] = [t.to_dict() for t in tools]
                with aef_tracing.llm_request(
                    request_headers=aef_tracing.with_trace_header({}),
                    request_body=aef_tracing.mask_llm_request_payload(request_body),
                ) as llm_span:
                    try:
                        outcome = call_until_stopped(
                            llm, kwargs, cancellation, executor, task_id=task_id, on_late_usage=report_usage
                        )
                        if outcome is None:
                            return LLMFailure("Task stopped")
                        resp_msg, usage = outcome
                    except Exception as exc:
                        _add_aef_llm_result(
                            llm_span,
                            response_headers={},
                            response_body=aef_tracing.mask_llm_response_payload(
                                model,
                                {},
                                {},
                                error=exc,
                            ),
                        )
                        raise
                    _add_aef_llm_result(
                        llm_span,
                        response_headers={},
                        response_body=aef_tracing.mask_llm_response_payload(
                            model,
                            resp_msg,
                            usage,
                        ),
                    )
                msg = LLMMessage.from_dict(resp_msg)

                display_model = str(usage.get("resolved_model") or model)
                provider = "local" if use_local else str(usage.get("provider") or "openrouter")
                if use_local:
                    display_model = f"{model} (local)"
                add_usage(accumulated_usage, usage)

                report_usage(usage)

                tool_calls = msg.tool_calls or []
                content = msg.content
                if is_empty_answer(content, tool_calls):
                    finish_reason = msg.finish_reason
                    is_provider_glitch = finish_reason is None
                    event_type = "provider_incomplete_response" if is_provider_glitch else "llm_empty_response"
                    log_msg = (
                        "Provider returned incomplete response (finish_reason=null)"
                        if is_provider_glitch
                        else "LLM returned empty response (no content, no tool_calls)"
                    )
                    _emit_live_log(
                        events,
                        scope,
                        {
                            "type": event_type,
                            "task_id": task_id,
                            "task_type": task_type,
                            "round": round_idx,
                            "attempt": attempt + 1,
                            "model": model,
                            "finish_reason": finish_reason,
                        },
                    )
                    log.warning("%s, attempt %d/%d", log_msg, attempt + 1, max_retries)

                    record_event(
                        "events",
                        event_type,
                        {
                            "ts": utc_now_iso(),
                            "task_id": task_id,
                            "round": round_idx,
                            "attempt": attempt + 1,
                            "model": model,
                            "raw_content": repr(content)[:500] if content else None,
                            "raw_tool_calls": repr(tool_calls)[:500] if tool_calls else None,
                            "finish_reason": finish_reason,
                        },
                        audit_event_repository=audit_event_repository,
                    )
                    failure = LLMFailure(_short_error_text(log_msg))
                    if is_provider_glitch:
                        record_provider_failure(
                            model,
                            use_local=use_local,
                            base_url=base_url,
                            error="provider_incomplete_response finish_reason=null",
                            category=LLMFailureCategory.INCOMPLETE,
                        )

                    if attempt < max_retries - 1:
                        if _stop_during_wait(cancellation, 2**attempt):
                            return LLMFailure("Task stopped")
                        continue
                    return failure

                accumulated_usage["rounds"] = accumulated_usage.get("rounds", 0) + 1
                record_provider_success(model, use_local=use_local, base_url=base_url)

                prompt_tokens = int(usage.get("prompt_tokens") or 0)
                completion_tokens = int(usage.get("completion_tokens") or 0)
                cached_tokens = int(usage.get("cached_tokens") or 0)
                cache_write_tokens = int(usage.get("cache_write_tokens") or 0)
                cache_hit_rate = (cached_tokens / prompt_tokens) if prompt_tokens > 0 else 0.0
                _round_event = {
                    "ts": utc_now_iso(),
                    "type": "llm_round",
                    "task_id": task_id,
                    "round": round_idx,
                    "model": display_model,
                    "reasoning_effort": effort,
                    "provider": provider,
                    "source": "loop",
                    "model_category": infer_model_category(display_model),
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "cached_tokens": cached_tokens,
                    "cache_write_tokens": cache_write_tokens,
                    "cache_hit_rate": cache_hit_rate,
                }
                _emit_live_log(
                    events,
                    scope,
                    {
                        "type": "llm_round_finished",
                        "task_id": task_id,
                        "task_type": task_type,
                        "round": round_idx,
                        "attempt": attempt + 1,
                        "model": display_model,
                        "reasoning_effort": effort,
                        "prompt_tokens": _round_event["prompt_tokens"],
                        "completion_tokens": _round_event["completion_tokens"],
                        "cached_tokens": _round_event["cached_tokens"],
                        "cache_write_tokens": _round_event["cache_write_tokens"],
                        "response_kind": "tool_calls" if tool_calls else "message",
                        "tool_call_count": len(tool_calls),
                        "has_text": bool(content and str(content).strip()),
                    },
                )
                record_event(
                    "events",
                    "llm_round",
                    _round_event,
                    audit_event_repository=audit_event_repository,
                )
                return msg

            # TN08: its own clause before the general one — the disabled version is never retried.
            except ModelUnavailableError as exc:
                record_provider_failure(model, use_local=use_local, base_url=base_url, error=exc)
                _emit_live_log(
                    events,
                    scope,
                    {
                        "type": "llm_round_error",
                        "task_id": task_id,
                        "task_type": task_type,
                        "round": round_idx,
                        "attempt": attempt + 1,
                        "model": model,
                        "error": repr(exc),
                    },
                )
                record_event(
                    "events",
                    "model_unavailable",
                    {
                        "ts": utc_now_iso(),
                        "task_id": task_id,
                        "round": round_idx,
                        "attempt": attempt + 1,
                        "model": model,
                        "error": repr(exc),
                    },
                    audit_event_repository=audit_event_repository,
                )
                return LLMFailure(_short_error_text(repr(exc)), model_unavailable=True)

            except LLMCallCancelled:
                return LLMFailure("Task stopped")
            except Exception as e:
                _emit_live_log(
                    events,
                    scope,
                    {
                        "type": "llm_round_error",
                        "task_id": task_id,
                        "task_type": task_type,
                        "round": round_idx,
                        "attempt": attempt + 1,
                        "model": model,
                        "error": repr(e),
                    },
                )
                record_event(
                    "events",
                    "llm_api_error",
                    {
                        "ts": utc_now_iso(),
                        "task_id": task_id,
                        "round": round_idx,
                        "attempt": attempt + 1,
                        "model": model,
                        "error": repr(e),
                    },
                    audit_event_repository=audit_event_repository,
                )
                failure_category = (
                    LLMFailureCategory.CONTEXT if isinstance(e, LocalContextTooLargeError) else classify_failure(e)
                )
                failure = LLMFailure(
                    _short_error_text(repr(e)), context_overflow=failure_category == LLMFailureCategory.CONTEXT
                )
                record_provider_failure(
                    model, use_local=use_local, base_url=base_url, error=e, category=failure_category
                )
                if isinstance(e, LocalContextTooLargeError):
                    record_event(
                        "events",
                        "local_context_overflow",
                        {
                            "ts": utc_now_iso(),
                            "task_id": task_id,
                            "round": round_idx,
                            "attempt": attempt + 1,
                            "model": model,
                            "error": repr(e),
                        },
                        audit_event_repository=audit_event_repository,
                    )
                    break
                if failure.context_overflow:
                    break
                if attempt < max_retries - 1 and _stop_during_wait(cancellation, min(2**attempt * 2, 30)):
                    return LLMFailure("Task stopped")

        return failure
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
