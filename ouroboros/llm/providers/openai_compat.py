"""OpenAI-compatible direct providers (Cloud.ru Foundation Models, self-hosted)."""

from __future__ import annotations

import json
import logging
from functools import partial
from typing import Any

from new_core.tools.schema import ToolSchema
from ouroboros.llm.models import ModelRef
from ouroboros.llm.provider_models import (
    CLOUDRU_FOUNDATION_MODELS_BASE_URL,
    normalize_openai_compatible_base_url,
)
from ouroboros.llm.providers.base import (
    ProviderResponseError,
    normalize_openai_compatible_response,
    normalize_system_message_order,
    remote_http_timeout,
    sanitize_chat_completion_tools,
    strip_cache_control,
    wire_reasoning_effort,
)
from ouroboros.llm.targets import RemoteTarget
from ouroboros.observability import aef_tracing

log = logging.getLogger(__name__)

OPENAI_COMPAT_PROVIDER_IDS = frozenset({"cloudru", "selfhosted"})
_NO_AUTH_OPENAI_API_KEY = "unused"


def no_proxy_timeout(read_timeout_sec: float | None = None, phase_cap_sec: float | None = None):
    import httpx

    if read_timeout_sec is not None or phase_cap_sec is not None:
        return remote_http_timeout(read_timeout_sec, phase_cap_sec)
    return httpx.Timeout(connect=30.0, read=3600.0, write=3600.0, pool=30.0)


def _configured_api_key(target: RemoteTarget | dict[str, Any]) -> str:
    return str(target.get("api_key") or "").strip()


def _openai_sdk_api_key(target: RemoteTarget | dict[str, Any]) -> str:
    api_key = _configured_api_key(target)
    if api_key:
        return api_key
    if str(target.get("provider") or "") == "selfhosted":
        return _NO_AUTH_OPENAI_API_KEY
    return ""


def strip_authorization_header(request: Any, target: RemoteTarget | dict[str, Any]) -> None:
    if str(target.get("provider") or "") == "selfhosted" and not _configured_api_key(target):
        request.headers.pop("Authorization", None)


def _prepare_request(request: Any, *, target: RemoteTarget | dict[str, Any]) -> None:
    request.headers.update(aef_tracing.with_trace_header(request.headers))
    strip_authorization_header(request, target)


def _request_event_hooks(target: RemoteTarget | dict[str, Any]) -> dict[str, list[Any]]:
    return {"request": [partial(_prepare_request, target=target)]}


def selfhosted_tls_client_kwargs(target: RemoteTarget | dict[str, Any]) -> dict[str, Any]:
    """Return httpx TLS kwargs for self-hosted targets."""
    if str(target.get("provider") or "") != "selfhosted":
        return {}
    kwargs: dict[str, Any] = {}
    verify_raw = target.get("verify_ssl")
    if verify_raw is False:
        kwargs["verify"] = False
    else:
        ca_path = str(target.get("ca_path") or "").strip()
        if ca_path:
            kwargs["verify"] = ca_path

    cert_path = str(target.get("client_cert_path") or "").strip()
    key_path = str(target.get("client_key_path") or "").strip()
    if bool(cert_path) != bool(key_path):
        raise ValueError("SELFHOSTED_LLM_CERT_PATH and SELFHOSTED_LLM_KEY_PATH must be set together")
    if cert_path and key_path:
        kwargs["cert"] = (cert_path, key_path)
    return kwargs


def build_remote_kwargs(
    target: RemoteTarget | dict[str, Any],
    messages: list[dict[str, Any]],
    max_tokens: int,
    tool_choice: str,
    temperature: float | None,
    tools: list[ToolSchema] | None,
    reasoning_effort: str = "none",
) -> dict[str, Any]:
    resolved_model = str(target.get("resolved_model") or "")
    clean_messages = normalize_system_message_order(strip_cache_control(messages))
    kwargs: dict[str, Any] = {
        "model": resolved_model,
        "messages": clean_messages,
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if tools:
        kwargs["tools"] = [t.to_dict() for t in sanitize_chat_completion_tools(tools)]
        kwargs["tool_choice"] = tool_choice
    # llm-router accepts the OpenAI-style root reasoning_effort field; send it via
    # extra_body so any openai>=1.0 SDK serializes it. Cloud.ru's contract is
    # unconfirmed, so the selfhosted lane alone opts in; "none" keeps it off the wire.
    effort = wire_reasoning_effort(reasoning_effort)
    if str(target.get("provider") or "") == "selfhosted" and effort != "none":
        kwargs["extra_body"] = {"reasoning_effort": effort}
    return kwargs


def make_no_proxy_client(
    target: RemoteTarget | dict[str, Any],
    read_timeout_sec: float | None = None,
    phase_cap_sec: float | None = None,
):
    import httpx
    from openai import OpenAI

    http_client = httpx.Client(
        trust_env=False,
        mounts={},
        timeout=no_proxy_timeout(read_timeout_sec, phase_cap_sec),
        event_hooks=_request_event_hooks(target),
        **selfhosted_tls_client_kwargs(target),
    )
    oa_client = OpenAI(
        api_key=_openai_sdk_api_key(target),
        base_url=str(target.get("base_url") or ""),
        default_headers=dict(target.get("default_headers") or {}),
        http_client=http_client,
        max_retries=0,
    )
    return oa_client, http_client


def _normalize_provider_response(response: Any, target: RemoteTarget | dict[str, Any]):
    try:
        payload = response.model_dump()
    except (AttributeError, TypeError, ValueError) as exc:
        raise ProviderResponseError("OpenAI-compatible provider returned a non-model response") from exc
    if not isinstance(payload, dict):
        raise ProviderResponseError("OpenAI-compatible provider returned a non-object response")
    try:
        # Provider payload flows through the normalizer, so these types signal
        # its shape errors; a leaked AttributeError is our own bug (INTERNAL).
        return normalize_openai_compatible_response(payload, target)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        # Background routing drops tracebacks, so log the original error here.
        log.warning(
            "OpenAI-compatible response failed to normalize (provider=%s): %s",
            str(target.get("provider") or ""),
            exc,
            exc_info=True,
        )
        raise ProviderResponseError("OpenAI-compatible provider returned a malformed response") from exc


class OpenAICompatibleProvider:
    """Adapter for Cloud.ru and self-hosted OpenAI-compatible endpoints."""

    def __init__(self, provider_id: str) -> None:
        if provider_id not in OPENAI_COMPAT_PROVIDER_IDS:
            raise ValueError(f"Invalid OpenAI-compatible provider id: {provider_id!r}")
        self.provider_id = provider_id
        self._remote_clients: dict[
            tuple[str, str, str, tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]], Any
        ] = {}

    def supports(self, ref: ModelRef) -> bool:
        return ref.provider == self.provider_id

    def close(self) -> None:
        clients = list(self._remote_clients.values())
        self._remote_clients.clear()
        for client in clients:
            try:
                client.close()
            except Exception:
                log.warning("Failed to close OpenAI-compatible client", exc_info=True)

    def resolve_target(self, ref: ModelRef) -> RemoteTarget:
        from ouroboros.storage.settings.store import load_settings

        settings = load_settings()
        if ref.provider == "cloudru":
            raw_base = (settings.CLOUDRU_FOUNDATION_MODELS_BASE_URL or "").strip() or CLOUDRU_FOUNDATION_MODELS_BASE_URL
            return RemoteTarget(
                provider="cloudru",
                resolved_model=ref.model_id,
                usage_model=ref.usage_model,
                api_key=(settings.CLOUDRU_FOUNDATION_MODELS_API_KEY.get_secret_value() or "").strip(),
                base_url=normalize_openai_compatible_base_url(raw_base),
                default_headers={},
            )
        if ref.provider == "selfhosted":
            raw_base = (settings.SELFHOSTED_LLM_BASE_URL or "").strip()
            return RemoteTarget(
                provider="selfhosted",
                resolved_model=ref.model_id,
                usage_model=ref.usage_model,
                api_key=(settings.SELFHOSTED_LLM_API_KEY.get_secret_value() or "").strip(),
                base_url=normalize_openai_compatible_base_url(raw_base),
                client_cert_path=str(settings.SELFHOSTED_LLM_CERT_PATH or "").strip(),
                client_key_path=str(settings.SELFHOSTED_LLM_KEY_PATH or "").strip(),
                ca_path=str(settings.SELFHOSTED_LLM_CA_PATH or "").strip(),
                verify_ssl=bool(settings.SELFHOSTED_LLM_VERIFY_SSL),
                default_headers={},
            )
        raise ValueError(f"Unsupported provider for OpenAI-compatible adapter: {ref.provider!r}")

    def _get_remote_client(self, target: RemoteTarget | dict[str, Any]):
        base_url = str(target.get("base_url") or "")
        api_key = _configured_api_key(target)
        headers_dict = dict(target.get("default_headers") or {})
        headers = tuple(sorted((str(k), str(v)) for k, v in headers_dict.items()))
        tls_kwargs = selfhosted_tls_client_kwargs(target)
        tls_key = tuple(sorted((str(k), repr(v)) for k, v in tls_kwargs.items()))
        cache_key = (str(target.get("provider") or ""), base_url, api_key, headers, tls_key)

        client = self._remote_clients.get(cache_key)
        if client is None:
            import httpx
            from openai import OpenAI

            kwargs: dict[str, Any] = {
                "api_key": _openai_sdk_api_key(target),
                "max_retries": 0,
                "http_client": httpx.Client(
                    timeout=remote_http_timeout(),
                    event_hooks=_request_event_hooks(target),
                    **tls_kwargs,
                ),
            }
            if base_url:
                kwargs["base_url"] = base_url
            if headers_dict:
                kwargs["default_headers"] = headers_dict
            client = OpenAI(**kwargs)
            self._remote_clients[cache_key] = client
        return client

    def chat(
        self,
        target: RemoteTarget | dict[str, Any],
        messages: list[dict[str, Any]],
        *,
        tools: list[ToolSchema] | None,
        reasoning_effort: str,
        max_tokens: int,
        tool_choice: str,
        temperature: float | None,
        no_proxy: bool,
        read_timeout_sec: float | None = None,
        phase_cap_sec: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if no_proxy:
            oa_client, http_client = make_no_proxy_client(target, read_timeout_sec, phase_cap_sec)
            try:
                kwargs = build_remote_kwargs(
                    target, messages, max_tokens, tool_choice, temperature, tools, reasoning_effort
                )
                try:
                    resp = oa_client.chat.completions.create(**kwargs)
                except json.JSONDecodeError as exc:
                    raise ProviderResponseError("OpenAI-compatible provider returned invalid JSON") from exc
                return _normalize_provider_response(resp, target)
            finally:
                http_client.close()
        client = self._get_remote_client(target)
        kwargs = build_remote_kwargs(target, messages, max_tokens, tool_choice, temperature, tools, reasoning_effort)
        request_client = (
            client.with_options(timeout=remote_http_timeout(read_timeout_sec, phase_cap_sec))
            if read_timeout_sec is not None or phase_cap_sec is not None
            else client
        )
        try:
            resp = request_client.chat.completions.create(**kwargs)
        except json.JSONDecodeError as exc:
            raise ProviderResponseError("OpenAI-compatible provider returned invalid JSON") from exc
        return _normalize_provider_response(resp, target)
