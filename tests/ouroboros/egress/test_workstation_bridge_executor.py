from __future__ import annotations

import json
from typing import Any

import pytest

from ouroboros.egress.workstation.bridge import WorkstationFileBridge
from ouroboros.egress.workstation.client import (
    WorkstationAccessDenied,
    WorkstationApiError,
    WorkstationClient,
    WorkstationConfig,
    WorkstationProtocolError,
)
from ouroboros.egress.workstation.executor import WorkstationCommandExecutor


def _config(**overrides: Any) -> WorkstationConfig:
    base = {
        "base_url": "https://ws.example",
        "create_secret": "",
        "session_strategy": "ephemeral",
        "idle_timeout_sec": 900,
        "absolute_ttl_sec": 3600,
        "command_timeout_sec": 300,
        "http_timeout_sec": 30.0,
        "max_attempts": 3,
        "retry_base_delay_sec": 0.01,
        "workspace_root": "/workspace",
        "access_level": "rw",
        "attachment_max_bytes": 1024,
    }
    base.update(overrides)
    return WorkstationConfig(**base)


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None, *, content: bytes = b"") -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = content

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class FakeStreamResponse:
    def __init__(self, status_code: int, lines: list[str]) -> None:
        self.status_code = status_code
        self._lines = lines

    def __enter__(self) -> FakeStreamResponse:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def iter_lines(self):
        yield from self._lines

    def read(self) -> None:
        return None

    def json(self) -> Any:
        return {"code": "internal", "message": "boom"}


class FakeHttpxClient:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.calls: list[dict[str, Any]] = []
        self.responses: list[Any] = []
        self.stream_responses: list[FakeStreamResponse] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)

    def stream(self, method: str, url: str, **kwargs: Any) -> FakeStreamResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.stream_responses.pop(0)

    def close(self) -> None:
        pass


@pytest.fixture()
def rig(monkeypatch: pytest.MonkeyPatch):
    instances: list[FakeHttpxClient] = []

    def _factory(**kwargs: Any) -> FakeHttpxClient:
        client = FakeHttpxClient(**kwargs)
        instances.append(client)
        return client

    monkeypatch.setattr("time.sleep", lambda _s: None)
    config = _config()
    client = WorkstationClient(config, client_factory=_factory)
    client._get_client()
    fake = instances[-1]
    executor = WorkstationCommandExecutor(client, session_id="sess-1", token="tok", config=config)
    bridge = WorkstationFileBridge(client, executor, session_id="sess-1", token="tok", config=config)
    return fake, executor, bridge, config


# --- executor -------------------------------------------------------------------


def test_run_buffered_returns_outcome(rig) -> None:
    fake, executor, _bridge, _cfg = rig
    fake.responses = [FakeResponse(200, {"exitCode": 0, "stdout": "hi", "stderr": "", "timedOut": False})]

    outcome = executor.run_buffered(["/bin/sh", "-c", "echo hi"])

    assert outcome.exit_code == 0
    assert outcome.stdout == "hi"
    assert outcome.stdout_overflowed is False


def test_run_buffered_caps_large_output(rig) -> None:
    fake, executor, _bridge, _cfg = rig
    big = "x" * (256 * 1024 + 10)
    fake.responses = [FakeResponse(200, {"exitCode": 0, "stdout": big, "stderr": "", "timedOut": False})]

    outcome = executor.run_buffered(["cat"])

    assert outcome.stdout_overflowed is True
    assert len(outcome.stdout.encode("utf-8")) <= 256 * 1024


def test_run_streamed_accumulates_chunks_and_terminal(rig) -> None:
    fake, executor, _bridge, _cfg = rig
    fake.stream_responses = [
        FakeStreamResponse(
            200,
            [
                json.dumps({"kind": "stdout", "data": "a"}),
                json.dumps({"kind": "stdout", "data": "b"}),
                json.dumps({"kind": "stderr", "data": "e"}),
                json.dumps({"kind": "exit", "exitCode": 7, "timedOut": True}),
            ],
        )
    ]

    outcome = executor.run_streamed(["/bin/sh", "-c", "x"])

    assert outcome.stdout == "ab"
    assert outcome.stderr == "e"
    assert outcome.exit_code == 7
    assert outcome.timed_out is True


# --- bridge ---------------------------------------------------------------------


def test_read_file_downloads_normalized_path(rig) -> None:
    fake, _executor, bridge, _config = rig
    fake.responses = [FakeResponse(200, content=b"hello")]

    data = bridge.read_file("foo.txt")

    assert data == b"hello"
    call = fake.calls[0]
    assert call["json"] == {"path": "/workspace/foo.txt"}


def test_write_file_creates_parent_dir_then_uploads(rig) -> None:
    fake, _executor, bridge, _config = rig
    # mkdirp -> run_buffered -> exec (request #1); upload_file -> files:upload (request #2)
    fake.responses = [
        FakeResponse(200, {"exitCode": 0, "stdout": "", "stderr": "", "timedOut": False}),
        FakeResponse(200, {"path": "/workspace/a/b.txt", "bytes": 3, "sha256": "x"}),
    ]

    bridge.write_file("a/b.txt", b"abc")

    assert "/v1/sessions/sess-1/exec" in fake.calls[0]["url"]
    assert "/v1/sessions/sess-1/files:upload" in fake.calls[1]["url"]
    assert fake.calls[1]["data"] == {"path": "/workspace/a/b.txt"}


def test_write_file_refuses_when_read_only(rig) -> None:
    fake, executor, _bridge, _cfg = rig
    ro_bridge = WorkstationFileBridge(
        executor._client, executor, session_id="sess-1", token="tok", config=_config(access_level="ro")
    )

    with pytest.raises(WorkstationAccessDenied):
        ro_bridge.write_file("a.txt", b"x")

    assert fake.calls == []


def test_mkdirp_remove_rename_refuse_when_read_only(rig) -> None:
    fake, executor, _bridge, _cfg = rig
    ro_bridge = WorkstationFileBridge(
        executor._client, executor, session_id="sess-1", token="tok", config=_config(access_level="ro")
    )

    with pytest.raises(WorkstationAccessDenied):
        ro_bridge.mkdirp("a")
    with pytest.raises(WorkstationAccessDenied):
        ro_bridge.remove("a")
    with pytest.raises(WorkstationAccessDenied):
        ro_bridge.rename("a", "b")
    assert fake.calls == []


def test_stat_returns_none_for_not_found(rig) -> None:
    fake, _executor, bridge, _config = rig
    fake.responses = [FakeResponse(404, {"code": "not-found", "message": "no such path"})]

    assert bridge.stat("missing.txt") is None


def test_stat_returns_none_for_empty_node(rig) -> None:
    fake, _executor, bridge, _config = rig
    fake.responses = [FakeResponse(200, {})]

    assert bridge.stat("weird.txt") is None


def test_stat_returns_file_stat_for_existing_file(rig) -> None:
    fake, _executor, bridge, _config = rig
    fake.responses = [FakeResponse(200, {"path": "/workspace/a.txt", "kind": "file", "bytes": 12, "sha256": "abc"})]

    result = bridge.stat("a.txt")

    assert result is not None
    assert result.kind == "file"
    assert result.bytes == 12
    assert result.sha256 == "abc"


def test_stat_propagates_other_api_errors(rig) -> None:
    fake, _executor, bridge, _config = rig
    fake.responses = [FakeResponse(500, {"code": "internal", "message": "boom"})] * 3

    with pytest.raises(WorkstationApiError):
        bridge.stat("a.txt")


def test_mkdirp_root_itself_is_a_noop(rig) -> None:
    fake, _executor, bridge, _config = rig

    bridge.mkdirp("/workspace")

    assert fake.calls == []


def test_run_shell_helper_raises_on_nonzero_exit(rig) -> None:
    fake, _executor, bridge, _config = rig
    fake.responses = [FakeResponse(200, {"exitCode": 1, "stdout": "", "stderr": "no such file", "timedOut": False})]

    with pytest.raises(WorkstationProtocolError):
        bridge.remove("missing.txt")


def test_readdir_treats_not_found_as_empty_listing(rig) -> None:
    fake, _executor, bridge, _config = rig
    fake.responses = [FakeResponse(404, {"code": "not-found", "message": "no such dir"})]

    result = bridge.readdir("nope")

    assert result == {"path": "/workspace/nope", "kind": "dir", "children": []}
