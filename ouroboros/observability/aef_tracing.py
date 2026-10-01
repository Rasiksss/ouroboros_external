"""AEF Tracing integration facade.

The AEF SDK is optional at import time. When tracing is disabled, or the SDK is
not installed, all helpers degrade to no-op context managers.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from ouroboros.app.agent_system_configuration_kafka import (
    build_agent_system_configuration_metadata,
    parse_kafka_bootstrap_servers,
)

try:
    from aef_pyframework.patterns.logging_utils import (
        incoming_trace_id_cvar as _FRAMEWORK_TRACE_CONTEXT,
    )
except ImportError:  # Optional integration; Ouroboros tracing works without it.
    _FRAMEWORK_TRACE_CONTEXT = None

log = logging.getLogger(__name__)

_ENABLED_ENV = "AEF_TRACING_ENABLED"
_TOPIC_ENV = "AEF_TRACING_KAFKA_TOPIC"
_SDK_LIST_ENV = "AEF_TRACING_SDK_LIST"
_SDK_CONTRACT_VERSION_ENV = "AEF_TRACING_SDK_CONTRACT_VERSION"
_AUTO_INSTRUMENTATION_ENV = "AEF_TRACING_AUTO_INSTRUMENTATION_ENABLED"
_BOOTSTRAP_SERVERS_ENV = "AGENT_SYSTEM_CONFIGURATION_KAFKA_BOOTSTRAP_SERVERS"
_SDK_ENV_ALIASES = {
    "agent-id": "AGENT_ID",
    "cluster-id": "CLUSTER_ID",
    "namespace": "POD_NAMESPACE",
    "distributive": "DISTRIBUTIVE",
}
MASKED_TRACE_VALUE = "***"
TRACE_HEADER_NAME = "X-Trace-Id"
_TRACE_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")

_CONFIGURE_LOCK = threading.Lock()
_CONFIGURED_SIGNATURE: tuple[Any, ...] | None = None
_CURRENT_SESSION_ID: ContextVar[str] = ContextVar("aef_tracing_session_id", default="")
_CURRENT_TRACE_ID: ContextVar[str] = ContextVar("aef_trace_id", default="")
_DEFAULT_SDK_CONTRACT_VERSION = "1.5"


@dataclass(frozen=True)
class AEFTracingConfig:
    enabled: bool
    bootstrap_servers: list[str]
    topic: str
    agent_headers: dict[str, str]
    sdk_list: list[dict[str, str]]
    sdk_contract_version: str
    auto_instrumentation_enabled: bool


class _NoopController:
    def add_output_result(self, *args: Any, **kwargs: Any) -> None:
        return None

    def add_giga_llm_result(self, *args: Any, **kwargs: Any) -> None:
        return None

    def add_tool_result(self, *args: Any, **kwargs: Any) -> None:
        return None

    def add_response(self, *args: Any, **kwargs: Any) -> None:
        return None


class _MaskedController:
    def __init__(self, controller: Any):
        self._controller = controller

    def __getattr__(self, name: str) -> Any:
        return getattr(self._controller, name)

    def add_output_result(self, *args: Any, **kwargs: Any) -> Any:
        if "output" in kwargs:
            kwargs["output"] = mask_trace_payload(kwargs["output"])
        return self._controller.add_output_result(*args, **kwargs)

    def add_giga_llm_result(self, *args: Any, **kwargs: Any) -> Any:
        if "response_headers" in kwargs:
            kwargs["response_headers"] = mask_trace_headers(kwargs["response_headers"])
        if "response_body" in kwargs:
            kwargs["response_body"] = mask_llm_response_body(kwargs["response_body"])
        return self._controller.add_giga_llm_result(*args, **kwargs)

    def add_tool_result(self, *args: Any, **kwargs: Any) -> Any:
        if "output" in kwargs:
            kwargs["output"] = mask_trace_payload(kwargs["output"])
        return self._controller.add_tool_result(*args, **kwargs)

    def add_response(self, *args: Any, **kwargs: Any) -> Any:
        if "headers" in kwargs:
            kwargs["headers"] = mask_trace_headers(kwargs["headers"])
        if "body" in kwargs:
            kwargs["body"] = mask_trace_payload(kwargs["body"])
        return self._controller.add_response(*args, **kwargs)


def mask_trace_payload(_value: Any) -> str:
    return MASKED_TRACE_VALUE


def _valid_trace_id(value: Any) -> str:
    text = str(value or "").strip()
    return text.lower() if _TRACE_ID_RE.fullmatch(text) else ""


def ensure_trace_id(value: Any = "") -> str:
    return _valid_trace_id(value) or uuid.uuid4().hex


def current_trace_id() -> str:
    return _CURRENT_TRACE_ID.get("")


def trace_id_from_headers(headers: Mapping[str, Any] | None) -> str:
    for key, value in (headers or {}).items():
        if str(key).casefold() == TRACE_HEADER_NAME.casefold():
            return _valid_trace_id(value)
    return ""


def _set_framework_trace_id(value: str) -> None:
    if _FRAMEWORK_TRACE_CONTEXT is None:
        return
    try:
        _FRAMEWORK_TRACE_CONTEXT.set(value)
    except Exception:
        log.debug("Could not update AEF framework trace context", exc_info=True)


def set_trace_id(trace_id: Any) -> Any:
    value = ensure_trace_id(trace_id)
    token = _CURRENT_TRACE_ID.set(value)
    _set_framework_trace_id(value)
    return token


def reset_trace_id(token: Any) -> None:
    _CURRENT_TRACE_ID.reset(token)
    _set_framework_trace_id(current_trace_id())


@contextmanager
def trace_scope(trace_id: Any = "") -> Iterator[str]:
    token = set_trace_id(_valid_trace_id(trace_id) or current_trace_id())
    try:
        yield current_trace_id()
    finally:
        reset_trace_id(token)


def with_trace_header(
    headers: Mapping[str, Any] | None = None,
    trace_id: Any = "",
    *,
    generate: bool = False,
) -> dict[str, str]:
    result = {str(key): str(value) for key, value in (headers or {}).items()}
    active = _valid_trace_id(trace_id) or current_trace_id()
    if not active and generate:
        active = ensure_trace_id()
    if active:
        result[TRACE_HEADER_NAME] = active
    return result


def mask_trace_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    if not headers:
        return {}
    masked: dict[str, str] = {}
    for key, value in headers.items():
        name = str(key)
        trace_id = _valid_trace_id(value) if name.casefold() == TRACE_HEADER_NAME.casefold() else ""
        masked[name] = trace_id or MASKED_TRACE_VALUE
    return masked


def _safe_json_key(value: Any) -> str:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return MASKED_TRACE_VALUE
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return str(value)
    return MASKED_TRACE_VALUE


def make_aef_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return MASKED_TRACE_VALUE
    if isinstance(value, Mapping):
        return {_safe_json_key(key): make_aef_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [make_aef_json_safe(item) for item in value]
    return MASKED_TRACE_VALUE


def _safe_text(value: Any, default: str = "") -> str:
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, (bytes, bytearray, memoryview)):
        return default
    if isinstance(value, (bool, int, float)):
        return str(value)
    return default


def _safe_token_count(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float) and math.isfinite(value):
        return max(int(value), 0)
    if isinstance(value, str):
        try:
            return max(int(value), 0)
        except ValueError:
            return default
    return default


def _usage_token_count(usage: Mapping[str, Any], *keys: str, default: int = 0) -> int:
    for key in keys:
        if key in usage:
            return _safe_token_count(usage.get(key), default)
    return default


def _giga_role(value: Any, default: str = "user") -> str:
    role = _safe_text(value, default).lower()
    if role == "tool":
        return "function"
    if role in {"system", "user", "assistant", "function"}:
        return role
    return default


def _mask_llm_function_call(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"name": "", "arguments": {}}
    return {
        "name": _safe_text(value.get("name")),
        "arguments": {},
    }


def _first_llm_function_call(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if isinstance(item, Mapping):
            return _mask_llm_function_call(item.get("function"))
    return None


def _mask_llm_message(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"role": "user", "content": MASKED_TRACE_VALUE}
    masked: dict[str, Any] = {
        "role": _giga_role(value.get("role")),
        "content": MASKED_TRACE_VALUE,
    }
    return make_aef_json_safe(masked)


def _mask_llm_response_message(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"role": "assistant", "content": MASKED_TRACE_VALUE}
    masked: dict[str, Any] = {
        "role": _giga_role(value.get("role"), "assistant"),
        "content": MASKED_TRACE_VALUE,
    }
    if name := _safe_text(value.get("name")):
        masked["name"] = name
    function_call = None
    if "function_call" in value:
        function_call = _mask_llm_function_call(value.get("function_call"))
    elif "tool_calls" in value:
        function_call = _first_llm_function_call(value.get("tool_calls"))
    if function_call is not None:
        masked["function_call"] = function_call
    return make_aef_json_safe(masked)


def _mask_llm_messages(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_mask_llm_message(item) for item in value]


def _mask_llm_tool_definition(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return None
    function = value.get("function") if value.get("type") == "function" else value
    if not isinstance(function, Mapping):
        return None
    name = _safe_text(function.get("name"))
    if not name:
        return None
    masked: dict[str, Any] = {
        "name": name,
        "description": MASKED_TRACE_VALUE,
        "parameters": {},
    }
    return make_aef_json_safe(masked)


def _mask_llm_functions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    masked = [_mask_llm_tool_definition(item) for item in value]
    return [item for item in masked if isinstance(item, dict)]


def _mask_llm_function_call_mode(value: Any) -> str | dict[str, Any] | None:
    if isinstance(value, Mapping):
        return {}
    if isinstance(value, str) and value in {"auto", "none"}:
        return str(value)
    return None


def _safe_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def _safe_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def mask_llm_request_payload(body: Any) -> dict[str, Any]:
    source = body if isinstance(body, Mapping) else {}
    masked: dict[str, Any] = {
        "model": _safe_text(source.get("model")),
        "messages": _mask_llm_messages(source.get("messages")),
    }
    for key in ("temperature", "top_p", "repetition_penalty"):
        if key in source and (value := _safe_float(source.get(key))) is not None:
            masked[key] = value
    for key in ("max_tokens", "update_interval"):
        if key in source:
            masked[key] = _safe_token_count(source.get(key))
    for key in ("stream", "profanity_check"):
        if key in source and (value := _safe_bool(source.get(key))) is not None:
            masked[key] = value
    if source.get("reasoning_effort") in {"off", "low", "medium", "high"}:
        masked["reasoning_effort"] = str(source.get("reasoning_effort"))
    function_call = _mask_llm_function_call_mode(source.get("function_call"))
    if function_call is not None:
        masked["function_call"] = function_call
    functions = _mask_llm_functions(source.get("functions"))
    if "tools" in source:
        functions.extend(_mask_llm_functions(source.get("tools")))
    if functions:
        masked["functions"] = functions
    return make_aef_json_safe(masked)


_LLM_FINISH_REASONS = frozenset(
    {
        "stop",
        "length",
        "function_call",
        "blacklist",
        "request_blacklist",
        "request_whitelist",
        "request_filter",
        "response_blacklist",
        "error",
    }
)


def _response_finish_reason(message: Any, error: BaseException | str | None) -> str:
    if error is not None:
        return "error"
    if isinstance(message, Mapping):
        finish_reason = _safe_text(message.get("finish_reason"))
        if finish_reason in _LLM_FINISH_REASONS:
            return finish_reason
        if message.get("function_call") or message.get("tool_calls"):
            return "function_call"
    return "stop"


def mask_llm_response_payload(
    model: Any,
    message: Any,
    usage: Mapping[str, Any] | None,
    *,
    error: BaseException | str | None = None,
    finish_reason: str | None = None,
) -> dict[str, Any]:
    safe_usage_source = usage or {}
    prompt_tokens = _usage_token_count(safe_usage_source, "prompt_tokens")
    completion_tokens = _usage_token_count(safe_usage_source, "completion_tokens")
    total_tokens = _usage_token_count(
        safe_usage_source,
        "total_tokens",
        default=prompt_tokens + completion_tokens,
    )
    precached_prompt_tokens = _usage_token_count(
        safe_usage_source,
        "precached_prompt_tokens",
        "cached_tokens",
        "cache_read_tokens",
    )
    response_message = _mask_llm_response_message(message)
    final_finish_reason = (
        finish_reason if finish_reason in _LLM_FINISH_REASONS else _response_finish_reason(message, error)
    )
    resolved_model = _safe_text(safe_usage_source.get("resolved_model")) or _safe_text(model)
    masked: dict[str, Any] = {
        "choices": [
            {
                "message": response_message,
                "index": 0,
                "finish_reason": final_finish_reason,
            }
        ],
        "created": int(time.time()),
        "model": resolved_model,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "precached_prompt_tokens": precached_prompt_tokens,
        },
        "object": "chat.completion",
    }
    return make_aef_json_safe(masked)


def mask_llm_response_body(body: Any) -> dict[str, Any]:
    if not isinstance(body, Mapping):
        return mask_llm_response_payload("", {}, {})
    if "choices" in body and "usage" in body:
        choices = body.get("choices")
        first_choice = choices[0] if isinstance(choices, list) and choices else {}
        first_choice = first_choice if isinstance(first_choice, Mapping) else {}
        return mask_llm_response_payload(
            body.get("model"),
            first_choice.get("message") or {},
            body.get("usage") if isinstance(body.get("usage"), Mapping) else {},
            finish_reason=_safe_text(first_choice.get("finish_reason")),
        )
    return mask_llm_response_payload(
        body.get("model"),
        body.get("message") or {},
        body.get("usage") if isinstance(body.get("usage"), Mapping) else {},
        error=body.get("error"),
    )


def _mask_span_kwargs(kwargs: dict[str, Any], *, premasked: tuple[str, ...] = ()) -> dict[str, Any]:
    masked = dict(kwargs)
    for key in ("input", "body"):
        if key not in masked:
            continue
        # ``premasked`` keys carry a payload whose masking policy was already
        # applied by the caller (mirroring ``request_body``, which arrives
        # pre-masked by ``mask_llm_request_payload``); only JSON-safety applies.
        masked[key] = make_aef_json_safe(masked[key]) if key in premasked else mask_trace_payload(masked[key])
    if "request_body" in masked:
        masked["request_body"] = make_aef_json_safe(masked["request_body"])
    for key in ("headers", "request_headers"):
        if key in masked:
            masked[key] = mask_trace_headers(masked[key])
    return masked


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_text(env: Mapping[str, str], key: str) -> str:
    return str(env.get(key, "") or "").strip()


def _parse_sdk_list(value: str, *, contract_version: str) -> list[dict[str, str]]:
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{_SDK_LIST_ENV} must be a JSON array") from exc
    if not isinstance(parsed, list):
        raise ValueError(f"{_SDK_LIST_ENV} must be a JSON array")

    out: list[dict[str, str]] = []
    required = ("type", "provider", "version", "stack")
    for idx, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise ValueError(f"{_SDK_LIST_ENV}[{idx}] must be an object")
        entry = {key: str(item.get(key, "") or "").strip() for key in required}
        missing = [key for key in required if not entry[key]]
        if missing:
            raise ValueError(f"{_SDK_LIST_ENV}[{idx}] misses required fields: {', '.join(missing)}")
        entry["contract_version"] = str(item.get("contract_version") or contract_version).strip()
        out.append(entry)
    return out


def build_aef_tracing_config(
    *,
    env: Mapping[str, str] | None = None,
) -> AEFTracingConfig:
    if env is not None:
        source = env
    else:
        from ouroboros.storage.settings.store import load_settings

        source = load_settings().to_persist_dict()
    enabled = _truthy(_env_text(source, _ENABLED_ENV))
    if not enabled:
        return AEFTracingConfig(
            enabled=False,
            bootstrap_servers=[],
            topic="",
            agent_headers={},
            sdk_list=[],
            sdk_contract_version=_DEFAULT_SDK_CONTRACT_VERSION,
            auto_instrumentation_enabled=False,
        )

    bootstrap_servers = parse_kafka_bootstrap_servers(_env_text(source, _BOOTSTRAP_SERVERS_ENV))
    if not bootstrap_servers:
        raise ValueError(f"{_BOOTSTRAP_SERVERS_ENV} is required when {_ENABLED_ENV}=true")

    topic = _env_text(source, _TOPIC_ENV)
    if not topic:
        raise ValueError(f"{_TOPIC_ENV} is required when {_ENABLED_ENV}=true")

    sdk_contract_version = _env_text(source, _SDK_CONTRACT_VERSION_ENV) or _DEFAULT_SDK_CONTRACT_VERSION
    auto_instrumentation_enabled = _truthy(_env_text(source, _AUTO_INSTRUMENTATION_ENV))

    return AEFTracingConfig(
        enabled=True,
        bootstrap_servers=bootstrap_servers,
        topic=topic,
        agent_headers=build_agent_system_configuration_metadata(env=source),
        sdk_list=_parse_sdk_list(
            _env_text(source, _SDK_LIST_ENV),
            contract_version=sdk_contract_version,
        ),
        sdk_contract_version=sdk_contract_version,
        auto_instrumentation_enabled=auto_instrumentation_enabled,
    )


def apply_aef_sdk_env(
    config: AEFTracingConfig,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> None:
    target = environ if environ is not None else os.environ
    for header_name, env_name in _SDK_ENV_ALIASES.items():
        value = str(config.agent_headers.get(header_name, "") or "").strip()
        if value:
            target[env_name] = value


def _import_aef_sdk() -> SimpleNamespace:
    import aef_tracing  # type: ignore[import-not-found]
    from aef_tracing.exporters import AEFProtobufSenderExporter  # type: ignore[import-not-found]
    from aef_tracing.exporters.senders.kafka.sender import AEFKafkaSender, SDKEntry  # type: ignore[import-not-found]

    try:
        from aef_tracing.instrumenting import uninstrument  # type: ignore[import-not-found]
    except Exception:

        def uninstrument() -> None:
            return None

    return SimpleNamespace(
        AEFBatchSpanProcessor=aef_tracing.AEFBatchSpanProcessor,
        AEFProtobufSenderExporter=AEFProtobufSenderExporter,
        AEFKafkaSender=AEFKafkaSender,
        AEFTracerProvider=aef_tracing.AEFTracerProvider,
        SDKEntry=SDKEntry,
        aef_agent_start=aef_tracing.aef_agent_start,
        aef_input_request=aef_tracing.aef_input_request,
        aef_llm_request=aef_tracing.aef_llm_request,
        aef_output_request=aef_tracing.aef_output_request,
        aef_tool_run=aef_tracing.aef_tool_run,
        uninstrument=uninstrument,
    )


def _set_tracer_provider(provider: Any) -> None:
    try:
        from opentelemetry import trace  # type: ignore[import-not-found]

        trace.set_tracer_provider(provider)
    except Exception:
        log.debug("Could not register AEF tracer provider as OpenTelemetry global", exc_info=True)


def _config_signature(config: AEFTracingConfig) -> tuple[Any, ...]:
    return (
        tuple(config.bootstrap_servers),
        config.topic,
        tuple(sorted(config.agent_headers.items())),
        tuple(tuple(sorted(item.items())) for item in config.sdk_list),
        config.auto_instrumentation_enabled,
    )


def _sdk_entry_supported_fields(sdk_entry_cls: Any) -> set[str] | None:
    fields = getattr(sdk_entry_cls, "model_fields", None) or getattr(sdk_entry_cls, "__fields__", None)
    if isinstance(fields, Mapping) and fields:
        return {str(key) for key in fields}
    return None


def _sdk_entry_kwargs(sdk_entry_cls: Any, entry: Mapping[str, str]) -> dict[str, str]:
    fields = _sdk_entry_supported_fields(sdk_entry_cls)
    if fields is None:
        return dict(entry)
    return {key: value for key, value in entry.items() if key in fields}


def _build_sdk_entry(sdk_entry_cls: Any, entry: Mapping[str, str]) -> Any:
    kwargs = _sdk_entry_kwargs(sdk_entry_cls, entry)
    try:
        return sdk_entry_cls(**kwargs)
    except Exception:
        if "contract_version" not in kwargs:
            raise
        fallback = dict(kwargs)
        fallback.pop("contract_version", None)
        log.debug("AEF SDKEntry rejected contract_version; retrying without it", exc_info=True)
        return sdk_entry_cls(**fallback)


def configure_aef_tracing(
    *,
    env: Mapping[str, str] | None = None,
    environ: MutableMapping[str, str] | None = None,
    logger: logging.Logger | None = None,
) -> bool:
    try:
        config = build_aef_tracing_config(env=env)
    except Exception as exc:
        (logger or log).warning("AEF tracing configuration is invalid: %s", exc, exc_info=True)
        return False

    if not config.enabled:
        return False

    signature = _config_signature(config)
    global _CONFIGURED_SIGNATURE
    with _CONFIGURE_LOCK:
        if signature == _CONFIGURED_SIGNATURE:
            return True
        try:
            apply_aef_sdk_env(config, environ=environ)
            sdk = _import_aef_sdk()
            sender_kwargs: dict[str, Any] = {
                "outbox_topic": config.topic,
                "kafka_producer_config": {
                    "bootstrap_servers": config.bootstrap_servers,
                },
                "headers": config.agent_headers,
            }
            if config.sdk_list:
                sender_kwargs["sdk_list_headers"] = [_build_sdk_entry(sdk.SDKEntry, entry) for entry in config.sdk_list]
            sender = sdk.AEFKafkaSender(**sender_kwargs)
            exporter = sdk.AEFProtobufSenderExporter(senders=[sender])
            processor = sdk.AEFBatchSpanProcessor(exporter)
            provider = sdk.AEFTracerProvider()
            if not config.auto_instrumentation_enabled:
                sdk.uninstrument()
            provider.add_span_processor(processor)
            _set_tracer_provider(provider)
            _CONFIGURED_SIGNATURE = signature
            return True
        except Exception as exc:
            (logger or log).warning("AEF tracing setup failed: %s", exc, exc_info=True)
            return False


def reset_aef_tracing_for_tests() -> None:
    global _CONFIGURED_SIGNATURE
    with _CONFIGURE_LOCK:
        _CONFIGURED_SIGNATURE = None
    _CURRENT_SESSION_ID.set("")
    _CURRENT_TRACE_ID.set("")


def set_session_id(session_id: Any) -> None:
    value = str(session_id or "").strip()
    if not value:
        return
    _CURRENT_SESSION_ID.set(value)
    _apply_session_id(value)


def _apply_session_id(session_id: str) -> None:
    if not session_id:
        return
    for module_name in ("aef_tracing.span_processors", "aef_tracing.span_processor"):
        try:
            module = __import__(module_name, fromlist=["session_id_cvar"])
            module.session_id_cvar.set(session_id)
            return
        except Exception:
            continue
    log.debug("Could not set AEF tracing session id")


@contextmanager
def _span_context(factory_name: str, *, premasked: tuple[str, ...] = (), **kwargs: Any) -> Iterator[Any]:
    noop = _NoopController()
    _apply_session_id(_CURRENT_SESSION_ID.get(""))
    if not configure_aef_tracing():
        yield noop
        return
    _apply_session_id(_CURRENT_SESSION_ID.get(""))

    manager: Any = None
    try:
        factory = getattr(_import_aef_sdk(), factory_name)
        manager = factory(**_mask_span_kwargs(kwargs, premasked=premasked))
        controller = manager.__enter__()
    except Exception:
        log.warning("AEF tracing span %s failed to start", factory_name, exc_info=True)
        yield noop
        return

    exc_info = None
    try:
        yield _MaskedController(controller)
    except BaseException:
        exc_info = sys.exc_info()
        raise
    finally:
        try:
            if exc_info is None:
                manager.__exit__(None, None, None)
            else:
                manager.__exit__(*exc_info)
        except Exception:
            log.warning("AEF tracing span %s failed to finish", factory_name, exc_info=True)


def agent_start(*, input: Any) -> AbstractContextManager[Any]:
    return _span_context("aef_agent_start", input=input)


def llm_request(*, request_headers: dict[str, Any], request_body: Any) -> AbstractContextManager[Any]:
    return _span_context(
        "aef_llm_request",
        request_headers=with_trace_header(request_headers),
        request_body=mask_llm_request_payload(request_body),
    )


def tool_run(*, input: Any) -> AbstractContextManager[Any]:
    """Tool span. ``input`` must arrive with masking policy already applied:
    the only producer is ``AefToolTracer``, which sends either the hard mask or
    the sanitized view per ``AEF_TOOL_TRACE_PAYLOAD_ENABLED``."""
    return _span_context("aef_tool_run", premasked=("input",), input=input)


def input_request(
    *,
    headers: dict[str, Any],
    body: Any,
    path: str,
    method: str,
    span_name: str = "input_request",
) -> AbstractContextManager[Any]:
    return _span_context(
        "aef_input_request",
        span_name=span_name,
        headers=with_trace_header(headers),
        body=body,
        path=path,
        method=method,
    )


def output_request(
    *,
    headers: dict[str, Any],
    body: Any,
    path: str,
    method: str,
    span_name: str = "output_request",
) -> AbstractContextManager[Any]:
    return _span_context(
        "aef_output_request",
        span_name=span_name,
        headers=with_trace_header(headers),
        body=body,
        path=path,
        method=method,
    )


__all__ = [
    "TRACE_HEADER_NAME",
    "AEFTracingConfig",
    "agent_start",
    "apply_aef_sdk_env",
    "build_aef_tracing_config",
    "configure_aef_tracing",
    "current_trace_id",
    "ensure_trace_id",
    "input_request",
    "llm_request",
    "make_aef_json_safe",
    "mask_llm_request_payload",
    "mask_llm_response_payload",
    "mask_trace_headers",
    "mask_trace_payload",
    "output_request",
    "reset_aef_tracing_for_tests",
    "reset_trace_id",
    "set_session_id",
    "set_trace_id",
    "tool_run",
    "trace_id_from_headers",
    "trace_scope",
    "with_trace_header",
]
