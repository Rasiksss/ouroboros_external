"""
LLM call, retry, and usage-event logic for the main loop.

Handles per-call retry with backoff and real-time usage event emission.
Extracted from loop.py to keep the main loop orchestrator focused.
"""

from __future__ import annotations

import logging
import pathlib
import queue
import time
from typing import Any, cast

from new_core.llm.types import LLMMessage
from new_core.tools.schema import ToolSchema
from ouroboros.llm import LLMClient, LocalContextTooLargeError, add_usage
from ouroboros.llm.circuit_breaker import (
    PROVIDER_OUTAGE_MESSAGE,
    LLMFailureCategory,
    provider_base_url_for_model,
    provider_circuit_is_open,
    provider_circuit_last_error,
    record_provider_failure,
    record_provider_success,
)
from ouroboros.observability import aef_tracing
from ouroboros.observability.event_record import emit_worker_log_event, record_event
from ouroboros.observability.pricing import emit_llm_usage_event, infer_model_category
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

MAIN_LOOP_MAX_TOKENS = 65_536


def _emit_live_log(event_queue: queue.Queue | None, payload: dict[str, Any]) -> None:
    """Thin wrapper around the SSOT helper — keeps the call-site signature stable."""
    emit_worker_log_event(
        event_queue,
        {"ts": utc_now_iso(), **payload},
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
    event_queue: queue.Queue | None,
    accumulated_usage: dict[str, Any],
    task_type: str = "",
    use_local: bool = False,
) -> LLMMessage | None:
    """
    Call LLM with retry logic, usage tracking, and event emission.

    Returns:
        response_message on success, None on failure after max_retries.
    """
    msg = None
    base_url = provider_base_url_for_model(model)
    if provider_circuit_is_open(model, use_local=use_local, base_url=base_url):
        accumulated_usage["_provider_breaker_open"] = True
        accumulated_usage["_last_llm_error"] = (
            provider_circuit_last_error(
                model,
                use_local=use_local,
                base_url=base_url,
            )
            or PROVIDER_OUTAGE_MESSAGE
        )
        _emit_live_log(
            event_queue,
            {
                "type": "llm_provider_breaker_open",
                "task_id": task_id,
                "task_type": task_type,
                "round": round_idx,
                "model": model,
            },
        )
        return None

    for attempt in range(max_retries):
        try:
            _emit_live_log(
                event_queue,
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
                    resp_msg, usage = llm.chat(**cast(Any, kwargs))
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
            accumulated_usage.pop("_last_llm_error", None)

            display_model = str(usage.get("resolved_model") or model)
            provider = "local" if use_local else str(usage.get("provider") or "openrouter")
            if use_local:
                display_model = f"{model} (local)"
            add_usage(accumulated_usage, usage)

            category = task_type if task_type in ("review", "summarize") else "task"
            emit_llm_usage_event(
                event_queue,
                task_id,
                display_model,
                usage,
                category,
                provider=provider,
                source="loop",
            )

            tool_calls = msg.tool_calls or []
            content = msg.content
            if not tool_calls and (not content or not content.strip()):
                finish_reason = msg.finish_reason
                is_provider_glitch = finish_reason is None
                event_type = "provider_incomplete_response" if is_provider_glitch else "llm_empty_response"
                log_msg = (
                    "Provider returned incomplete response (finish_reason=null)"
                    if is_provider_glitch
                    else "LLM returned empty response (no content, no tool_calls)"
                )
                _emit_live_log(
                    event_queue,
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
                    drive_logs.parent,
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
                )
                accumulated_usage["_last_llm_error"] = _short_error_text(log_msg)
                if is_provider_glitch and record_provider_failure(
                    model,
                    use_local=use_local,
                    base_url=base_url,
                    error="provider_incomplete_response finish_reason=null",
                    category=LLMFailureCategory.INCOMPLETE,
                ):
                    accumulated_usage["_provider_breaker_open"] = True

                if attempt < max_retries - 1:
                    time.sleep(2**attempt)
                    continue
                return None

            accumulated_usage["rounds"] = accumulated_usage.get("rounds", 0) + 1
            accumulated_usage.pop("_provider_breaker_open", None)
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
                event_queue,
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
            record_event(drive_logs.parent, "events", "llm_round", _round_event)
            return msg

        except Exception as e:
            _emit_live_log(
                event_queue,
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
                drive_logs.parent,
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
            )
            accumulated_usage["_last_llm_error"] = _short_error_text(repr(e))
            if record_provider_failure(model, use_local=use_local, base_url=base_url, error=e):
                accumulated_usage["_provider_breaker_open"] = True
            if isinstance(e, LocalContextTooLargeError):
                record_event(
                    drive_logs.parent,
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
                )
                break
            if attempt < max_retries - 1:
                time.sleep(min(2**attempt * 2, 30))

    return None
