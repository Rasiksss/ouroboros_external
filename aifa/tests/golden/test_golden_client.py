"""Записывающий клиент (ADR-0005, F5): вход → проект → документы → ходы → файлы → запись.

Клиент говорит по настоящему HTTP с локальным подставным сервером, который повторяет контракт
ручек службы (`CONTRACTS.md` §1): вход с cookie, загрузка, `/chat/turns`, SSE `/events`,
скачивание файла. Живой прогон против dev — `golden_cli.py stability` (шаг 05, PROGRESS.md).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from golden_client import (
    ApiSession,
    ClientError,
    ClientSettings,
    StandError,
    check_stand,
    iter_sse,
)
from golden_format import dump
from golden_record import Scenario, record_scenario

COOKIE_VALUE = "SECRET-SESSION-TOKEN"
RESULT_BYTES = b"xlsx-bytes"
PDF_BYTES = b"%PDF-1.4 fake document body"
# Байты `/chat/runs/{id}/events`, снятые настоящим кодом службы (`_typed_run_response` поверх
# `run_orchestrator`): реплей, хвост, биения; конец штатный или сбой хода (PROGRESS.md, шаг 05).
REAL_SSE = Path(__file__).parent / "_selftest" / "sse"


@dataclass
class ServerState:
    turn_bodies: list[dict[str, Any]] = field(default_factory=list)
    uploads: list[str] = field(default_factory=list)
    drop_first_stream: bool = False
    stream_requests: list[str] = field(default_factory=list)
    ending: str = "completed"  # completed | error | session_lost | silent
    cut_mid_chunk: bool = False
    real_stream: bytes = b""  # отдать снятый поток службы вместо собранного вручную
    cancels: list[str] = field(default_factory=list)


def envelope(seq: int, kind: str, **fields: Any) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "event_id": f"e{seq}",
        "run_id": "r1",
        "sequence": seq,
        "occurred_at": "2026-10-16T10:00:00Z",
        "event_type": kind,
        **fields,
    }


def journal(turn_no: int, ending: str = "completed") -> list[dict[str, Any]]:
    head = [
        envelope(1, "legacy", legacy_event={"type": "session_id", "session_id": "sess-1"}),
        envelope(2, "legacy", legacy_event={"type": "thinking", "text": "Думаю"}),
        envelope(3, "legacy", legacy_event={"type": "text_delta", "text": "Смотрю. "}),
        envelope(4, "legacy", legacy_event={"type": "tool_call", "name": "calc"}),
        envelope(5, "legacy", legacy_event={"type": "text_delta", "text": f"Итог {turn_no}."}),
        envelope(
            6,
            "legacy",
            legacy_event={"type": "file", "file_id": "res-1", "filename": "r.xlsx"},
        ),
    ]
    if ending == "error":  # сбой или отмена хода: `_drive` пишет `error` и закрывает журнал
        failure = {"code": "legacy_error", "message": "run cancelled", "retryable": False}
        return [*head, envelope(7, "error", error=failure, partial_results=[])]
    if ending == "session_lost":  # за потерей прогона в журнале идёт `done`
        lost = envelope(7, "legacy", legacy_event={"type": "session_lost", "message": "x"})
        return [*head, lost, envelope(8, "completed", status="failed")]
    if ending == "silent":  # ход идёт, а конца нет
        return head
    return [*head, envelope(7, "completed", status="succeeded")]


def handler_for(state: ServerState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            return

        def send_json(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def authorised(self) -> bool:
            return f"tok={COOKIE_VALUE}" in (self.headers.get("Cookie") or "")

        def read_body(self) -> bytes:
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            body = self.read_body()
            if path == "/auth/login":
                creds = json.loads(body)
                if creds != {"username": "qa", "password": "pw"}:
                    self.send_json(401, {"detail": "Invalid username or password"})
                    return
                cookie = f"tok={COOKIE_VALUE}; HttpOnly; Secure; Path=/"
                self.send_json(200, {"username": "qa"}, {"Set-Cookie": cookie})
            elif not self.authorised():
                self.send_json(401, {"detail": "Not authenticated"})
            elif path == "/documents/upload":
                name = body.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
                state.uploads.append(name)
                if name.endswith(".xls"):
                    self.send_json(415, {"detail": "Формат XLS не поддерживается"})
                else:
                    self.send_json(200, {"doc_id": f"doc-{len(state.uploads)}", "processing": True})
            elif path.endswith("/parse"):
                self.send_json(200, {"processing": False, "ready": True})
            elif path == "/chat/turns":
                state.turn_bodies.append(json.loads(body))
                self.send_json(200, {"run_id": "r1", "events_url": "/chat/runs/r1/events"})
            elif path == "/chat/runs/r1/cancel":
                state.cancels.append("r1")
                self.send_json(200, {"cancelled": True})
            else:
                self.send_json(404, {"detail": "no route"})

        def do_GET(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            if not self.authorised():
                self.send_json(401, {"detail": "Not authenticated"})
            elif parts.path.endswith("/status"):
                self.send_json(200, {"processing": False})
            elif parts.path == "/chat/runs/r1/events":
                self.stream(parts.query)
            elif re.fullmatch(r"/projects/qa-golden-[\w-]+/sources/res-1/file", parts.path):
                # как экран: `projectSourceDownloadUrl` (api.ts)
                self.send_response(200)
                self.send_header("Content-Length", str(len(RESULT_BYTES)))
                self.end_headers()
                self.wfile.write(RESULT_BYTES)
            elif parts.path.endswith("/artifacts"):
                self.send_json(200, {"artifacts": []})
            else:
                self.send_json(404, {"detail": "no route"})

        def stream(self, query: str) -> None:
            state.stream_requests.append(query)
            after = int((parse_qs(query).get("after") or ["0"])[0])
            if state.real_stream:
                self.send_real_stream(after)
                return
            turn_no = len(state.turn_bodies)
            events = [e for e in journal(turn_no, state.ending) if e["sequence"] > after]
            drop = state.drop_first_stream and after == 0
            if drop:
                events = events[:3]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            if state.cut_mid_chunk and after == 0:
                self.send_chunk_and_cut(events[:3])
                return
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b": comment\n\n")
            self.wfile.write(
                b'event: run_event.v1\ndata: {"event_type": "heartbeat", "sequence": 0}\n\n'
            )
            for event in events:
                payload = json.dumps(event, ensure_ascii=False)
                self.wfile.write(
                    f"id: {event['sequence']}\nevent: run_event.v1\ndata: {payload}\n\n".encode()
                )
            self.wfile.flush()
            self.close_connection = True

        def send_chunk_and_cut(self, events: list[dict[str, Any]]) -> None:
            # Как uvicorn: поток кусками (chunked); связь рвётся посреди объявленного куска.
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for event in events:
                frame = f"id: {event['sequence']}\ndata: {json.dumps(event)}\n\n".encode()
                self.wfile.write(f"{len(frame):x}\r\n".encode() + frame + b"\r\n")
            self.wfile.write(b"400\r\nid: 4\ndata: {")
            self.wfile.flush()
            self.close_connection = True

        def send_real_stream(self, after: int) -> None:
            # Кадры снятого потока как есть; догон — по `id:`, как `_event_cursor` службы.
            frames = [f for f in state.real_stream.split(b"\n\n") if f.strip()]
            kept = [f for f in frames if (_frame_id(f) or after + 1) > after]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"".join(frame + b"\n\n" for frame in kept))
            self.wfile.flush()
            self.close_connection = True

    return Handler


def _frame_id(frame: bytes) -> int | None:
    for line in frame.split(b"\n"):
        if line.startswith(b"id: "):
            return int(line[4:])
    return None


@pytest.fixture
def server() -> Iterator[tuple[str, ServerState]]:
    state = ServerState()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(state))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", state
    finally:
        httpd.shutdown()
        httpd.server_close()


def settings(base: str) -> ClientSettings:
    return ClientSettings(
        base_url=base, username="qa", password="pw", poll_interval_s=0.01, build="abc1234"
    )


def scenario(
    tmp_path: Path, *, documents: tuple[str, ...] = ("fs.pdf",), turns: int = 2
) -> Scenario:
    paths = []
    for name in documents:
        path = tmp_path / name
        path.write_bytes(PDF_BYTES)
        paths.append(path)
    return Scenario(
        name="T1",
        documents=tuple(paths),
        turns=tuple(f"вопрос {n}" for n in range(1, turns + 1)),
    )


def test_a_scenario_is_recorded_through_login_upload_turns_and_download(
    server: tuple[str, ServerState], tmp_path: Path
) -> None:
    base, state = server
    recording = record_scenario(scenario(tmp_path), settings(base))
    assert [t.user for t in recording.turns] == ["вопрос 1", "вопрос 2"]
    first = recording.turns[0]
    assert first.answer == "Итог 1."  # текст после последнего вызова инструмента, как на экране
    assert all(e["event_type"] != "heartbeat" for e in first.events)
    assert [e["sequence"] for e in first.events] == [1, 2, 3, 4, 5, 6, 7]
    assert first.files[0].name == "r.xlsx"
    assert first.files[0].sha256 == hashlib.sha256(RESULT_BYTES).hexdigest()
    assert recording.documents[0].sha256 == hashlib.sha256(PDF_BYTES).hexdigest()
    assert recording.documents[0].upload_status == 200
    assert recording.meta.stand == "127.0.0.1" and recording.meta.build == "abc1234"
    assert recording.snapshot["artifacts"] == {"artifacts": []}
    assert "Case" in recording.snapshot["case"] or "case" in recording.snapshot["case"].lower()
    # ход 2 продолжает серверную сессию, документ приложен к обоим ходам
    assert state.turn_bodies[0]["session_id"] == ""
    assert state.turn_bodies[1]["session_id"] == "sess-1"
    assert state.turn_bodies[1]["doc_ids"] == ["doc-1"]
    assert state.turn_bodies[0]["project_id"] == state.turn_bodies[1]["project_id"] != ""


def test_the_recording_text_has_no_token_cookie_or_password(
    server: tuple[str, ServerState], tmp_path: Path
) -> None:
    base, _ = server
    text = dump(record_scenario(scenario(tmp_path), settings(base)))
    for secret in (COOKIE_VALUE, "pw", "Set-Cookie", "Authorization"):
        assert secret not in text, secret


def test_a_refused_upload_is_recorded_and_not_attached_to_turns(
    server: tuple[str, ServerState], tmp_path: Path
) -> None:
    base, state = server
    recording = record_scenario(
        scenario(tmp_path, documents=("old.xls", "fs.pdf"), turns=1), settings(base)
    )
    refused, accepted = recording.documents
    assert (refused.name, refused.upload_status) == ("old.xls", 415)
    assert "XLS" in refused.upload_detail
    assert accepted.upload_status == 200
    assert state.turn_bodies[0]["doc_ids"] == ["doc-2"]


def test_a_dropped_stream_is_resumed_from_the_last_event(
    server: tuple[str, ServerState], tmp_path: Path
) -> None:
    base, state = server
    state.drop_first_stream = True
    recording = record_scenario(scenario(tmp_path, turns=1), settings(base))
    assert [e["sequence"] for e in recording.turns[0].events] == [1, 2, 3, 4, 5, 6, 7]
    assert any("after=3" in query for query in state.stream_requests)


def test_a_wrong_password_stops_before_any_turn(
    server: tuple[str, ServerState], tmp_path: Path
) -> None:
    base, state = server
    bad = ClientSettings(base_url=base, username="qa", password="wrong")
    with pytest.raises(ClientError, match="вход"):
        record_scenario(scenario(tmp_path), bad)
    assert state.turn_bodies == []


def test_only_the_dev_stand_or_loopback_is_allowed() -> None:
    check_stand("https://finagent.homyakagent.ru/api")
    check_stand("http://127.0.0.1:18180")
    for url in ("https://example.org/api", "https://example.com", "ftp://127.0.0.1"):
        with pytest.raises(StandError):
            check_stand(url)


def test_settings_come_from_the_environment_and_name_the_missing_variable() -> None:
    env = {
        "AIFA_GOLDEN_BASE_URL": "http://127.0.0.1:1",
        "AIFA_GOLDEN_USER": "qa",
        "AIFA_GOLDEN_PASSWORD": "pw",
    }
    assert ClientSettings.from_env(env).username == "qa"
    del env["AIFA_GOLDEN_PASSWORD"]
    with pytest.raises(ClientError, match="AIFA_GOLDEN_PASSWORD"):
        ClientSettings.from_env(env)


def test_a_non_dev_address_is_refused_before_any_request() -> None:
    with pytest.raises(StandError):
        ApiSession(ClientSettings(base_url="https://example.org", username="u", password="p"))


def test_sse_lines_become_json_payloads_without_comments() -> None:
    lines = [
        b": keep-alive\n",
        b"\n",
        b"id: 4\n",
        b"event: run_event.v1\n",
        b'data: {"a":\n',
        b"data: 1}\n",
        b"\n",
        b'data: {"b": 2}\n',
        b"\n",
    ]
    assert list(iter_sse(lines)) == [{"a": 1}, {"b": 2}]


def test_a_turn_that_failed_or_was_cancelled_ends_with_error_and_is_recorded(
    server: tuple[str, ServerState], tmp_path: Path
) -> None:
    """Журнал сбоя кончается `error` без `completed`, поток закрывается — ход окончен."""
    base, state = server
    state.ending = "error"
    recording = record_scenario(scenario(tmp_path, turns=1), settings(base))
    assert recording.turns[0].events[-1]["event_type"] == "error"
    assert len(state.stream_requests) == 1
    assert state.cancels == []


def test_session_lost_is_read_up_to_its_completed(
    server: tuple[str, ServerState], tmp_path: Path
) -> None:
    base, state = server
    state.ending = "session_lost"
    events = record_scenario(scenario(tmp_path, turns=1), settings(base)).turns[0].events
    assert [e["event_type"] for e in events[-2:]] == ["legacy", "completed"]


@pytest.mark.parametrize("turn_timeout_s", [0.0, 1800.0])
def test_a_turn_that_does_not_end_is_cancelled_on_the_stand(
    server: tuple[str, ServerState], tmp_path: Path, turn_timeout_s: float
) -> None:
    """Таймаут и исчерпанные переподключения: ход на dev не остаётся работать без записи."""
    base, state = server
    state.ending = "silent"
    slow = ClientSettings(
        base_url=base,
        username="qa",
        password="pw",
        poll_interval_s=0.01,
        turn_timeout_s=turn_timeout_s,
    )
    with pytest.raises(ClientError, match="отменён"):
        record_scenario(scenario(tmp_path, turns=1), slow)
    assert state.cancels == ["r1"]


def test_a_stream_cut_inside_a_chunk_is_resumed(
    server: tuple[str, ServerState], tmp_path: Path
) -> None:
    base, state = server
    state.cut_mid_chunk = True
    recording = record_scenario(scenario(tmp_path, turns=1), settings(base))
    assert [e["sequence"] for e in recording.turns[0].events] == [1, 2, 3, 4, 5, 6, 7]
    assert any("after=3" in query for query in state.stream_requests)


@pytest.mark.parametrize("ending", ["completed", "error"])
def test_a_real_stream_of_the_service_is_parsed_with_heartbeats_and_cursors(ending: str) -> None:
    raw = (REAL_SSE / f"events-{ending}.sse").read_bytes()
    payloads = list(iter_sse(raw.splitlines(keepends=True)))
    kinds = [p["event_type"] for p in payloads]
    assert "heartbeat" in kinds
    durable = [p["sequence"] for p in payloads if p["event_type"] != "heartbeat"]
    assert durable == list(range(1, len(durable) + 1))
    assert kinds[-1] == ending


@pytest.mark.parametrize("ending", ["completed", "error"])
def test_a_real_stream_of_the_service_is_recorded_to_its_end(
    server: tuple[str, ServerState], tmp_path: Path, ending: str
) -> None:
    base, state = server
    state.real_stream = (REAL_SSE / f"events-{ending}.sse").read_bytes()
    events = record_scenario(scenario(tmp_path, turns=1), settings(base)).turns[0].events
    assert events[-1]["event_type"] == ending
    assert all(e["event_type"] != "heartbeat" for e in events)
    assert len(state.stream_requests) == 1 and state.cancels == []
