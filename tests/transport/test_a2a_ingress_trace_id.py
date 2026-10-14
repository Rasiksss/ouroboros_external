"""A2A is an acceptor: the caller's `X-Trace-Id` arrives on the per-request call context.

The headers were already reaching the executor and being ignored — `execute` opened a bare
`trace_scope()` and started a fresh chain, so a caller's own identifier was dropped at the door.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

pytest.importorskip("a2a.server.routes.jsonrpc_routes", reason="a2a-sdk not installed; skip A2A tests")

from a2a.server.context import ServerCallContext

from ouroboros.observability import aef_tracing
from tests.helpers.fake_gateway import FakeGateway, RecordingMemoryEvents
from transport.a2a.delivery import A2AResponseRouter
from transport.a2a.executor import OuroborosA2AExecutor
from transport.a2a.interfaces import ISupervisorReadiness
from transport.a2a.proto import new_user_text_message
from transport.a2a.server import build_call_context_builder

DASHED = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
#: The other spelling a caller may send, normalized to the dashed form on the way in.
PACKED = "f47ac10b58cc4372a5670e02b2c3d479"
OTHER = "463ac35c-9f64-13ad-4848-5a3953bb6124"


class _ReadyNow(ISupervisorReadiness):
    async def wait_ready(self, *, timeout_sec: float) -> None:
        return None


class _RecordingExecutor(OuroborosA2AExecutor):
    """The production executor with the supervisor replaced by a read of the active id."""

    def __init__(self, tmp_path) -> None:
        gateway = FakeGateway()
        super().__init__(
            max_concurrent=3,
            task_store=_NullTaskStore(),
            gateway=gateway,
            router=A2AResponseRouter(gateway.outbox),
            readiness=_ReadyNow(),
            session_id="sess-test",
            memory_events=RecordingMemoryEvents(),
            drive_root=tmp_path,
        )
        self.observed = ""

    async def _dispatch_to_supervisor(self, text, task_id, context_id, message_id):
        self.observed = aef_tracing.current_trace_id()
        return "done"


class _NullTaskStore:
    async def get(self, task_id, call_context=None):
        return None

    async def save(self, *args, **kwargs):
        return None


class _CollectingQueue:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def enqueue_event(self, event: object) -> None:
        self.events.append(event)


class _RequestContext:
    """Only what `execute` reads: ids, the message, and the per-request call context."""

    def __init__(self, call_context) -> None:
        self.task_id = "task-1"
        self.context_id = "ctx-1"
        self.message = new_user_text_message("hello")
        self.current_task = None
        self.call_context = call_context


def _run(executor: _RecordingExecutor, call_context) -> str:
    asyncio.run(executor.execute(_RequestContext(call_context), _CollectingQueue()))
    return executor.observed


def _tracing_warnings(caplog) -> list[logging.LogRecord]:
    """Only the tracing logger's own records: caplog captures every logger, and an unrelated
    warning from elsewhere in the executor says nothing about identifier acceptance."""
    return [record for record in caplog.records if record.name == aef_tracing.log.name]


def _context_with(headers: dict[str, str] | None) -> ServerCallContext:
    return ServerCallContext(state={} if headers is None else {"headers": headers})


def test_a_callers_identifier_is_accepted_from_the_request_headers(tmp_path):
    observed = _run(_RecordingExecutor(tmp_path), _context_with({"x-trace-id": OTHER}))

    assert observed == OTHER


def test_a_dashed_identifier_runs_under_exactly_what_the_caller_sent(tmp_path):
    observed = _run(_RecordingExecutor(tmp_path), _context_with({"X-Trace-Id": DASHED}))

    assert observed == DASHED


def test_a_packed_identifier_runs_under_its_dashed_form(tmp_path):
    """The dashes a caller left out are put back, rather than the ones it sent being cut."""
    observed = _run(_RecordingExecutor(tmp_path), _context_with({"X-Trace-Id": PACKED}))

    assert observed == DASHED


def test_a_malformed_identifier_warns_naming_the_boundary_and_mints(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger=aef_tracing.log.name):
        observed = _run(_RecordingExecutor(tmp_path), _context_with({"x-trace-id": "not-a-uuid"}))

    assert aef_tracing.normalize_trace_id(observed) == observed
    assert observed != "not-a-uuid"
    assert [r for r in caplog.records if "a2a" in r.getMessage()]
    assert "not-a-uuid" not in caplog.text


def test_a_request_with_no_header_mints_silently(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger=aef_tracing.log.name):
        observed = _run(_RecordingExecutor(tmp_path), _context_with({"accept": "*/*"}))

    assert aef_tracing.normalize_trace_id(observed) == observed
    assert not _tracing_warnings(caplog)


def test_an_absent_call_context_is_no_carrier_rather_than_an_error(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger=aef_tracing.log.name):
        observed = _run(_RecordingExecutor(tmp_path), None)

    assert aef_tracing.normalize_trace_id(observed) == observed
    assert not _tracing_warnings(caplog)


def test_the_ingress_supplies_its_own_call_context_builder():
    """The pin for D3: relying on the SDK's default would make the ingress stop correlating,
    silently, the day that default changes."""
    from a2a.server.routes.common import ServerCallContextBuilder

    builder = build_call_context_builder()

    assert isinstance(builder, ServerCallContextBuilder)


def test_the_builder_guarantees_the_headers_entry():
    from starlette.requests import Request

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"x-trace-id", OTHER.encode())],
        }
    )

    context = build_call_context_builder().build(request)

    assert context.state["headers"]["x-trace-id"] == OTHER


def test_the_pre_routes_sdk_branch_also_gets_the_builder(monkeypatch):
    """The legacy constructor path must carry the same guarantee.

    It only runs when `a2a.server.routes.common` is missing — which is also where the modern
    base class comes from, so wiring it off that name alone made the keyword permanently
    unreachable and left older SDKs on the default builder, uncorrelated and silent about it.
    """
    from transport.a2a import server

    passed: dict[str, object] = {}

    class _LegacyBuilderBase:
        def build(self, request):
            return type("_Ctx", (), {"state": {}})()

    class _LegacyApp:
        def __init__(self, *, agent_card, http_handler, card_modifier, context_builder=None):
            passed["context_builder"] = context_builder

        def build(self, *, agent_card_url, rpc_url):
            return "legacy-app"

    monkeypatch.setattr(server, "_A2AStarletteApplication", _LegacyApp)
    monkeypatch.setattr(server, "_DefaultServerCallContextBuilder", _LegacyBuilderBase)

    app = server._build_starlette_app(object(), object(), _NoSkills(), max_chars=1000)

    assert app == "legacy-app"
    assert isinstance(passed["context_builder"], _LegacyBuilderBase)


class _NoSkills:
    def list_skills(self):
        return []
