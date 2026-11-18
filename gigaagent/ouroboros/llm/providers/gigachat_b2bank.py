"""GigaChat B2Bank direct provider (mTLS REST, non-OpenAI message shapes)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable, Generator
from dataclasses import dataclass, field as dataclass_field
from typing import Any

import httpx

from new_core.llm.usage import add_usage
from new_core.tools.schema import ToolSchema
from ouroboros.llm.env_utils import env_positive_float, env_positive_int, env_truthy
from ouroboros.llm.models import ModelRef, usage_model_name
from ouroboros.llm.preview_canary import record_preview_failure, select_request_model
from ouroboros.llm.providers.base import (
    ModelUnavailableError,
    ProviderRequestError,
    ProviderResponseError,
    configured_read_timeout_sec,
    is_empty_answer,
    remote_http_timeout,
    sanitize_chat_completion_tools,
    stringify_content,
    stringify_tool_description,
    wire_reasoning_effort,
)
from ouroboros.llm.targets import RemoteTarget
from ouroboros.observability import aef_tracing

log = logging.getLogger(__name__)

PROVIDER_ID = "gigachat-b2bank"
_CHAT_API_VERSIONS = frozenset({"v1", "v2"})
_SAFE_FUNCTION_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z_]{0,63}$")
_TRAILING_VERSION_RE = re.compile(r"/v\d+$", re.IGNORECASE)
_MAX_B2BANK_IMAGE_COUNT = 10
_MAX_B2BANK_IMAGE_BYTES = 15_000_000
_MAX_B2BANK_TOTAL_IMAGE_BYTES = 80_000_000
_MAX_ERROR_BODY_CHARS = 1_000
_ERROR_BODY_FIELDS = (
    "status",
    "code",
    "error_code",
    "type",
    "request_id",
    "trace_id",
    "message",
    "messages",
    "details",
    "errors",
)
_SAFE_ERROR_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
_MODEL_UNAVAILABLE_MARKER = "service is temporarily unavailable"
_MODEL_UNAVAILABLE_REASON = "model_temporarily_unavailable"
_IMAGE_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/tiff": "tiff",
    "image/tif": "tif",
    "image/bmp": "bmp",
    "image/gif": "gif",
    "image/webp": "webp",
}


class B2BankProtocolError(RuntimeError):
    """A B2Bank request or response that violates the selected wire contract."""


class B2BankHTTPError(httpx.HTTPStatusError):
    """A bounded, payload-safe B2Bank HTTP error."""


class B2BankModelUnavailableError(B2BankHTTPError, ModelUnavailableError):
    """HTTP 403 whose body signals the selected model version is temporarily disabled (TN08)."""


@dataclass(frozen=True)
class ToolNameMap:
    runtime_to_provider: dict[str, str]
    provider_to_runtime: dict[str, str]

    def provider_name(self, runtime_name: str) -> str:
        name = str(runtime_name or "").strip()
        provider_name = self.runtime_to_provider.get(name)
        if provider_name is None:
            raise B2BankProtocolError(f"B2Bank request references unknown runtime function {name!r}")
        return provider_name

    def runtime_name(self, provider_name: str) -> str:
        name = str(provider_name or "").strip()
        runtime_name = self.provider_to_runtime.get(name)
        if runtime_name is None:
            raise B2BankProtocolError(f"B2Bank response contains unknown function alias {name!r}")
        return runtime_name


@dataclass(frozen=True)
class _PreparedImage:
    data: bytes
    mime: str
    digest: str


def _base26(value: int) -> str:
    if value == 0:
        return "a"
    letters: list[str] = []
    while value:
        value, remainder = divmod(value, 26)
        letters.append(chr(ord("a") + remainder))
    return "".join(reversed(letters))


def _tool_name_digest(runtime_name: str) -> str:
    digest = hashlib.blake2s(runtime_name.encode("utf-8")).digest()
    return _base26(int.from_bytes(digest, "big")).rjust(55, "a")


def provider_function_name(runtime_name: str) -> str:
    """Return the deterministic base alias used when no request map is available."""
    name = str(runtime_name or "").strip()
    if not name or _SAFE_FUNCTION_NAME_RE.fullmatch(name):
        return name
    return f"f{_tool_name_digest(name)[:10]}"


def runtime_function_name(provider_name: str, tool_names: ToolNameMap | None = None) -> str:
    """Restore a runtime name through the request-local map."""
    if tool_names is None:
        return str(provider_name or "").strip()
    return tool_names.runtime_name(provider_name)


def _tool_choice_name(tool_choice: Any) -> str:
    if isinstance(tool_choice, dict):
        function = tool_choice.get("function") or tool_choice
        if isinstance(function, dict):
            return str(function.get("name") or "").strip()
    if isinstance(tool_choice, str) and tool_choice not in {"auto", "none", "required", "any"}:
        return tool_choice.strip()
    return ""


def _runtime_tool_names(
    tools: list[dict[str, Any]] | None,
    messages: list[dict[str, Any]],
    tool_choice: Any = None,
) -> set[str]:
    names: set[str] = set()
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") or {}
        if isinstance(function, dict):
            name = str(function.get("name") or "").strip()
            if name:
                names.add(name)
    for message in messages:
        if not isinstance(message, dict):
            continue
        explicit_name = str(message.get("name") or message.get("tool_name") or "").strip()
        if explicit_name and str(message.get("role") or "").lower() == "tool":
            names.add(explicit_name)
        for tool_call in message.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function") or {}
            if isinstance(function, dict):
                name = str(function.get("name") or "").strip()
                if name:
                    names.add(name)
    forced_name = _tool_choice_name(tool_choice)
    if forced_name:
        names.add(forced_name)
    return names


def _as_tool_dicts(tools: Any) -> list[dict[str, Any]] | None:
    """Accept typed ToolSchema entries or raw OpenAI-style dicts."""
    if tools is None:
        return None
    return [tool.to_dict() if isinstance(tool, ToolSchema) else tool for tool in tools]


def build_tool_name_map(
    tools: list[ToolSchema] | list[dict[str, Any]] | None,
    messages: list[dict[str, Any]],
    tool_choice: Any = None,
) -> ToolNameMap:
    """Build the collision-safe name map for one B2Bank request."""
    tools = _as_tool_dicts(tools)
    runtime_names = _runtime_tool_names(tools, messages, tool_choice)
    runtime_to_provider: dict[str, str] = {}
    provider_to_runtime: dict[str, str] = {}

    for runtime_name in sorted(runtime_names):
        if _SAFE_FUNCTION_NAME_RE.fullmatch(runtime_name):
            existing = provider_to_runtime.get(runtime_name)
            if existing is not None and existing != runtime_name:
                raise B2BankProtocolError(f"B2Bank function name collision between {existing!r} and {runtime_name!r}")
            runtime_to_provider[runtime_name] = runtime_name
            provider_to_runtime[runtime_name] = runtime_name

    for runtime_name in sorted(runtime_names):
        if runtime_name in runtime_to_provider:
            continue
        digest = _tool_name_digest(runtime_name)
        length = 10
        while True:
            if length > len(digest):
                digest += _tool_name_digest(f"{runtime_name}:{length}")
            alias = f"f{digest[:length]}"
            existing = provider_to_runtime.get(alias)
            if existing is None or existing == runtime_name:
                break
            length += 2
        runtime_to_provider[runtime_name] = alias
        provider_to_runtime[alias] = runtime_name

    if len(runtime_to_provider) != len(provider_to_runtime):
        raise B2BankProtocolError("B2Bank function aliases are not unique")
    return ToolNameMap(runtime_to_provider, provider_to_runtime)


def parameters_schema(parameters: Any) -> dict[str, Any]:
    """Normalize semantic object nodes without changing pure refs/combinators."""

    def normalize(value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)

        properties = normalized.get("properties")
        if isinstance(properties, dict):
            normalized["properties"] = {
                str(prop_name): normalize(prop_schema) for prop_name, prop_schema in properties.items()
            }
        additional = normalized.get("additionalProperties")
        if isinstance(additional, dict):
            normalized["additionalProperties"] = normalize(additional)
        items = normalized.get("items")
        if isinstance(items, dict):
            normalized["items"] = normalize(items)
        for keyword in ("anyOf", "oneOf", "allOf"):
            branches = normalized.get(keyword)
            if isinstance(branches, list):
                normalized[keyword] = [normalize(branch) for branch in branches]

        is_object = (
            normalized.get("type") == "object" or "properties" in normalized or "additionalProperties" in normalized
        )
        if is_object:
            normalized.setdefault("type", "object")
            if not isinstance(normalized.get("properties"), dict):
                normalized["properties"] = {}
        return normalized

    if not isinstance(parameters, dict):
        return {"type": "object", "properties": {}}
    normalized = normalize(parameters)
    if not normalized:
        return {"type": "object", "properties": {}}
    if "$ref" not in normalized and not any(keyword in normalized for keyword in ("anyOf", "oneOf", "allOf")):
        normalized.setdefault("type", "object")
    if not isinstance(normalized.get("properties"), dict):
        normalized["properties"] = {}
    return normalized


def gigachat_b2bank_credentials_ready(settings: Any) -> bool:
    """Return True when enough GigaChat B2Bank config is present for runtime use."""
    if hasattr(settings, "GIGACHAT_B2BANK_BASE_URL"):
        base_url = str(settings.GIGACHAT_B2BANK_BASE_URL or "").strip()
        verify_ssl = env_truthy(settings.GIGACHAT_B2BANK_VERIFY_SSL, True)
    elif hasattr(settings, "get"):
        base_url = str(settings.get("GIGACHAT_B2BANK_BASE_URL", "") or "").strip()
        verify_ssl = env_truthy(settings.get("GIGACHAT_B2BANK_VERIFY_SSL", True), True)
    else:
        return False

    if not base_url:
        return False
    if not verify_ssl:
        return True
    return True


def normalized_base_url(base_url: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    return _TRAILING_VERSION_RE.sub("", base)


def _chat_api_version(value: Any) -> str:
    version = str(value or "v1").strip().lower()
    if version not in _CHAT_API_VERSIONS:
        raise ValueError("GIGACHAT_B2BANK_CHAT_API_VERSION must be either 'v1' or 'v2'")
    return version


def _chat_url(base_url: str, version: str) -> str:
    return f"{normalized_base_url(base_url)}/{_chat_api_version(version)}/chat/completions"


def _files_url(base_url: str) -> str:
    return f"{normalized_base_url(base_url)}/v1/files"


def _api_url(base_url: str, path: str, version: str = "v1") -> str:
    """Compatibility helper for callers that still construct a single B2Bank path."""
    clean_path = "/" + str(path or "").lstrip("/")
    if clean_path == "/files":
        return _files_url(base_url)
    return f"{normalized_base_url(base_url)}/{_chat_api_version(version)}{clean_path}"


def _decode_data_image_url(url: str) -> tuple[bytes, str]:
    text = str(url or "").strip()
    marker = ";base64,"
    if not text.startswith("data:") or marker not in text:
        raise ValueError("B2Bank image_url must be provided as image_base64 data URL")
    header, encoded = text[len("data:") :].split(marker, 1)
    mime = header.strip().lower() or "image/png"
    if mime not in _IMAGE_EXTENSIONS:
        raise ValueError(f"B2Bank image_base64 MIME type is not supported: {mime or '<empty>'}")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("B2Bank image_base64 data URL is not valid base64") from exc
    if not raw:
        raise ValueError("B2Bank image_base64 payload is empty")
    if len(raw) > _MAX_B2BANK_IMAGE_BYTES:
        raise ValueError(f"B2Bank image_base64 payload is too large ({len(raw)} bytes)")
    return raw, mime


def _image_filename(mime: str) -> str:
    return f"image-{uuid.uuid4().hex}.{_IMAGE_EXTENSIONS[mime]}"


def _parse_tool_arguments(raw_arguments: Any) -> dict[str, Any]:
    if isinstance(raw_arguments, dict):
        return dict(raw_arguments)
    if isinstance(raw_arguments, str):
        text = raw_arguments.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"raw": raw_arguments}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    if raw_arguments is None:
        return {}
    return {"value": raw_arguments}


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if isinstance(block, dict):
                block_type = str(block.get("type") or "").strip()
                if block_type == "image_url":
                    continue
                if block_type in {"text", "input_text", "output_text"} or block.get("text") is not None:
                    parts.append(str(block.get("text") or ""))
                    continue
            parts.append(stringify_content(block))
        return "".join(part for part in parts if part)
    if isinstance(content, dict):
        block_type = str(content.get("type") or "").strip()
        if block_type == "image_url":
            return ""
        if block_type in {"text", "input_text", "output_text"} or content.get("text") is not None:
            return str(content.get("text") or "")
    return stringify_content(content)


def _function_result_content(content: Any) -> str:
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, list):
        return _function_result_content(_content_text(content))
    if isinstance(content, str):
        text = content.strip()
        if not text:
            return "{}"
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return json.dumps({"result": content}, ensure_ascii=False)
        if isinstance(parsed, dict):
            return text
        return json.dumps({"result": parsed}, ensure_ascii=False)
    text = _content_text(content)
    if not text:
        return "{}"
    return json.dumps({"result": text}, ensure_ascii=False)


def _history_call_names(messages: list[dict[str, Any]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for message in messages:
        for tool_call in message.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            call_id = str(tool_call.get("id") or "").strip()
            function = tool_call.get("function") or {}
            name = str(function.get("name") or "").strip() if isinstance(function, dict) else ""
            if call_id and name:
                names[call_id] = name
    return names


def _tool_result_name(message: dict[str, Any], call_names: dict[str, str]) -> str:
    explicit_name = str(message.get("name") or message.get("tool_name") or "").strip()
    if explicit_name:
        return explicit_name
    call_id = str(message.get("tool_call_id") or "").strip()
    if call_id and call_id in call_names:
        return call_names[call_id]
    raise B2BankProtocolError(f"B2Bank tool result references unknown tool call {call_id!r}")


def _system_parts(messages: list[dict[str, Any]]) -> list[str]:
    return [
        text
        for message in messages
        if str(message.get("role") or "").strip().lower() == "system"
        and (text := _content_text(message.get("content")))
    ]


def _attachments(message: dict[str, Any]) -> list[str]:
    raw = message.get("attachments")
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item or "").strip()]


def build_messages_v1(
    messages: list[dict[str, Any]],
    tool_names: ToolNameMap,
) -> list[dict[str, Any]]:
    payload_messages: list[dict[str, Any]] = []
    call_names = _history_call_names(messages)

    for message in messages:
        role = str(message.get("role") or "").strip().lower()
        if role == "system" or role not in {"user", "assistant", "tool"}:
            continue
        if role == "assistant":
            payload_message: dict[str, Any] = {"role": "assistant", "content": _content_text(message.get("content"))}
            tool_calls = message.get("tool_calls") or []
            if len(tool_calls) > 1:
                raise B2BankProtocolError("B2Bank v1 cannot represent multiple assistant tool calls")
            if tool_calls:
                function = (tool_calls[0] or {}).get("function") or {}
                runtime_name = str(function.get("name") or "").strip()
                if not runtime_name:
                    raise B2BankProtocolError("B2Bank assistant tool call has no function name")
                payload_message["function_call"] = {
                    "name": tool_names.provider_name(runtime_name),
                    "arguments": _parse_tool_arguments(function.get("arguments")),
                }
            tools_state_id = message.get("tools_state_id")
            if tools_state_id:
                payload_message["functions_state_id"] = str(tools_state_id)
            payload_messages.append(payload_message)
            continue
        if role == "tool":
            runtime_name = _tool_result_name(message, call_names)
            payload_messages.append(
                {
                    "role": "function",
                    "name": tool_names.provider_name(runtime_name),
                    "content": _function_result_content(message.get("content")),
                }
            )
            continue

        payload_message = {"role": "user", "content": _content_text(message.get("content"))}
        attachments = _attachments(message)
        if attachments:
            payload_message["attachments"] = attachments
        payload_messages.append(payload_message)

    system_parts = _system_parts(messages)
    if system_parts:
        payload_messages.insert(0, {"role": "system", "content": "\n\n".join(system_parts)})
    return payload_messages


def _v2_content(text: str, attachments: list[str]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    if text:
        content.append({"text": text})
    if attachments:
        content.append({"files": [{"id": file_id} for file_id in attachments]})
    return content


def build_messages_v2(
    messages: list[dict[str, Any]],
    tool_names: ToolNameMap,
) -> list[dict[str, Any]]:
    payload_messages: list[dict[str, Any]] = []
    call_names = _history_call_names(messages)

    for message in messages:
        role = str(message.get("role") or "").strip().lower()
        if role == "system" or role not in {"user", "assistant", "tool", "reasoning"}:
            continue
        if role == "reasoning":
            payload_messages.append(
                {
                    "role": "reasoning",
                    "content": _v2_content(_content_text(message.get("content")), []),
                }
            )
            continue
        if role == "assistant":
            reasoning_content = _content_text(message.get("reasoning_content"))
            if reasoning_content:
                payload_messages.append(
                    {
                        "role": "reasoning",
                        "content": [{"text": reasoning_content}],
                    }
                )
            content = _v2_content(_content_text(message.get("content")), [])
            for tool_call in message.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    raise B2BankProtocolError("B2Bank assistant tool call must be an object")
                function = tool_call.get("function") or {}
                runtime_name = str(function.get("name") or "").strip()
                if not runtime_name:
                    raise B2BankProtocolError("B2Bank assistant tool call has no function name")
                content.append(
                    {
                        "function_call": {
                            "name": tool_names.provider_name(runtime_name),
                            "arguments": _parse_tool_arguments(function.get("arguments")),
                        }
                    }
                )
            payload_message: dict[str, Any] = {"role": "assistant", "content": content}
            if message.get("tools_state_id"):
                payload_message["tools_state_id"] = str(message.get("tools_state_id"))
            payload_messages.append(payload_message)
            continue
        if role == "tool":
            runtime_name = _tool_result_name(message, call_names)
            payload_messages.append(
                {
                    "role": "tool",
                    "content": [
                        {
                            "function_result": {
                                "name": tool_names.provider_name(runtime_name),
                                "result": _function_result_content(message.get("content")),
                            }
                        }
                    ],
                }
            )
            continue

        payload_messages.append(
            {
                "role": "user",
                "content": _v2_content(_content_text(message.get("content")), _attachments(message)),
            }
        )

    system_parts = _system_parts(messages)
    if system_parts:
        payload_messages.insert(
            0,
            {
                "role": "system",
                "content": [{"text": "\n\n".join(system_parts)}],
            },
        )
    return payload_messages


def build_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Backward-compatible v1 message builder."""
    return build_messages_v1(messages, build_tool_name_map(None, messages))


def _aliased_tools(
    tools: list[dict[str, Any]] | None,
    tool_names: ToolNameMap,
) -> list[dict[str, Any]]:
    aliased: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") or {}
        if not isinstance(function, dict):
            continue
        runtime_name = str(function.get("name") or "").strip()
        if not runtime_name:
            continue
        tool_copy = dict(tool)
        function_copy = dict(function)
        function_copy["name"] = tool_names.provider_name(runtime_name)
        tool_copy["function"] = function_copy
        aliased.append(tool_copy)
    schemas = [
        ToolSchema(
            name=str((tool.get("function") or {}).get("name") or ""),
            description=(tool.get("function") or {}).get("description") or "",
            parameters=(tool.get("function") or {}).get("parameters") or {},
        )
        for tool in aliased
    ]
    return [schema.to_dict() for schema in sanitize_chat_completion_tools(schemas)]


def _effective_model_options(
    target: RemoteTarget | dict[str, Any],
    max_tokens: int,
    temperature: float | None,
) -> tuple[int, float | None, float | None]:
    effective_max_tokens = int(target.get("default_max_tokens") or 0) if max_tokens == 65536 else max_tokens
    if effective_max_tokens <= 0:
        effective_max_tokens = max_tokens
    effective_temperature = temperature
    if effective_temperature is None:
        effective_temperature = target.get("default_temperature")
    return effective_max_tokens, effective_temperature, target.get("default_top_p")


def _function_specifications(
    sanitized_tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    specifications: list[dict[str, Any]] = []
    for tool in sanitized_tools:
        function = tool.get("function") or {}
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        specification: dict[str, Any] = {
            "name": name,
            "description": stringify_tool_description(function.get("description")),
            "parameters": parameters_schema(function.get("parameters")),
        }
        if function.get("few_shot_examples"):
            specification["few_shot_examples"] = function.get("few_shot_examples")
        if function.get("return_parameters"):
            specification["return_parameters"] = function.get("return_parameters")
        specifications.append(specification)
    return specifications


def _forced_runtime_name(tool_choice: Any, tools: list[dict[str, Any]]) -> str:
    explicit = _tool_choice_name(tool_choice)
    if explicit:
        return explicit
    if isinstance(tool_choice, str) and tool_choice in {"required", "any"} and len(tools) == 1:
        function = tools[0].get("function") or {}
        return str(function.get("name") or "").strip()
    return ""


def build_tool_config_v1(
    tool_choice: Any,
    tools: list[dict[str, Any]],
    tool_names: ToolNameMap,
) -> str | dict[str, Any] | None:
    if not tools:
        return None
    if not tool_choice or tool_choice == "auto":
        return "auto"
    if tool_choice == "none":
        return "none"
    forced = _forced_runtime_name(tool_choice, tools)
    if forced:
        return {"name": tool_names.provider_name(forced)}
    return "auto"


def build_tool_config_v2(
    tool_choice: Any,
    tools: list[dict[str, Any]],
    tool_names: ToolNameMap,
) -> dict[str, str] | None:
    if not tools:
        return None
    if tool_choice == "none":
        return {"mode": "none"}
    forced = _forced_runtime_name(tool_choice, tools)
    if forced:
        return {"mode": "forced", "function_name": tool_names.provider_name(forced)}
    return {"mode": "auto"}


def build_tool_config(tool_choice: Any, tools: list[ToolSchema] | list[dict[str, Any]]) -> str | dict[str, Any] | None:
    tools = _as_tool_dicts(tools) or []
    names = build_tool_name_map(tools, [], tool_choice)
    return build_tool_config_v1(tool_choice, tools, names)


def build_payload_v1(
    *,
    target: RemoteTarget | dict[str, Any],
    messages: list[dict[str, Any]],
    max_tokens: int,
    tool_choice: Any,
    temperature: float | None,
    tools: list[dict[str, Any]] | None,
    reasoning_effort: str = "none",
    tool_names: ToolNameMap | None = None,
) -> dict[str, Any]:
    names = tool_names or build_tool_name_map(tools, messages, tool_choice)
    effective_max_tokens, effective_temperature, effective_top_p = _effective_model_options(
        target, max_tokens, temperature
    )
    payload: dict[str, Any] = {
        "model": str(target.get("resolved_model") or ""),
        "messages": build_messages_v1(messages, names),
        "max_tokens": effective_max_tokens,
    }
    if effective_temperature is not None:
        payload["temperature"] = effective_temperature
    if effective_top_p is not None:
        payload["top_p"] = effective_top_p
    # v1 takes the effort at the payload root; "none" keeps reasoning off the wire.
    effort = wire_reasoning_effort(reasoning_effort)
    if effort != "none":
        payload["reasoning_effort"] = effort

    sanitized_tools = _aliased_tools(tools, names)
    specifications = _function_specifications(sanitized_tools)
    if specifications:
        payload["functions"] = specifications
        function_call = build_tool_config_v1(tool_choice, tools or [], names)
        if function_call is not None:
            payload["function_call"] = function_call
    return payload


def build_payload_v2(
    *,
    target: RemoteTarget | dict[str, Any],
    messages: list[dict[str, Any]],
    reasoning_effort: str,
    max_tokens: int,
    tool_choice: Any,
    temperature: float | None,
    tools: list[dict[str, Any]] | None,
    tool_names: ToolNameMap | None = None,
) -> dict[str, Any]:
    names = tool_names or build_tool_name_map(tools, messages, tool_choice)
    effective_max_tokens, effective_temperature, effective_top_p = _effective_model_options(
        target, max_tokens, temperature
    )
    model_options: dict[str, Any] = {"max_tokens": effective_max_tokens}
    if effective_temperature is not None:
        model_options["temperature"] = effective_temperature
    if effective_top_p is not None:
        model_options["top_p"] = effective_top_p
    effort = wire_reasoning_effort(reasoning_effort)
    if effort != "none":
        model_options["reasoning"] = {"effort": effort}
    if target.get("parallel_tool_calls"):
        model_options["parallel_tool_calls"] = True

    payload: dict[str, Any] = {
        "model": str(target.get("resolved_model") or ""),
        "messages": build_messages_v2(messages, names),
        "model_options": model_options,
    }
    sanitized_tools = _aliased_tools(tools, names)
    specifications = _function_specifications(sanitized_tools)
    if specifications:
        payload["tools"] = [{"functions": {"specifications": specifications}}]
        tool_config = build_tool_config_v2(tool_choice, tools or [], names)
        if tool_config is not None:
            payload["tool_config"] = tool_config
    return payload


def build_payload(
    *,
    target: RemoteTarget | dict[str, Any],
    messages: list[dict[str, Any]],
    max_tokens: int,
    tool_choice: Any,
    temperature: float | None,
    tools: list[ToolSchema] | list[dict[str, Any]] | None,
    reasoning_effort: str = "none",
    tool_names: ToolNameMap | None = None,
) -> dict[str, Any]:
    tools = _as_tool_dicts(tools)
    if _chat_api_version(target.get("chat_api_version") or "v1") == "v2":
        return build_payload_v2(
            target=target,
            messages=messages,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
            temperature=temperature,
            tools=tools,
            tool_names=tool_names,
        )
    return build_payload_v1(
        target=target,
        messages=messages,
        max_tokens=max_tokens,
        tool_choice=tool_choice,
        temperature=temperature,
        tools=tools,
        reasoning_effort=reasoning_effort,
        tool_names=tool_names,
    )


def _normalize_usage(
    raw_usage: Any,
    target: RemoteTarget | dict[str, Any],
) -> dict[str, Any]:
    usage = raw_usage if isinstance(raw_usage, dict) else {}
    input_details = usage.get("input_tokens_details")
    prompt_details = usage.get("prompt_tokens_details")
    input_details = input_details if isinstance(input_details, dict) else {}
    prompt_details = prompt_details if isinstance(prompt_details, dict) else {}

    def token_count(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return max(0, value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return 0

    prompt_tokens = token_count(usage.get("prompt_tokens") or usage.get("input_tokens"))
    completion_tokens = token_count(usage.get("completion_tokens") or usage.get("output_tokens"))
    raw_total_tokens = usage.get("total_tokens")
    total_tokens = prompt_tokens + completion_tokens if raw_total_tokens is None else token_count(raw_total_tokens)
    cached_tokens = token_count(
        usage.get("precached_prompt_tokens")
        or input_details.get("cached_tokens")
        or prompt_details.get("cached_tokens")
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "provider": PROVIDER_ID,
        "resolved_model": str(target.get("usage_model") or target.get("resolved_model") or ""),
    }


def _tool_call(
    function_call: dict[str, Any],
    *,
    tool_names: ToolNameMap | None,
    request_id: str,
    index: int,
) -> dict[str, Any]:
    provider_name = str(function_call.get("name") or "").strip()
    if not provider_name:
        raise B2BankProtocolError("B2Bank response function call has no name")
    runtime_name = runtime_function_name(provider_name, tool_names)
    arguments = function_call.get("arguments")
    if not isinstance(arguments, dict):
        raise B2BankProtocolError("B2Bank response function call arguments must be an object")
    return {
        "id": f"call_gb_{request_id}_{index}",
        "type": "function",
        "function": {
            "name": runtime_name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def normalize_response_v1(
    resp_dict: dict[str, Any],
    target: RemoteTarget | dict[str, Any],
    *,
    tool_names: ToolNameMap | None = None,
    request_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(resp_dict, dict):
        raise B2BankProtocolError("B2Bank v1 response must be an object")
    choices = resp_dict.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise B2BankProtocolError("B2Bank v1 response must contain a non-empty choices array")
    choice = choices[0]
    response_message = choice.get("message")
    if not isinstance(response_message, dict):
        raise B2BankProtocolError("B2Bank v1 choice must contain a message object")

    content = response_message.get("content")
    if content is not None and not isinstance(content, str):
        raise B2BankProtocolError("B2Bank v1 assistant content must be a string")
    message: dict[str, Any] = {
        "role": str(response_message.get("role") or "assistant"),
        "content": content or "",
        "finish_reason": choice.get("finish_reason"),
    }
    state_id = response_message.get("functions_state_id") or response_message.get("tools_state_id")
    if state_id:
        message["tools_state_id"] = str(state_id)
    if response_message.get("reasoning_content"):
        message["reasoning_content"] = str(response_message.get("reasoning_content"))

    function_call = response_message.get("function_call")
    if function_call is not None:
        if not isinstance(function_call, dict):
            raise B2BankProtocolError("B2Bank v1 function_call must be an object")
        message["tool_calls"] = [
            _tool_call(
                function_call,
                tool_names=tool_names,
                request_id=request_id or uuid.uuid4().hex,
                index=0,
            )
        ]
    finish_reason = choice.get("finish_reason")
    if finish_reason == "function_call" and function_call is None:
        raise B2BankProtocolError("B2Bank v1 finish_reason=function_call has no function call")
    if function_call is not None and finish_reason not in {None, "function_call"}:
        raise B2BankProtocolError("B2Bank v1 function call has inconsistent finish_reason")
    return message, _normalize_usage(resp_dict.get("usage"), target)


def _response_text_blocks(message: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    content = message.get("content")
    if not isinstance(content, list):
        raise B2BankProtocolError("B2Bank v2 message content must be an array")
    text_parts: list[str] = []
    function_calls: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            raise B2BankProtocolError("B2Bank v2 message content block must be an object")
        if block.get("text") is not None:
            text_parts.append(str(block.get("text") or ""))
        if "function_call" in block:
            function_call = block.get("function_call")
            if not isinstance(function_call, dict):
                raise B2BankProtocolError("B2Bank v2 function_call block must be an object")
            function_calls.append(function_call)
        if "tool_execution" in block:
            raise B2BankProtocolError("B2Bank built-in tool execution is not supported")
    return text_parts, function_calls


def normalize_response_v2(
    resp_dict: dict[str, Any],
    target: RemoteTarget | dict[str, Any],
    *,
    tool_names: ToolNameMap | None = None,
    request_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(resp_dict, dict):
        raise B2BankProtocolError("B2Bank v2 response must be an object")
    finish_reason = resp_dict.get("finish_reason")
    if not isinstance(finish_reason, str) or not finish_reason:
        raise B2BankProtocolError("B2Bank v2 response has no finish_reason")
    if finish_reason == "function_call_error":
        raise B2BankProtocolError("B2Bank v2 returned function_call_error")
    messages = resp_dict.get("messages")
    if not isinstance(messages, list) or not messages:
        raise B2BankProtocolError("B2Bank v2 response must contain messages")
    assistant_text: list[str] = []
    reasoning_text: list[str] = []
    function_calls: list[dict[str, Any]] = []
    tools_state_id = ""
    assistant_seen = False
    for response_message in messages:
        if not isinstance(response_message, dict):
            raise B2BankProtocolError("B2Bank v2 response message must be an object")
        role = str(response_message.get("role") or "").strip().lower()
        text_parts, calls = _response_text_blocks(response_message)
        if role == "reasoning":
            if calls:
                raise B2BankProtocolError("B2Bank v2 function_call must be in an assistant message")
            reasoning_text.extend(text_parts)
        elif role == "assistant":
            assistant_seen = True
            assistant_text.extend(text_parts)
            function_calls.extend(calls)
            state_id = response_message.get("tools_state_id") or response_message.get("tool_state_id")
            if state_id:
                tools_state_id = str(state_id)
        elif calls:
            raise B2BankProtocolError("B2Bank v2 function_call must be in an assistant message")
    if not assistant_seen:
        raise B2BankProtocolError("B2Bank v2 response has no assistant message")
    if finish_reason == "function_call" and not function_calls:
        raise B2BankProtocolError("B2Bank v2 finish_reason=function_call has no function call")
    if function_calls and finish_reason != "function_call":
        raise B2BankProtocolError("B2Bank v2 function call has inconsistent finish_reason")

    stable_request_id = request_id or uuid.uuid4().hex
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(assistant_text),
        "finish_reason": finish_reason,
    }
    if reasoning_text:
        message["reasoning_content"] = "".join(reasoning_text)
    if tools_state_id:
        message["tools_state_id"] = tools_state_id
    if function_calls:
        message["tool_calls"] = [
            _tool_call(
                function_call,
                tool_names=tool_names,
                request_id=stable_request_id,
                index=index,
            )
            for index, function_call in enumerate(function_calls)
        ]
    return message, _normalize_usage(resp_dict.get("usage"), target)


def normalize_response(
    resp_dict: dict[str, Any],
    target: RemoteTarget | dict[str, Any],
    *,
    tool_names: ToolNameMap | None = None,
    request_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if _chat_api_version(target.get("chat_api_version") or "v1") == "v2":
        return normalize_response_v2(
            resp_dict,
            target,
            tool_names=tool_names,
            request_id=request_id,
        )
    return normalize_response_v1(
        resp_dict,
        target,
        tool_names=tool_names,
        request_id=request_id,
    )


def _prepared_images(messages: list[dict[str, Any]]) -> dict[int, list[_PreparedImage]]:
    image_blocks: dict[int, list[dict[str, Any]]] = {}
    for index, message in enumerate(messages):
        if str(message.get("role") or "").strip().lower() != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        blocks: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "").strip()
            if block_type == "image_url":
                blocks.append(block)
            elif "image" in block_type:
                raise ValueError("B2Bank image input must use image_base64 data URLs")
        if blocks:
            image_blocks[index] = blocks

    count = sum(len(blocks) for blocks in image_blocks.values())
    if count > _MAX_B2BANK_IMAGE_COUNT:
        raise ValueError(f"B2Bank request has too many images ({count}; max {_MAX_B2BANK_IMAGE_COUNT})")

    prepared: dict[int, list[_PreparedImage]] = {}
    total_bytes = 0
    for index, blocks in image_blocks.items():
        images: list[_PreparedImage] = []
        for block in blocks:
            image_url = block.get("image_url") or ""
            if isinstance(image_url, dict):
                url = str(image_url.get("url") or "").strip()
            else:
                url = str(image_url).strip()
            raw, mime = _decode_data_image_url(url)
            total_bytes += len(raw)
            if total_bytes > _MAX_B2BANK_TOTAL_IMAGE_BYTES:
                raise ValueError(f"B2Bank image_base64 payloads are too large in total ({total_bytes} bytes)")
            images.append(_PreparedImage(raw, mime, hashlib.sha256(raw).hexdigest()))
        prepared[index] = images
    return prepared


def _bounded_error_body(response: Any) -> str:
    try:
        payload = response.json()
    except Exception:
        try:
            char_count = len(str(response.text or ""))
        except Exception:
            return "<unavailable>"
        return f"<redacted non-json body; chars={char_count}>"
    if not isinstance(payload, dict):
        return f"<redacted {type(payload).__name__} body>"

    summary: dict[str, Any] = {
        "fields": [field for field in _ERROR_BODY_FIELDS if field in payload],
    }
    for field in ("status", "code", "error_code", "type", "request_id", "trace_id"):
        value = payload.get(field)
        if isinstance(value, (int, float, bool)) or (isinstance(value, str) and _SAFE_ERROR_TOKEN_RE.fullmatch(value)):
            summary[field] = value
    return json.dumps(summary, ensure_ascii=True, separators=(",", ":"))[:_MAX_ERROR_BODY_CHARS]


def _response_request_id(response: Any, fallback: str) -> str:
    try:
        headers = response.headers
        return str(headers.get("X-Request-ID") or headers.get("RqUID") or fallback)
    except Exception:
        return fallback


def _is_model_unavailable(response: Any) -> bool:
    if getattr(response, "status_code", None) != 403:
        return False
    try:
        payload = response.json()
    except Exception:
        return False
    message = payload.get("message") if isinstance(payload, dict) else None
    return isinstance(message, str) and _MODEL_UNAVAILABLE_MARKER in message.lower()


def _http_error(
    operation: str,
    response: Any,
    request_id: str,
    *,
    error_cls: type[B2BankHTTPError] = B2BankHTTPError,
    reason: str = "",
) -> B2BankHTTPError:
    status = getattr(response, "status_code", None)
    response_request_id = _response_request_id(response, request_id)
    body = _bounded_error_body(response)
    details = f"B2Bank {operation} HTTP status {status}; request_id={response_request_id}"
    if reason:
        details += f"; reason={reason}"
    if body:
        details += f"; body={body}"
    try:
        request = response.request
    except Exception:
        request = httpx.Request("POST", "https://gigachat.invalid")
    return error_cls(details, request=request, response=response)


def _response_json(response: Any, *, operation: str, request_id: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise B2BankProtocolError(
            f"B2Bank {operation} returned invalid JSON; request_id={_response_request_id(response, request_id)}"
        ) from exc
    if not isinstance(payload, dict):
        raise B2BankProtocolError(
            f"B2Bank {operation} response must be an object; request_id={_response_request_id(response, request_id)}"
        )
    return payload


def _image_upload_kwargs(
    *, headers: dict[str, str], image: _PreparedImage, timeout: httpx.Timeout | None
) -> dict[str, Any]:
    upload_headers = {key: value for key, value in headers.items() if key.lower() != "content-type"}
    request_kwargs: dict[str, Any] = {
        "headers": upload_headers,
        "data": {"purpose": "general"},
        "files": {"file": (_image_filename(image.mime), image.data, image.mime)},
    }
    if timeout is not None:
        request_kwargs["timeout"] = timeout
    return request_kwargs


def _uploaded_file_id(response: httpx.Response, request_id: str) -> str:
    try:
        payload = _response_json(response, operation="image upload", request_id=request_id)
        file_id = str(payload.get("id") or "").strip()
        if not file_id:
            raise B2BankProtocolError(f"B2Bank image upload response has no file id; request_id={request_id}")
    except B2BankProtocolError as exc:
        raise ProviderResponseError(str(exc)) from exc
    return file_id


def _upload_b2bank_image(
    *,
    client: httpx.Client,
    target: RemoteTarget | dict[str, Any],
    headers: dict[str, str],
    image: _PreparedImage,
    timeout: httpx.Timeout | None,
    request_id: str,
) -> str:
    request_kwargs = _image_upload_kwargs(headers=headers, image=image, timeout=timeout)
    try:
        response = client.post(_files_url(str(target.get("base_url") or "")), **request_kwargs)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise _http_error("image upload", exc.response, request_id) from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"B2Bank image upload failed; request_id={request_id}; error={type(exc).__name__}") from exc
    return _uploaded_file_id(response, request_id)


def _messages_with_b2bank_attachments(
    *,
    client: httpx.Client,
    target: RemoteTarget | dict[str, Any],
    messages: list[dict[str, Any]],
    headers: dict[str, str],
    timeout: httpx.Timeout | None,
    request_id: str,
) -> list[dict[str, Any]]:
    prepared = _prepared_images(messages)
    if not prepared:
        return messages

    uploaded: dict[str, str] = {}
    for message_images in prepared.values():
        for image in message_images:
            if image.digest not in uploaded:
                uploaded[image.digest] = _upload_b2bank_image(
                    client=client,
                    target=target,
                    headers=headers,
                    image=image,
                    timeout=timeout,
                    request_id=request_id,
                )

    return _messages_with_uploaded_attachments(messages, prepared, uploaded)


def _messages_with_uploaded_attachments(
    messages: list[dict[str, Any]],
    prepared: dict[int, list[_PreparedImage]],
    uploaded: dict[str, str],
) -> list[dict[str, Any]]:
    transformed: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        images = prepared.get(index)
        if not images:
            transformed.append(message)
            continue
        text = _content_text(message.get("content"))
        existing_attachments = _attachments(message)
        base_message = {key: value for key, value in message.items() if key not in {"content", "attachments"}}
        for image in images:
            transformed.append(
                {
                    **base_message,
                    "content": text,
                    "attachments": [*existing_attachments, uploaded[image.digest]],
                }
            )
    return transformed


async def _async_upload_b2bank_image(
    *,
    client: httpx.AsyncClient,
    target: RemoteTarget | dict[str, Any],
    headers: dict[str, str],
    image: _PreparedImage,
    timeout: httpx.Timeout | None,
    request_id: str,
) -> str:
    request_kwargs = _image_upload_kwargs(headers=headers, image=image, timeout=timeout)
    try:
        response = await client.post(_files_url(str(target.get("base_url") or "")), **request_kwargs)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise _http_error("image upload", exc.response, request_id) from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"B2Bank image upload failed; request_id={request_id}; error={type(exc).__name__}") from exc
    return _uploaded_file_id(response, request_id)


async def _async_messages_with_b2bank_attachments(
    *,
    client: httpx.AsyncClient,
    target: RemoteTarget | dict[str, Any],
    messages: list[dict[str, Any]],
    headers: dict[str, str],
    timeout: httpx.Timeout | None,
    request_id: str,
) -> list[dict[str, Any]]:
    prepared = _prepared_images(messages)
    if not prepared:
        return messages
    uploaded: dict[str, str] = {}
    for message_images in prepared.values():
        for image in message_images:
            if image.digest not in uploaded:
                uploaded[image.digest] = await _async_upload_b2bank_image(
                    client=client,
                    target=target,
                    headers=headers,
                    image=image,
                    timeout=timeout,
                    request_id=request_id,
                )
    return _messages_with_uploaded_attachments(messages, prepared, uploaded)


def _split_read_budget() -> Any:
    """Half the read budget for each attempt, so a drawn request keeps the usual envelope.

    Both the probe and the fallback are capped, not just the probe: the caller waits the
    configured budget either way. A preview answer slower than its half is a failure of
    the version under test, not a reason to make the caller wait twice as long.
    """
    return remote_http_timeout(read_timeout_sec=max(1.0, configured_read_timeout_sec() / 2))


@dataclass(frozen=True)
class _ChatSetup:
    """Everything one chat call decides before it touches the network."""

    version: str
    versioned_target: dict[str, Any]
    request_id: str
    session_id: str
    headers: dict[str, str]
    timeout: httpx.Timeout | None
    tool_dicts: list[dict[str, Any]] | None
    installed_model: str
    chat_url: str


@dataclass(frozen=True)
class _ChatRequest:
    setup: _ChatSetup
    payload: dict[str, Any]
    tool_names: ToolNameMap
    request_kwargs: dict[str, Any]


@dataclass(frozen=True)
class _ChatAttempt:
    model: str
    request_id: str
    timeout: httpx.Timeout | None = None


@dataclass
class _ChatOutcome:
    message: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    error: BaseException | None = None
    #: What the gateway billed for this attempt, readable even when the body failed to normalize.
    billed_usage: dict[str, Any] = dataclass_field(default_factory=dict)

    def settled(self) -> tuple[dict[str, Any], dict[str, Any]]:
        if self.error is not None:
            raise self.error
        if self.message is None or self.usage is None:
            raise ProviderResponseError("a chat attempt settled with neither an answer nor an error")
        return self.message, self.usage


def _chat_setup(
    target: RemoteTarget | dict[str, Any],
    *,
    tools: list[ToolSchema] | None,
    read_timeout_sec: float | None,
    phase_cap_sec: float | None,
) -> _ChatSetup:
    tool_dicts = _as_tool_dicts(tools)
    try:
        version = _chat_api_version(target.get("chat_api_version") or "v1")
    except ValueError as exc:
        raise ProviderRequestError(str(exc)) from exc
    request_id = uuid.uuid4().hex
    session_id = uuid.uuid4().hex
    headers = {
        "Content-Type": "application/json",
        "X-Request-ID": request_id,
        "X-Session-ID": session_id,
    }
    client_id = str(target.get("client_id") or "").strip()
    if client_id:
        headers["X-Client-ID"] = client_id
    return _ChatSetup(
        version=version,
        # Plain dict, not RemoteTarget: ``target`` may already be one, and ``**``
        # of an untyped mapping into a TypedDict literal is not expressible.
        versioned_target={**target, "chat_api_version": version},
        request_id=request_id,
        session_id=session_id,
        headers=aef_tracing.with_agent_header(aef_tracing.with_trace_header(headers)),
        timeout=(
            remote_http_timeout(read_timeout_sec, phase_cap_sec)
            if read_timeout_sec is not None or phase_cap_sec is not None
            else None
        ),
        tool_dicts=tool_dicts,
        installed_model=str(target.get("resolved_model") or "").strip(),
        chat_url=_chat_url(str(target.get("base_url") or ""), version),
    )


def _chat_request(
    setup: _ChatSetup,
    payload_messages: list[dict[str, Any]],
    *,
    tool_names: ToolNameMap,
    reasoning_effort: str,
    max_tokens: int,
    tool_choice: Any,
    temperature: float | None,
) -> _ChatRequest:
    payload = build_payload(
        target=setup.versioned_target,
        messages=payload_messages,
        reasoning_effort=reasoning_effort,
        max_tokens=max_tokens,
        tool_choice=tool_choice,
        temperature=temperature,
        tools=setup.tool_dicts,
        tool_names=tool_names,
    )
    request_kwargs: dict[str, Any] = {"headers": setup.headers, "json": payload}
    if setup.timeout is not None:
        request_kwargs["timeout"] = setup.timeout
    return _ChatRequest(setup=setup, payload=payload, tool_names=tool_names, request_kwargs=request_kwargs)


def _attempt_kwargs(request: _ChatRequest, attempt: _ChatAttempt) -> dict[str, Any]:
    # One id per attempt: the two requests are distinct to the gateway and to the
    # log lines TN09 reads, while the shared session id still ties them together.
    kwargs = {**request.request_kwargs, "headers": {**request.setup.headers, "X-Request-ID": attempt.request_id}}
    if attempt.timeout is not None:
        kwargs["timeout"] = attempt.timeout
    request.payload["model"] = attempt.model
    return kwargs


def _status_failure(exc: httpx.HTTPStatusError, *, request: _ChatRequest, attempt: _ChatAttempt) -> BaseException:
    response = exc.response
    setup = request.setup
    if _is_model_unavailable(response):
        log.warning(
            "LLM call provider=gigachat-b2bank model=%s status=model_unavailable http_status=403 "
            "reason=%s path=%s request_id=%s session_id=%s",
            attempt.model,
            _MODEL_UNAVAILABLE_REASON,
            setup.chat_url,
            attempt.request_id,
            setup.session_id,
        )
        failure: BaseException = _http_error(
            "chat",
            response,
            attempt.request_id,
            error_cls=B2BankModelUnavailableError,
            reason=_MODEL_UNAVAILABLE_REASON,
        )
    else:
        log.warning(
            "LLM call provider=gigachat-b2bank model=%s status=http_error http_status=%s request_id=%s session_id=%s",
            attempt.model,
            getattr(response, "status_code", None),
            attempt.request_id,
            setup.session_id,
        )
        failure = _http_error("chat", response, attempt.request_id)
    failure.__cause__ = exc
    return failure


def _transport_failure(exc: httpx.HTTPError, *, request: _ChatRequest, attempt: _ChatAttempt) -> BaseException:
    log.warning(
        "LLM call provider=gigachat-b2bank model=%s status=error error_type=%s request_id=%s session_id=%s",
        attempt.model,
        type(exc).__name__,
        attempt.request_id,
        request.setup.session_id,
    )
    return exc


def _read_attempt(
    response: httpx.Response, *, request: _ChatRequest, attempt: _ChatAttempt, started: float
) -> _ChatOutcome:
    setup = request.setup
    billed: dict[str, Any] = {}
    try:
        response_payload = _response_json(response, operation="chat", request_id=attempt.request_id)
        # Recorded before normalization: a body the gateway already billed for still
        # costs money when it turns out to violate the wire contract.
        billed = _normalize_usage(response_payload.get("usage"), setup.versioned_target)
        message, usage = normalize_response(
            response_payload,
            setup.versioned_target,
            tool_names=request.tool_names,
            request_id=attempt.request_id,
        )
    except B2BankProtocolError as exc:
        log.warning(
            "LLM call provider=gigachat-b2bank model=%s status=protocol_error http_status=%s request_id=%s session_id=%s",
            attempt.model,
            getattr(response, "status_code", None),
            attempt.request_id,
            setup.session_id,
            exc_info=True,
        )
        failure = ProviderResponseError(str(exc))
        failure.__cause__ = exc
        return _ChatOutcome(error=failure, billed_usage=billed)
    # TN09 reads p95 from this line; the caller measures no per-request latency.
    log.info(
        "LLM call provider=gigachat-b2bank model=%s status=ok http_status=%s duration_ms=%s request_id=%s session_id=%s",
        attempt.model,
        getattr(response, "status_code", None),
        int((time.monotonic() - started) * 1000),
        attempt.request_id,
        setup.session_id,
    )
    return _ChatOutcome(message=message, usage=usage, billed_usage=billed)


def _chat_route(
    setup: _ChatSetup,
) -> Generator[_ChatAttempt, _ChatOutcome, tuple[dict[str, Any], dict[str, Any]]]:
    """The preview-canary decision: which version is asked, in what order, and who pays.

    Declared once and driven by both transports, so the version under test is charged the
    same way whether the call was awaited or not.
    """
    installed_model = setup.installed_model
    request_model = select_request_model(installed_model, bounded=setup.timeout is not None)
    if request_model == installed_model:
        outcome = yield _ChatAttempt(installed_model, setup.request_id)
        return outcome.settled()

    preview_request_id = uuid.uuid4().hex
    half_budget = _split_read_budget()
    outcome = yield _ChatAttempt(request_model, preview_request_id, half_budget)
    if outcome.error is not None:
        # Includes B2BankModelUnavailableError: a preview build the platform disabled is
        # exactly what the check exists to catch, so it falls back rather than stopping
        # the task the way TN08 stops it for the installed version.
        if not isinstance(outcome.error, (httpx.HTTPError, ProviderResponseError)):
            raise outcome.error
        preview_failure = type(outcome.error).__name__
        wasted_usage = dict(outcome.billed_usage)
    else:
        message, usage = outcome.settled()
        if not is_empty_answer(message.get("content"), message.get("tool_calls")):
            # The answering version travels in the usage record; events derive it from there.
            usage["resolved_model"] = usage_model_name(PROVIDER_ID, request_model)
            return message, usage
        preview_failure = "empty_response"
        wasted_usage = usage

    message, usage = (yield _ChatAttempt(installed_model, setup.request_id, half_budget)).settled()
    # The discarded attempt burned real tokens; the report must not hide them.
    add_usage(usage, wasted_usage)
    # Charged to the version only once the installed one has answered for real: if both
    # fail, the fault is the request or the provider, not the version under test.
    if not is_empty_answer(message.get("content"), message.get("tool_calls")):
        record_preview_failure(request_model, request_id=preview_request_id, reason=preview_failure)
    return message, usage


class GigachatB2BankProvider:
    """Adapter for GigaChat B2Bank v1/v2 non-stream REST APIs."""

    provider_id = PROVIDER_ID

    def __init__(
        self,
        client_factory: Callable[..., httpx.Client] | None = None,
        *,
        async_client_factory: Callable[..., httpx.AsyncClient] | None = None,
    ) -> None:
        self._clients: dict[tuple[bool, str, str, str, bool], httpx.Client] = {}
        self._clients_lock = threading.Lock()
        self._client_factory = client_factory or httpx.Client
        self._async_client_factory = async_client_factory or httpx.AsyncClient

    def supports(self, ref: ModelRef) -> bool:
        return ref.provider == PROVIDER_ID

    def resolve_target(self, ref: ModelRef) -> RemoteTarget:
        from ouroboros.storage.settings.store import load_settings

        settings = load_settings()
        verify_ssl = env_truthy(settings.GIGACHAT_B2BANK_VERIFY_SSL, True)
        return RemoteTarget(
            provider=PROVIDER_ID,
            resolved_model=ref.model_id,
            usage_model=ref.usage_model,
            api_key="",
            base_url=(settings.GIGACHAT_B2BANK_BASE_URL or "").strip(),
            chat_api_version=_chat_api_version(settings.GIGACHAT_B2BANK_CHAT_API_VERSION),
            client_cert_path=(settings.GIGACHAT_B2BANK_CERT_PATH.get_secret_value() or "").strip(),
            client_key_path=(settings.GIGACHAT_B2BANK_KEY_PATH.get_secret_value() or "").strip(),
            ca_path=(settings.GIGACHAT_B2BANK_CA_PATH.get_secret_value() or "").strip(),
            verify_ssl=verify_ssl,
            client_id=(settings.GIGACHAT_B2BANK_CLIENT_ID or "").strip(),
            default_temperature=env_positive_float(settings.GIGACHAT_B2BANK_TEMPERATURE),
            default_top_p=env_positive_float(settings.GIGACHAT_B2BANK_TOP_P),
            default_max_tokens=env_positive_int(settings.GIGACHAT_B2BANK_MAX_TOKENS),
            parallel_tool_calls=env_truthy(settings.GIGACHAT_B2BANK_PARALLEL_TOOL_CALLS, False),
            default_headers={},
        )

    def _get_client(
        self,
        target: RemoteTarget | dict[str, Any],
        *,
        no_proxy: bool,
    ) -> httpx.Client:
        kwargs = self._client_kwargs(target, no_proxy=no_proxy)
        cache_key = (
            bool(target.get("verify_ssl", True)),
            str(target.get("ca_path") or "").strip(),
            str(target.get("client_cert_path") or "").strip(),
            str(target.get("client_key_path") or "").strip(),
            not no_proxy,
        )
        with self._clients_lock:
            client = self._clients.get(cache_key)
            if client is None:
                client = self._client_factory(**kwargs)
                self._clients[cache_key] = client
            return client

    def _client_kwargs(self, target: RemoteTarget | dict[str, Any], *, no_proxy: bool) -> dict[str, Any]:
        verify_ssl = bool(target.get("verify_ssl", True))
        ca_path = str(target.get("ca_path") or "").strip()
        cert_path = str(target.get("client_cert_path") or "").strip()
        key_path = str(target.get("client_key_path") or "").strip()
        if bool(cert_path) != bool(key_path):
            raise ValueError("GIGACHAT_B2BANK_CERT_PATH and GIGACHAT_B2BANK_KEY_PATH must be set together")
        kwargs: dict[str, Any] = {
            "verify": False if not verify_ssl else (ca_path or True),
            "trust_env": not no_proxy,
            "timeout": remote_http_timeout(),
            "follow_redirects": False,
        }
        if cert_path and key_path:
            kwargs["cert"] = (cert_path, key_path)
        return kwargs

    def _async_client(self, target: RemoteTarget | dict[str, Any], *, no_proxy: bool) -> httpx.AsyncClient:
        """One asynchronous client per call, closed by the call.

        Not cached beside the synchronous ones: this adapter is a process global reached from
        every root, and an ``httpx.AsyncClient`` bound to one event loop and reused from
        another fails the way a pooled asynchronous database connection does.
        """
        return self._async_client_factory(**self._client_kwargs(target, no_proxy=no_proxy))

    def close(self) -> None:
        with self._clients_lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            try:
                client.close()
            except Exception:
                log.warning("Failed to close B2Bank HTTP client", exc_info=True)

    def chat(
        self,
        target: RemoteTarget | dict[str, Any],
        messages: list[dict[str, Any]],
        *,
        tools: list[ToolSchema] | None,
        reasoning_effort: str,
        max_tokens: int,
        tool_choice: Any,
        temperature: float | None,
        no_proxy: bool,
        read_timeout_sec: float | None = None,
        phase_cap_sec: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        setup = _chat_setup(target, tools=tools, read_timeout_sec=read_timeout_sec, phase_cap_sec=phase_cap_sec)
        client = self._get_client(target, no_proxy=no_proxy)
        # Protocol violations and validation ValueErrors while building the
        # request (tool maps, image data URLs, payload) are not provider
        # faults; upload response errors are already ProviderResponseError.
        try:
            tool_names = build_tool_name_map(setup.tool_dicts, messages, tool_choice)
            payload_messages = _messages_with_b2bank_attachments(
                client=client,
                target=target,
                messages=messages,
                headers=setup.headers,
                timeout=setup.timeout,
                request_id=setup.request_id,
            )
            request = _chat_request(
                setup,
                payload_messages,
                tool_names=tool_names,
                reasoning_effort=reasoning_effort,
                max_tokens=max_tokens,
                tool_choice=tool_choice,
                temperature=temperature,
            )
        except (B2BankProtocolError, ValueError) as exc:
            raise ProviderRequestError(str(exc)) from exc

        route = _chat_route(setup)
        outcome: _ChatOutcome | None = None
        while True:
            try:
                attempt = route.send(outcome)  # type: ignore[arg-type]
            except StopIteration as stop:
                return stop.value
            outcome = self._attempt(client, request, attempt)

    @staticmethod
    def _attempt(client: httpx.Client, request: _ChatRequest, attempt: _ChatAttempt) -> _ChatOutcome:
        kwargs = _attempt_kwargs(request, attempt)
        started = time.monotonic()
        try:
            response = client.post(request.setup.chat_url, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return _ChatOutcome(error=_status_failure(exc, request=request, attempt=attempt))
        except httpx.HTTPError as exc:
            return _ChatOutcome(error=_transport_failure(exc, request=request, attempt=attempt))
        return _read_attempt(response, request=request, attempt=attempt, started=started)

    async def async_chat(
        self,
        target: RemoteTarget | dict[str, Any],
        messages: list[dict[str, Any]],
        *,
        tools: list[ToolSchema] | None,
        reasoning_effort: str,
        max_tokens: int,
        tool_choice: Any,
        temperature: float | None,
        no_proxy: bool,
        read_timeout_sec: float | None = None,
        phase_cap_sec: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        setup = _chat_setup(target, tools=tools, read_timeout_sec=read_timeout_sec, phase_cap_sec=phase_cap_sec)
        client = self._async_client(target, no_proxy=no_proxy)
        try:
            try:
                tool_names = build_tool_name_map(setup.tool_dicts, messages, tool_choice)
                payload_messages = await _async_messages_with_b2bank_attachments(
                    client=client,
                    target=target,
                    messages=messages,
                    headers=setup.headers,
                    timeout=setup.timeout,
                    request_id=setup.request_id,
                )
                request = _chat_request(
                    setup,
                    payload_messages,
                    tool_names=tool_names,
                    reasoning_effort=reasoning_effort,
                    max_tokens=max_tokens,
                    tool_choice=tool_choice,
                    temperature=temperature,
                )
            except (B2BankProtocolError, ValueError) as exc:
                raise ProviderRequestError(str(exc)) from exc

            route = _chat_route(setup)
            outcome: _ChatOutcome | None = None
            while True:
                try:
                    attempt = route.send(outcome)  # type: ignore[arg-type]
                except StopIteration as stop:
                    return stop.value
                outcome = await self._async_attempt(client, request, attempt)
        finally:
            await client.aclose()

    @staticmethod
    async def _async_attempt(client: httpx.AsyncClient, request: _ChatRequest, attempt: _ChatAttempt) -> _ChatOutcome:
        kwargs = _attempt_kwargs(request, attempt)
        started = time.monotonic()
        try:
            response = await client.post(request.setup.chat_url, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return _ChatOutcome(error=_status_failure(exc, request=request, attempt=attempt))
        except httpx.HTTPError as exc:
            return _ChatOutcome(error=_transport_failure(exc, request=request, attempt=attempt))
        return _read_attempt(response, request=request, attempt=attempt, started=started)
