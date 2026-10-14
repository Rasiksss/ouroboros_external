"""The asynchronous transports: their hooks are awaited, and both paths decide alike."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from ouroboros.llm.providers.gigachat_b2bank import GigachatB2BankProvider
from ouroboros.llm.providers.openai_compat import OpenAICompatibleProvider
from ouroboros.observability.aef_tracing import TRACE_HEADER_NAME, trace_scope

TRACE = "c4fd7f38-c206-4af9-bb90-8731c4534492"

_ANSWER = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _b2bank_target() -> dict:
    return {
        "provider": "gigachat-b2bank",
        "resolved_model": "GigaChat-Pro",
        "usage_model": "GigaChat-Pro",
        "base_url": "https://gigachat.local/v1",
        "chat_api_version": "v1",
        "verify_ssl": True,
        "default_headers": {},
    }


def _selfhosted_target() -> dict:
    return {
        "provider": "selfhosted",
        "resolved_model": "my-model",
        "usage_model": "my-model",
        "base_url": "https://llm.local/v1",
        "api_key": "",
        "default_headers": {},
    }


def _recording_async_client(requests: list[httpx.Request], body: dict, **kwargs) -> httpx.AsyncClient:
    """A real ``AsyncClient`` over a mock transport, so its own event hooks really run.

    The production kwargs are passed through — the event hooks among them are the point:
    an ``AsyncClient`` awaits them, so a synchronous hook fails inside the transport.
    """

    async def _handler(request: httpx.Request) -> httpx.Response:
        await request.aread()
        requests.append(request)
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(_handler), **kwargs)


# --- the tracing hook ---------------------------------------------------------


def test_an_awaited_openai_request_carries_its_trace_and_does_not_fail_in_the_transport():
    """A synchronous hook installed on an ``AsyncClient`` raises inside the transport."""
    requests: list[httpx.Request] = []
    provider = OpenAICompatibleProvider(
        "selfhosted", async_client_factory=lambda **kwargs: _recording_async_client(requests, _ANSWER, **kwargs)
    )

    with trace_scope(TRACE):
        message, usage = _run(
            provider.async_chat(
                _selfhosted_target(),
                [{"role": "user", "content": "hi"}],
                tools=None,
                reasoning_effort="low",
                max_tokens=16,
                tool_choice="auto",
                temperature=None,
                no_proxy=False,
            )
        )

    assert (message["content"], usage["resolved_model"]) == ("ok", "my-model")
    assert requests[0].headers[TRACE_HEADER_NAME] == TRACE


def test_an_awaited_b2bank_request_carries_its_trace():
    """B2Bank stamps the headers itself rather than through a hook; both paths use one helper."""
    requests: list[httpx.Request] = []
    body = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    provider = GigachatB2BankProvider(
        async_client_factory=lambda **kwargs: _recording_async_client(requests, body, **kwargs)
    )

    with trace_scope(TRACE):
        message, _usage = _run(
            provider.async_chat(
                _b2bank_target(),
                [{"role": "user", "content": "hi"}],
                tools=None,
                reasoning_effort="low",
                max_tokens=16,
                tool_choice="auto",
                temperature=None,
                no_proxy=False,
            )
        )

    assert message["content"] == "ok"
    assert requests[0].headers[TRACE_HEADER_NAME] == TRACE


def test_the_awaited_b2bank_client_is_closed_by_its_call():
    closed: list[bool] = []
    requests: list[httpx.Request] = []

    def _factory(**_kwargs):
        client = _recording_async_client(requests, _ANSWER, **_kwargs)
        original = client.aclose

        async def _aclose():
            closed.append(True)
            await original()

        client.aclose = _aclose  # type: ignore[method-assign]
        return client

    provider = GigachatB2BankProvider(async_client_factory=_factory)
    _run(
        provider.async_chat(
            _b2bank_target(),
            [{"role": "user", "content": "hi"}],
            tools=None,
            reasoning_effort="low",
            max_tokens=16,
            tool_choice="auto",
            temperature=None,
            no_proxy=False,
        )
    )

    assert closed == [True]


# --- the two paths decide alike ----------------------------------------------


def test_both_b2bank_paths_send_the_same_payload_and_report_the_same_usage():
    sync_requests: list[httpx.Request] = []
    async_requests: list[httpx.Request] = []
    body = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }

    def _sync_handler(request: httpx.Request) -> httpx.Response:
        request.read()
        sync_requests.append(request)
        return httpx.Response(200, json=body)

    call = {
        "tools": None,
        "reasoning_effort": "low",
        "max_tokens": 16,
        "tool_choice": "auto",
        "temperature": None,
        "no_proxy": False,
    }
    messages = [{"role": "user", "content": "hi"}]
    synchronous = GigachatB2BankProvider(
        client_factory=lambda **kwargs: httpx.Client(transport=httpx.MockTransport(_sync_handler), **kwargs)
    )
    asynchronous = GigachatB2BankProvider(
        async_client_factory=lambda **kwargs: _recording_async_client(async_requests, body, **kwargs)
    )

    with trace_scope(TRACE):
        sync_result = synchronous.chat(_b2bank_target(), messages, **call)
        async_result = _run(asynchronous.async_chat(_b2bank_target(), messages, **call))

    assert sync_result[0] == async_result[0]
    assert sync_result[1] == async_result[1]
    assert json.loads(sync_requests[0].content) == json.loads(async_requests[0].content)


def test_an_awaited_b2bank_http_error_raises_the_same_class_as_the_synchronous_one():
    from ouroboros.llm.providers.gigachat_b2bank import B2BankHTTPError

    async def _handler(request: httpx.Request) -> httpx.Response:
        await request.aread()
        return httpx.Response(500, json={"error": "boom"})

    provider = GigachatB2BankProvider(
        async_client_factory=lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(_handler), **kwargs)
    )

    with pytest.raises(B2BankHTTPError):
        _run(
            provider.async_chat(
                _b2bank_target(),
                [{"role": "user", "content": "hi"}],
                tools=None,
                reasoning_effort="low",
                max_tokens=16,
                tool_choice="auto",
                temperature=None,
                no_proxy=False,
            )
        )


def test_the_awaited_openai_path_returns_the_tool_call_the_model_made():
    body = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "memory_save", "arguments": "{}"}}
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    requests: list[httpx.Request] = []
    provider = OpenAICompatibleProvider(
        "selfhosted", async_client_factory=lambda **kwargs: _recording_async_client(requests, body, **kwargs)
    )

    message, _usage = _run(
        provider.async_chat(
            _selfhosted_target(),
            [{"role": "user", "content": "hi"}],
            tools=[SimpleNamespace(name="memory_save", description="d", parameters={}, to_dict=dict)],
            reasoning_effort="low",
            max_tokens=16,
            tool_choice="auto",
            temperature=None,
            no_proxy=False,
        )
    )

    assert [call["function"]["name"] for call in message["tool_calls"]] == ["memory_save"]


# --- the client entry points -------------------------------------------------


def _client_with(adapter) -> object:
    from ouroboros.llm.client import LLMClient
    from ouroboros.llm.registry import ProviderRegistry

    return LLMClient(registry=ProviderRegistry([adapter]))


class _BothWaysAdapter:
    """One adapter answering on both paths, recording what each was asked for."""

    provider_id = "gigachat-b2bank"

    def __init__(self, answer: dict, usage: dict) -> None:
        self.answer, self.usage, self.calls = answer, usage, []

    def supports(self, ref) -> bool:
        return True

    def close(self) -> None:
        return None

    def resolve_target(self, ref):
        return {**_selfhosted_target(), "resolved_model": ref.model_id, "usage_model": ref.model_id}

    def chat(self, target, messages, **kwargs):
        self.calls.append(("sync", kwargs))
        return dict(self.answer), dict(self.usage)

    async def async_chat(self, target, messages, **kwargs):
        self.calls.append(("async", kwargs))
        return dict(self.answer), dict(self.usage)


def test_both_client_paths_route_validate_and_account_alike(monkeypatch):
    """One request through each: the same candidate answers, and the same tokens are counted."""
    from ouroboros.llm.client import InvalidResponse, async_chat_with_fallback, chat_with_fallback

    monkeypatch.setattr("ouroboros.llm.client.provider_base_url_for_model", lambda model: "https://llm.local/v1")
    monkeypatch.setattr("ouroboros.llm.client.provider_circuit_is_open", lambda model, base_url: False)
    adapter = _BothWaysAdapter({"content": "ok", "finish_reason": "stop"}, {"prompt_tokens": 7, "completion_tokens": 3})
    client = _client_with(adapter)
    seen: list[tuple[str, dict]] = []

    def _reject_nothing(message):
        return None if message.get("content") else InvalidResponse.rejected("empty")

    call = {
        "model": "gigachat-b2bank::my-model",
        "fallback_model": "",
        "deadline": None,
        "timeout_sec": 30.0,
        "response_error": _reject_nothing,
        "on_response": lambda model, usage: seen.append((model, usage)),
        "messages": [{"role": "user", "content": "hi"}],
    }

    synchronous = chat_with_fallback(client, **call)
    asynchronous = _run(async_chat_with_fallback(client, **call))

    assert synchronous == asynchronous
    assert [model for model, _usage in seen] == ["gigachat-b2bank::my-model", "gigachat-b2bank::my-model"]
    assert seen[0][1] == seen[1][1]
    assert [path for path, _kwargs in adapter.calls] == ["sync", "async"]


def test_both_client_paths_fall_back_to_the_same_second_candidate(monkeypatch):
    from ouroboros.llm.client import async_chat_with_fallback, chat_with_fallback

    monkeypatch.setattr(
        "ouroboros.llm.client.provider_base_url_for_model",
        lambda model: f"https://{model.rsplit(chr(58), 1)[-1]}.local/v1",
    )
    monkeypatch.setattr("ouroboros.llm.client.provider_circuit_is_open", lambda model, base_url: False)
    monkeypatch.setattr("ouroboros.llm.client.record_provider_failure", lambda *a, **k: None)
    monkeypatch.setattr("ouroboros.llm.client.record_provider_success", lambda *a, **k: None)

    class _FailsFirst(_BothWaysAdapter):
        def chat(self, target, messages, **kwargs):
            if target["resolved_model"] == "primary":
                raise TimeoutError("provider unreachable")
            return super().chat(target, messages, **kwargs)

        async def async_chat(self, target, messages, **kwargs):
            if target["resolved_model"] == "primary":
                raise TimeoutError("provider unreachable")
            return await super().async_chat(target, messages, **kwargs)

    adapter = _FailsFirst({"content": "ok", "finish_reason": "stop"}, {"prompt_tokens": 1, "completion_tokens": 1})
    client = _client_with(adapter)
    call = {
        "model": "gigachat-b2bank::primary",
        "fallback_model": "gigachat-b2bank::secondary",
        "deadline": None,
        "timeout_sec": 30.0,
        "messages": [{"role": "user", "content": "hi"}],
    }

    assert chat_with_fallback(client, **call)[2] == "gigachat-b2bank::secondary"
    assert _run(async_chat_with_fallback(client, **call))[2] == "gigachat-b2bank::secondary"


def test_an_awaited_model_call_holds_a_slot_and_yields_while_it_waits(monkeypatch, restore_process_singletons):
    """The slot is taken on both paths, and waiting for one does not stall the loop."""
    from new_core.coordination.inprocess import InProcessBudget
    from new_core.coordination.registry import MODEL_CALLS, concurrency_budgets
    from ouroboros.llm.client import LLMClient
    from ouroboros.llm.registry import ProviderRegistry

    budget = InProcessBudget({MODEL_CALLS: 1})
    concurrency_budgets.initialize(budget)
    held = budget.acquire(MODEL_CALLS, holder="somebody-else", ttl_sec=60)
    assert held is not None
    ticks: list[int] = []
    in_use: list[int] = []

    class _Counting(_BothWaysAdapter):
        async def async_chat(self, target, messages, **kwargs):
            in_use.append(budget.in_use(MODEL_CALLS))
            return await super().async_chat(target, messages, **kwargs)

    adapter = _Counting({"content": "ok", "finish_reason": "stop"}, {})
    client = LLMClient(registry=ProviderRegistry([adapter]))

    async def _scenario():
        call = asyncio.ensure_future(
            client.async_chat([{"role": "user", "content": "hi"}], "gigachat-b2bank::my-model", timeout_sec=5)
        )
        for index in range(5):
            ticks.append(index)
            await asyncio.sleep(0.01)
        budget.release(held)
        return await call

    message, _usage = _run(_scenario())

    assert (message["content"], ticks) == ("ok", [0, 1, 2, 3, 4])
    assert in_use == [1], "the awaited call holds a slot for its duration"
    assert budget.in_use(MODEL_CALLS) == 0, "and releases it on the way out"
