import asyncio
import logging
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace

import pytest

from ouroboros.llm import client as client_module


@pytest.fixture
def slots(monkeypatch):
    monkeypatch.setattr(client_module, "_llm_concurrency_slot", lambda **kwargs: nullcontext())

    @asynccontextmanager
    async def async_slot(**kwargs):
        yield

    monkeypatch.setattr(client_module, "_async_llm_concurrency_slot", async_slot)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_resolution_failure_is_visible_in_container_logs(caplog, asynchronous):
    failure = ValueError("secret provider configuration")

    def resolve(model):
        raise failure

    llm = client_module.LLMClient(registry=SimpleNamespace(resolve=resolve))
    with caplog.at_level(logging.INFO), pytest.raises(ValueError) as caught:
        if asynchronous:
            asyncio.run(llm.async_chat([{"content": "secret document"}], model="selfhosted::glm-test"))
        else:
            llm.chat([{"content": "secret document"}], model="selfhosted::glm-test")
    assert caught.value is failure
    assert "event=llm_call_start" in caplog.text
    assert "event=llm_call_failed" in caplog.text
    assert "error_type=ValueError" in caplog.text
    assert "selfhosted::glm-test" in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.parametrize("asynchronous", [False, True])
def test_model_response_logs_metadata_only(caplog, slots, asynchronous):
    answer = ({"content": "secret answer", "tool_calls": []}, {"prompt_tokens": 12, "completion_tokens": 5})

    def chat(*args, **kwargs):
        return answer

    async def async_chat(*args, **kwargs):
        return answer

    adapter = SimpleNamespace(chat=chat, async_chat=async_chat)
    target = {"provider": "selfhosted", "base_url": "http://user:secret@llm/v1?token=secret"}
    llm = client_module.LLMClient(registry=SimpleNamespace(resolve=lambda model: (adapter, target)))
    with caplog.at_level(logging.INFO):
        if asynchronous:
            result = asyncio.run(llm.async_chat([{"content": "secret prompt"}], model="glm-test"))
        else:
            result = llm.chat([{"content": "secret prompt"}], model="glm-test")
    assert result == answer
    assert "event=llm_call_complete" in caplog.text
    assert "event=llm_response" in caplog.text
    assert "http://llm/v1" in caplog.text
    assert "input_tokens=12" in caplog.text
    assert "output_tokens=5" in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.parametrize("kind", ["http", "websocket"])
def test_request_logging_does_not_read_or_buffer_messages(caplog, kind):
    from ouroboros.observability.diagnostics import RequestLoggingMiddleware

    sent = []

    async def receive():
        raise AssertionError("Logger must not read payloads")

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        scope["route"] = SimpleNamespace(path="/api/{id}")
        if kind == "http":
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"secret body", "more_body": True})
            assert len(sent) == 2
            await send({"type": "http.response.body", "body": b"last", "more_body": False})
        else:
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.send", "text": "secret message"})
            await send({"type": "websocket.close", "code": 1000})

    scope = {"type": kind, "path": "/api/secret-id", "method": "GET",
             "query_string": b"token=secret", "headers": [(b"authorization", b"secret")]}
    with caplog.at_level(logging.INFO):
        asyncio.run(RequestLoggingMiddleware(app)(scope, receive, send))
    assert len(sent) == 3
    assert f"event={kind}_complete" in caplog.text
    assert "/api/{id}" in caplog.text
    assert "secret" not in caplog.text
