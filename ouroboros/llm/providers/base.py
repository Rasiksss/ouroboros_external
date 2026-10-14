"""Shared helpers for direct LLM provider adapters."""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

from new_core.tools.schema import ToolSchema
from ouroboros.llm.targets import RemoteTarget
from ouroboros.tools.naming import PROVIDER_NAME_RE

log = logging.getLogger(__name__)


class ProviderResponseError(RuntimeError):
    """A provider response that cannot be normalized."""


class ProviderRequestError(RuntimeError):
    """A request this provider's wire format cannot represent."""


class ModelUnavailableError(Exception):
    """The selected model version is temporarily disabled upstream (TN08).

    Provider-agnostic, so the retry loop catches it in its own clause and the
    classifier need not name one provider's error class.
    """


def configured_read_timeout_sec() -> float:
    """The deployment's read budget for one provider request, in seconds."""
    from ouroboros.storage.settings.store import load_settings

    try:
        configured = float(load_settings().OUROBOROS_LLM_HTTP_READ_TIMEOUT_SEC or 300)
    except (TypeError, ValueError):
        configured = 300.0
    return max(1.0, configured)


def remote_http_timeout(read_timeout_sec: float | None = None, phase_cap_sec: float | None = None) -> Any:
    """Build an httpx.Timeout from settings, a caller read override, and a per-phase cap.

    `read_timeout_sec` replaces only the configured read window; connect, write,
    and pool stay at their configured values. `phase_cap_sec` caps every phase.
    """
    import httpx

    from ouroboros.storage.settings.store import load_settings

    settings = load_settings()
    configured_read_sec = configured_read_timeout_sec()
    try:
        connect_sec = float(settings.OUROBOROS_LLM_HTTP_CONNECT_TIMEOUT_SEC or 30)
    except (TypeError, ValueError):
        connect_sec = 30.0
    read_sec = configured_read_sec
    connect_sec = max(1.0, connect_sec)
    if read_timeout_sec is not None:
        read_sec = max(0.001, float(read_timeout_sec))
    phase_cap = float("inf") if phase_cap_sec is None else max(0.001, float(phase_cap_sec))
    return httpx.Timeout(
        connect=min(connect_sec, phase_cap),
        read=min(read_sec, phase_cap),
        write=min(configured_read_sec, phase_cap),
        pool=min(connect_sec, phase_cap),
    )


def strip_cache_control(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip cache_control from messages and flatten tool content blocks."""
    cleaned = copy.deepcopy(messages)
    for msg in cleaned:
        msg.pop("reasoning_content", None)
        msg.pop("tools_state_id", None)
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        if msg.get("role") == "tool":
            msg["content"] = "".join(
                block.get("text", "") if isinstance(block, dict) else str(block) for block in content
            )
        else:
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
    return cleaned


def is_empty_answer(content: Any, tool_calls: Any) -> bool:
    """The runtime's rule for a round that produced nothing: no tool call and no text.

    One owner for the predicate: the agent loop treats such a round as a failure and the
    B2Bank canary counts it against the version under test.
    """
    text = content if isinstance(content, str) else ""
    return not tool_calls and not text.strip()


def assistant_history_message(message: dict[str, Any]) -> dict[str, Any]:
    """Keep provider round state when an assistant response enters history."""
    history: dict[str, Any] = {
        "role": "assistant",
        "content": message.get("content") or "",
    }
    tool_calls = message.get("tool_calls")
    if tool_calls:
        history["tool_calls"] = tool_calls
    for field in ("reasoning_content", "tools_state_id"):
        if message.get(field):
            history[field] = message[field]
    return history


# Effort levels the provider APIs accept: low/medium/high (confirmed for both
# GigaChat b2bank and llm-router; per-model support varies). Internal-only
# levels clamp to the nearest wire value; anything unrecognized falls back to
# "medium" (the value b2bank historically sent for any non-none effort).
# Disabling is NOT unified across models yet ("off" vs "none"): the "off" entry
# folds the platform disable synonym into "none" so a disable request never
# falls back to "medium" and silently re-enables reasoning.
_WIRE_EFFORTS = frozenset({"none", "low", "medium", "high"})
_EFFORT_CLAMP = {"minimal": "low", "xhigh": "high", "off": "none"}


def wire_reasoning_effort(value: str) -> str:
    """Map an internal reasoning-effort level onto the wire-safe set."""
    effort = str(value or "none").strip().lower()
    effort = _EFFORT_CLAMP.get(effort, effort)
    return effort if effort in _WIRE_EFFORTS else "medium"


def stringify_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _system_content_text(value: Any) -> str:
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            else:
                parts.append(stringify_content(block))
        return "\n".join(part for part in parts if part)
    return stringify_content(value)


def _append_system_content(system_message: dict[str, Any], content: Any) -> None:
    extra = _system_content_text(content)
    if not extra:
        return

    current = system_message.get("content")
    if isinstance(current, list):
        current.append({"type": "text", "text": extra})
    elif isinstance(current, str):
        system_message["content"] = f"{current}\n\n{extra}" if current else extra
    elif current is None:
        system_message["content"] = extra
    else:
        current_text = stringify_content(current)
        system_message["content"] = f"{current_text}\n\n{extra}" if current_text else extra


def normalize_system_message_order(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    system_message: dict[str, Any] | None = None
    other_messages: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "system":
            other_messages.append(message)
            continue
        if system_message is None:
            system_message = dict(message)
        else:
            _append_system_content(system_message, message.get("content"))

    if system_message is None:
        return other_messages
    return [system_message, *other_messages]


def stringify_tool_description(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "".join(str(part) for part in value if part is not None)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def sanitize_chat_completion_tools(
    tools: list[ToolSchema] | None,
) -> list[ToolSchema]:
    sanitized_tools: list[ToolSchema] = []
    seen_tool_names: set[str] = set()

    if not tools:
        return sanitized_tools

    for tool in tools:
        tool_copy = copy.deepcopy(tool)
        name = tool_copy.name
        if not name:
            continue
        if not PROVIDER_NAME_RE.match(name):
            log.warning("Dropping provider-invalid tool schema name: %s", name)
            continue
        if name in seen_tool_names:
            log.warning("Dropping duplicate tool schema: %s", name)
            continue
        seen_tool_names.add(name)
        tool_copy.description = stringify_tool_description(tool_copy.description)
        if not isinstance(tool_copy.parameters, dict):
            tool_copy.parameters = {"type": "object", "properties": {}}
        sanitized_tools.append(tool_copy)
    sanitized_tools.sort(key=lambda tool: tool.name)
    return sanitized_tools


def normalize_openai_compatible_response(
    resp_dict: dict[str, Any],
    target: RemoteTarget | dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_usage = resp_dict.get("usage") or {}
    if not isinstance(raw_usage, dict):
        raise ValueError("usage must be an object")
    usage = dict(raw_usage)
    choices = resp_dict.get("choices") or [{}]
    if not isinstance(choices, list):
        raise ValueError("choices must be a list")
    choice = choices[0] if choices else {}
    if not isinstance(choice, dict):
        raise ValueError("choices[0] must be an object")
    raw_message = choice.get("message") or {}
    if not isinstance(raw_message, dict):
        raise ValueError("choices[0].message must be an object")
    msg = dict(raw_message)
    finish_reason = choice.get("finish_reason") or choice.get("stop_reason")
    if finish_reason is not None:
        msg["finish_reason"] = finish_reason

    if not usage.get("cached_tokens"):
        prompt_details = usage.get("prompt_tokens_details") or {}
        if isinstance(prompt_details, dict) and prompt_details.get("cached_tokens"):
            usage["cached_tokens"] = int(prompt_details["cached_tokens"])

    if not usage.get("cache_write_tokens"):
        prompt_details_for_write = usage.get("prompt_tokens_details") or {}
        if isinstance(prompt_details_for_write, dict):
            cache_write = (
                prompt_details_for_write.get("cache_write_tokens")
                or prompt_details_for_write.get("cache_creation_tokens")
                or prompt_details_for_write.get("cache_creation_input_tokens")
            )
            if cache_write:
                usage["cache_write_tokens"] = int(cache_write)

    usage["provider"] = str(target.get("provider") or "")
    usage["resolved_model"] = str(target.get("usage_model") or target.get("resolved_model") or "")

    return msg, usage
