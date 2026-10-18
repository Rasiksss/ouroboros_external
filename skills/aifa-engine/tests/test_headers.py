"""F12 (б): каждый вызов плагина несёт `X-Trace-Id` и `X-Agent-Id` (инвариант 6 ouroboros, ADR-0022).

Заголовки ставит `ouroboros.observability.aef_tracing`, плагин заголовки сам не чеканит. Транспорт
подставной (`httpx.MockTransport`), так что видно то, что ушло бы в службу.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from aifa_engine_support import RUN_ID, load_plugin, tool_context

from ouroboros.observability import aef_tracing

TRACE = "0a0a0a0a-0a0a-4a0a-8a0a-0a0a0a0a0a0a"
AGENT = "aifa-agent-1"


@pytest.fixture(autouse=True)
def service_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Адрес и токен службы — из окружения (R20); «кто я» отвечает ядро, его ответ подставлен."""
    monkeypatch.setenv("AIFA_ENGINE_URL", "http://engine:8000")
    monkeypatch.setenv("AIFA_ENGINE_TOKEN", "secret-token")
    monkeypatch.setattr(aef_tracing, "current_agent_id", lambda: "aifa-agent-1")


class Wire:
    """Транспорт, который записывает запросы и отвечает кодом `ГОТОВО`."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, text="ГОТОВО\nитог")


def call(tool: str, wire: Wire, **args: object) -> str:
    plugin = load_plugin()
    handler = plugin.EngineCall(tool, transport=httpx.MockTransport(wire))
    return asyncio.run(handler(tool_context(), **args))


def test_the_trace_and_agent_headers_are_on_a_goal_call() -> None:
    """F12 (б): `aifa_goal` идёт с трассой хода и идентификатором агента."""
    wire = Wire()

    call("aifa_goal", wire)

    (request,) = wire.requests
    assert request.headers["X-Trace-Id"] == TRACE
    assert request.headers["X-Agent-Id"] == AGENT


def test_the_headers_are_on_every_call_not_only_the_first() -> None:
    """F12 (б): заголовки ставятся на каждом вызове, а не запоминаются при первом."""
    wire = Wire()

    call("aifa_goal", wire)
    call("aifa_wait", wire, job_id="j1", wait_sec=10)
    call("aifa_wait", wire, job_id="j1", wait_sec=10)

    assert len(wire.requests) == 3
    for request in wire.requests:
        assert (request.headers["X-Trace-Id"], request.headers["X-Agent-Id"]) == (TRACE, AGENT)


def test_the_trace_of_each_call_is_the_trace_of_its_own_context() -> None:
    """F12 (б): другая задача — другая трасса; плагин не склеивает вызовы разных ходов одной трассой."""
    other = "1b1b1b1b-1b1b-4b1b-8b1b-1b1b1b1b1b1b"
    wire = Wire()
    plugin = load_plugin()
    handler = plugin.EngineCall("aifa_goal", transport=httpx.MockTransport(wire))

    asyncio.run(handler(tool_context(trace_id=TRACE)))
    asyncio.run(handler(tool_context(trace_id=other, transport={"source_message_id": RUN_ID})))

    assert [r.headers["X-Trace-Id"] for r in wire.requests] == [TRACE, other]


def test_the_service_token_goes_as_a_bearer_and_owner_headers_are_not_minted_here() -> None:
    """§3.0: `Authorization: Bearer`; заголовки владельца `X-Ouroboros-*` ставит только клиент хода (F12 (а))."""
    wire = Wire()

    call("aifa_goal", wire)

    (request,) = wire.requests
    assert request.headers["Authorization"] == "Bearer secret-token"
    assert not [name for name in request.headers if name.lower().startswith("x-ouroboros")]
