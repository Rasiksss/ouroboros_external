"""Browser WS is an acceptor, on both of its carriers: the message body and the handshake.

`_browser_trace_id` used to compose over `trace_id_from_headers`, which validates as it looks
up and so returns `""` for a malformed header as well as an absent one — the collapse the
boundary acceptor exists to undo.

The two carriers have different lifetimes, and that is what splits the acceptor in two. A body
value arrives once per message; the handshake headers are fixed for the life of the socket, so
they are read once when it is accepted. Reading them per message would re-log the same rejection
for every frame a client sends, which is unbounded log volume from one bad header on an ingress
that is not authenticated.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from ouroboros.observability import aef_tracing
from tests.helpers.files import file_client, stored_file
from tests.helpers.server_client import patched_server_client
from transport.browser.ws import _browser_trace_id, _handshake_trace_id

DASHED = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
#: The other spelling a caller may send, normalized to the dashed form on the way in.
PACKED = "f47ac10b58cc4372a5670e02b2c3d479"
OTHER = "463ac35c-9f64-13ad-4848-5a3953bb6124"


def test_parallel_downloads_keep_trace_and_agent_headers_through_response(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SYSTEM_CONFIGURATION_AGENT_ID", "configured-agent")
    file = stored_file()
    observations = []

    class Blob:
        @asynccontextmanager
        async def open(self, *args):
            async def chunks():
                await asyncio.sleep(0)
                observations.append(aef_tracing.with_agent_header(aef_tracing.with_trace_header()))
                yield b"hello"

            yield chunks()

    with file_client(tmp_path) as client, ThreadPoolExecutor(2) as pool:
        client.app.state.file_blob = Blob()
        requests = [
            pool.submit(client.get, url, headers={"X-Trace-Id": trace})
            for url, trace in (
                (f"/api/files/{file['id']}/content", DASHED),
                (f"/api/chat/files/{file['id']}/report.txt", OTHER),
            )
        ]
        assert all(request.result(timeout=5).content == b"hello" for request in requests)
    assert {item["X-Trace-Id"] for item in observations} == {DASHED, OTHER}
    assert {item["X-Agent-Id"] for item in observations} == {"configured-agent"}
    assert aef_tracing.current_trace_id() == ""


class _Socket:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}


def test_a_dashed_identifier_in_the_body_is_accepted_unchanged():
    assert _browser_trace_id({"trace_id": DASHED}, "") == DASHED


def test_a_packed_identifier_in_the_body_is_accepted_and_dashed():
    assert _browser_trace_id({"trace_id": PACKED}, "") == DASHED


def test_a_malformed_identifier_in_the_body_warns_and_mints(caplog):
    with caplog.at_level(logging.WARNING, logger=aef_tracing.log.name):
        trace_id = _browser_trace_id({"trace_id": "not-a-uuid"}, "")

    assert aef_tracing.normalize_trace_id(trace_id) == trace_id
    assert [record for record in caplog.records if "browser" in record.getMessage()]
    assert "not-a-uuid" not in caplog.text


def test_a_malformed_identifier_in_the_handshake_warns_and_mints(caplog):
    with caplog.at_level(logging.WARNING, logger=aef_tracing.log.name):
        trace_id = _handshake_trace_id(_Socket({"x-trace-id": "bogus"}))

    assert aef_tracing.normalize_trace_id(trace_id) == trace_id
    assert [record for record in caplog.records if "browser" in record.getMessage()]


def test_the_handshake_header_is_read_once_however_many_messages_follow(caplog):
    """The regression: a socket that read the fixed handshake headers per message logged one
    rejection per frame, for the life of the connection."""
    socket = _Socket({"x-trace-id": "bogus"})

    with caplog.at_level(logging.WARNING, logger=aef_tracing.log.name):
        connection_trace_id = _handshake_trace_id(socket)
        for _ in range(5):
            _browser_trace_id({}, connection_trace_id)

    assert len([record for record in caplog.records if "browser" in record.getMessage()]) == 1


def test_the_handshake_header_is_accepted_when_the_body_carries_nothing():
    assert _browser_trace_id({}, _handshake_trace_id(_Socket({"X-Trace-Id": OTHER}))) == OTHER


def test_a_connection_without_the_header_mints_per_message_rather_than_pinning_one():
    """A client that sent no header asked for nothing, so its messages stay independent
    operations. A client that *did* send one asked for connection scope and gets it."""
    connection_trace_id = _handshake_trace_id(_Socket())

    minted = {_browser_trace_id({}, connection_trace_id) for _ in range(3)}

    assert connection_trace_id == ""
    assert len(minted) == 3


def test_no_identifier_anywhere_mints_silently(caplog):
    with caplog.at_level(logging.WARNING, logger=aef_tracing.log.name):
        trace_id = _browser_trace_id({}, _handshake_trace_id(_Socket()))

    assert aef_tracing.normalize_trace_id(trace_id) == trace_id
    assert not [record for record in caplog.records if record.name == aef_tracing.log.name]


def test_the_http_half_of_the_transport_accepts_the_identifier_too(monkeypatch, tmp_path):
    """The regression this closes: `/api/chat/*` read no header, so an upload's storage writes
    went to the wire bare and its log lines dropped the field — the header arrived and nothing
    read it, which leaves no evidence anywhere that correlation was lost."""
    observed: list[str] = []

    with patched_server_client(monkeypatch, tmp_path, browser_enabled=True, reset_storage=True) as client:
        observed = client.app.state.file_blob.traces
        response = client.post(
            "/api/chat/upload",
            files={"file": ("trace.txt", b"hello", "text/plain")},
            headers={aef_tracing.TRACE_HEADER_NAME: DASHED},
        )

    assert response.status_code == 200
    assert observed == [DASHED]


def test_an_http_request_carrying_the_packed_spelling_is_normalized(monkeypatch, tmp_path):
    observed: list[str] = []

    with patched_server_client(monkeypatch, tmp_path, browser_enabled=True, reset_storage=True) as client:
        observed = client.app.state.file_blob.traces
        client.post(
            "/api/chat/upload",
            files={"file": ("trace.txt", b"hello", "text/plain")},
            headers={aef_tracing.TRACE_HEADER_NAME: PACKED},
        )

    assert observed == [DASHED]


def test_a_malformed_http_identifier_warns_and_mints(monkeypatch, tmp_path, capsys):
    """Read off the configured sink rather than `caplog`: the server under test installs the
    project's own logging setup, so the record reaches stdout as JSON and not the pytest handler.
    That is also the sink an operator reads, which is the point of the warning."""
    observed: list[str] = []

    with patched_server_client(monkeypatch, tmp_path, browser_enabled=True, reset_storage=True) as client:
        observed = client.app.state.file_blob.traces
        client.post(
            "/api/chat/upload",
            files={"file": ("trace.txt", b"hello", "text/plain")},
            headers={aef_tracing.TRACE_HEADER_NAME: "not-a-uuid"},
        )

    logged = capsys.readouterr().out
    assert aef_tracing.normalize_trace_id(observed[0]) == observed[0]
    assert "Rejected a malformed trace id at the browser boundary" in logged
    assert "not-a-uuid" not in logged


def test_an_http_request_with_no_identifier_originates_one(monkeypatch, tmp_path):
    """An ingress originates rather than running under nothing: work with no caller still has
    an operation, and the storage writes it makes are correlated to each other."""
    observed: list[str] = []

    with patched_server_client(monkeypatch, tmp_path, browser_enabled=True, reset_storage=True) as client:
        observed = client.app.state.file_blob.traces
        client.post("/api/chat/upload", files={"file": ("trace.txt", b"hello", "text/plain")})

    assert len(observed) == 1
    assert aef_tracing.normalize_trace_id(observed[0]) == observed[0]


def test_two_http_requests_each_run_under_their_own_identifier(monkeypatch, tmp_path):
    """One scope per request, not one per process: the header is read at each request."""
    observed: list[str] = []

    with patched_server_client(monkeypatch, tmp_path, browser_enabled=True, reset_storage=True) as client:
        observed = client.app.state.file_blob.traces
        for trace_id in (DASHED, OTHER):
            client.post(
                "/api/chat/upload",
                files={"file": ("trace.txt", b"hello", "text/plain")},
                headers={aef_tracing.TRACE_HEADER_NAME: trace_id},
            )

    assert observed == [DASHED, OTHER]


def test_the_scope_does_not_outlive_the_request(monkeypatch, tmp_path):
    """A scope leaked out of the handler would attribute later, unrelated work to this caller."""
    with patched_server_client(monkeypatch, tmp_path, browser_enabled=True, reset_storage=True) as client:
        client.get("/api/chat/history", headers={aef_tracing.TRACE_HEADER_NAME: DASHED})

    assert aef_tracing.current_trace_id() == ""


def test_a_ws_frame_runs_under_a_scope_not_only_labels_its_envelope(monkeypatch, tmp_path):
    """The regression: the WS handler resolved the identifier, put it on the envelope and stopped.

    It binds attachments to object storage and persists the row *in band*, so those writes read an
    empty context variable and went to the wire bare, and the handler's own warnings dropped the
    field. Labelling the envelope correlates the task the supervisor later runs; it does nothing
    for the work the transport does first.
    """
    from tests.helpers.supervisor import bootstrap_supervisor_for_tests

    runtime = bootstrap_supervisor_for_tests(tmp_path)
    observed: list[str] = []
    monkeypatch.setattr(
        runtime.gateway.inbox,
        "submit",
        lambda envelope: observed.append(aef_tracing.current_trace_id()),
    )

    with patched_server_client(monkeypatch, tmp_path, browser_enabled=True, reset_runtime=True) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "chat", "content": "hello", "trace_id": DASHED})
            deadline = time.time() + 2.0
            while time.time() < deadline and not observed:
                time.sleep(0.02)

    assert observed == [DASHED]


def test_the_ws_scope_does_not_outlive_the_frame(monkeypatch, tmp_path):
    """Per frame, not per connection: a scope left open would attribute every later frame on the
    socket — and every heartbeat and broadcast between them — to whoever sent the first one."""
    from tests.helpers.supervisor import bootstrap_supervisor_for_tests

    runtime = bootstrap_supervisor_for_tests(tmp_path)
    observed: list[str] = []
    monkeypatch.setattr(
        runtime.gateway.inbox,
        "submit",
        lambda envelope: observed.append(aef_tracing.current_trace_id()),
    )

    with patched_server_client(monkeypatch, tmp_path, browser_enabled=True, reset_runtime=True) as client:
        with client.websocket_connect("/ws") as websocket:
            for index, trace_id in enumerate((DASHED, OTHER)):
                websocket.send_json(
                    {
                        "type": "chat",
                        "content": f"m{index}",
                        "client_message_id": uuid.uuid4().hex,
                        "trace_id": trace_id,
                    }
                )
                deadline = time.time() + 2.0
                while time.time() < deadline and len(observed) <= index:
                    time.sleep(0.02)

    assert observed == [DASHED, OTHER]
    assert aef_tracing.current_trace_id() == ""
