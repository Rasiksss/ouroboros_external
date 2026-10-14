"""Remote LLM target configuration passed to provider adapters."""

from __future__ import annotations

from typing import Any, TypedDict


class RemoteTarget(TypedDict, total=False):
    provider: str
    resolved_model: str
    usage_model: str
    api_key: str
    base_url: str
    default_headers: dict[str, str]
    client_cert_path: str
    client_key_path: str
    ca_path: str
    verify_ssl: bool
    chat_api_version: str
    client_id: str
    default_temperature: float | None
    default_top_p: float | None
    default_max_tokens: int | None
    parallel_tool_calls: bool


def target_get(target: RemoteTarget | dict[str, Any], key: str, default: Any = None) -> Any:
    return target.get(key, default) if isinstance(target, dict) else default
