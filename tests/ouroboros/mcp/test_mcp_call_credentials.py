from __future__ import annotations

import asyncio

import anyio
from mcp.shared.message import ClientMessageMetadata, SessionMessage
from mcp.types import JSONRPCNotification, JSONRPCRequest, JSONRPCResponse

from ouroboros.mcp.transports import CallCredentials, CredentialedWriteStream
from tests.helpers.parametrize import parametrize_from_dict

CREDENTIALS = {"X-Sudir-Token": "t1", "Cookie": "sudir=abc"}


def _call(request_id: int = 1) -> JSONRPCRequest:
    return JSONRPCRequest(jsonrpc="2.0", id=request_id, method="tools/call", params={"name": "echo"})


async def _sent_through(items: list[SessionMessage], *, bound: bool) -> list[SessionMessage]:
    credentials = CallCredentials()
    send, receive = anyio.create_memory_object_stream[SessionMessage](len(items))
    stream = CredentialedWriteStream(send, credentials)
    if bound:
        with credentials.bound(CREDENTIALS):
            for item in items:
                await stream.send(item)
    else:
        for item in items:
            await stream.send(item)
    await stream.aclose()
    return [message async for message in receive]


def test_a_bound_tool_call_carries_the_credentials_beside_its_own_headers() -> None:
    item = SessionMessage(message=_call(), metadata=ClientMessageMetadata(headers={"Mcp-Name": "echo"}))

    (sent,) = asyncio.run(_sent_through([item], bound=True))

    assert sent.message is item.message
    assert sent.metadata.headers == {"Mcp-Name": "echo", **CREDENTIALS}
    assert item.metadata.headers == {"Mcp-Name": "echo"}


untouched_cases = {
    "a tool listing": {
        "item": SessionMessage(message=JSONRPCRequest(jsonrpc="2.0", id=2, method="tools/list")),
        "bound": True,
    },
    "a cancellation": {
        "item": SessionMessage(
            message=JSONRPCNotification(jsonrpc="2.0", method="notifications/cancelled", params={"requestId": 1})
        ),
        "bound": True,
    },
    "a reply to a server request": {
        "item": SessionMessage(message=JSONRPCResponse(jsonrpc="2.0", id=7, result={})),
        "bound": True,
    },
    "a tool call from a task that bound nothing": {"item": SessionMessage(message=_call()), "bound": False},
}


@parametrize_from_dict(untouched_cases)
def test_anything_but_a_bound_tool_call_passes_through_untouched(item, bound) -> None:
    (sent,) = asyncio.run(_sent_through([item], bound=bound))

    assert sent is item


def test_credentials_are_bound_to_one_task_and_released_after_it() -> None:
    credentials = CallCredentials()

    async def body() -> None:
        other_saw: list[dict[str, str]] = []

        async def other_task() -> None:
            other_saw.append(credentials.of_current_task())

        with credentials.bound(CREDENTIALS):
            assert credentials.of_current_task() == CREDENTIALS
            await asyncio.create_task(other_task())
        assert other_saw == [{}]
        assert credentials.of_current_task() == {}

    asyncio.run(body())


def test_binding_outside_a_task_is_refused() -> None:
    async def body() -> str:
        loop = asyncio.get_running_loop()
        outcome: asyncio.Future[str] = loop.create_future()

        def from_a_callback() -> None:
            try:
                with CallCredentials().bound(CREDENTIALS):
                    outcome.set_result("bound")
            except RuntimeError as refusal:
                outcome.set_result(str(refusal))

        loop.call_soon(from_a_callback)
        return await outcome

    assert "inside a task" in asyncio.run(body())
