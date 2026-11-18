#!/usr/bin/env python3
"""Manual workstation API smoke run.

The script intentionally exercises success and failure paths so container logs
show HTTP, session, policy, runtime, wrapper, exec, stream and file diagnostics.
It uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib import error, request

DEFAULT_BASE_URL = "http://localhost:18087"
DEFAULT_SESSION_PREFIX = "manual-smoke"
REDACTED = "[redacted]"


@dataclass
class ApiResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    elapsed_ms: int
    request_id: str

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


class SmokeClient:
    def __init__(self, base_url: str, *, pause_sec: float = 0.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.pause_sec = pause_sec

    def get(
        self,
        path: str,
        *,
        label: str,
        headers: Mapping[str, str] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> ApiResponse:
        return self.request("GET", path, label=label, headers=headers, expected=expected)

    def post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        label: str,
        headers: Mapping[str, str] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> ApiResponse:
        body = json.dumps(payload).encode("utf-8")
        return self.request(
            "POST",
            path,
            label=label,
            body=body,
            headers={"Content-Type": "application/json", **dict(headers or {})},
            expected=expected,
        )

    def post_multipart_upload(
        self,
        path: str,
        *,
        virtual_path: str,
        filename: str,
        content: bytes,
        label: str,
        headers: Mapping[str, str],
        expected: tuple[int, ...] = (200,),
    ) -> ApiResponse:
        boundary = f"----workstation-smoke-{uuid.uuid4().hex}"
        body = _multipart_body(
            boundary=boundary,
            fields={"path": virtual_path},
            files={"file": (filename, "text/plain", content)},
        )
        return self.request(
            "POST",
            path,
            label=label,
            body=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                **headers,
            },
            expected=expected,
        )

    def stream_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        label: str,
        headers: Mapping[str, str],
        expected: tuple[int, ...] = (200,),
    ) -> list[str]:
        request_id = _request_id(label)
        req = request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Request-Id": request_id,
                **headers,
            },
        )
        print(f"\n=> POST {path} [{label}] request_id={request_id}")
        started = time.perf_counter()
        lines: list[str] = []
        try:
            with request.urlopen(req, timeout=30) as response:
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                status = response.status
                print(f"<= {status} {elapsed_ms}ms")
                if status not in expected:
                    raise SystemExit(f"unexpected status for {label}: {status}, expected {expected}")
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                    lines.append(line)
                    print(f"   stream {line}")
        except error.HTTPError as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            body = exc.read()
            print(f"<= {exc.code} {elapsed_ms}ms")
            _print_body(body, dict(exc.headers))
            if exc.code not in expected:
                raise
        self._pause()
        return lines

    def request(
        self,
        method: str,
        path: str,
        *,
        label: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> ApiResponse:
        request_id = _request_id(label)
        req = request.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={
                "X-Request-Id": request_id,
                **dict(headers or {}),
            },
        )
        print(f"\n=> {method} {path} [{label}] request_id={request_id}")
        if body and req.headers.get("Content-type") == "application/json":
            _print_json(json.loads(body.decode("utf-8")))
        started = time.perf_counter()
        try:
            with request.urlopen(req, timeout=30) as response:
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                data = response.read()
                api_response = ApiResponse(
                    status=response.status,
                    headers=dict(response.headers),
                    body=data,
                    elapsed_ms=elapsed_ms,
                    request_id=request_id,
                )
        except error.HTTPError as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            api_response = ApiResponse(
                status=exc.code,
                headers=dict(exc.headers),
                body=exc.read(),
                elapsed_ms=elapsed_ms,
                request_id=request_id,
            )

        print(f"<= {api_response.status} {api_response.elapsed_ms}ms")
        _print_body(api_response.body, api_response.headers)
        if api_response.status not in expected:
            raise SystemExit(f"unexpected status for {label}: {api_response.status}, expected {expected}")
        self._pause()
        return api_response

    def _pause(self) -> None:
        if self.pause_sec > 0:
            time.sleep(self.pause_sec)


def main() -> int:
    args = _parse_args()
    session_id = args.session_id or f"{DEFAULT_SESSION_PREFIX}-{uuid.uuid4().hex[:8]}"
    client = SmokeClient(args.base_url, pause_sec=args.pause)

    print(f"base_url={args.base_url.rstrip('/')}")
    print(f"session_id={session_id}")
    print("watch logs:")
    print(f"  docker logs -f {args.container_name}")

    client.get("/version", label="version")
    client.get("/health/liveness", label="liveness")
    client.get(
        f"/v1/sessions/{session_id}",
        label="auth-error-missing-bearer",
        headers={"X-Session-Id": session_id},
        expected=(401,),
    )

    created = client.post_json(
        "/v1/sessions",
        {
            "profile": "default",
            "networkEnabled": False,
            "idleTimeoutSec": 900,
            "absoluteTtlSec": 3600,
        },
        label="session-create",
        headers={"X-Session-Id": session_id},
        expected=(200, 201),
    )
    token = created.json()["token"]
    auth_headers = {"Authorization": f"Bearer {token}", "X-Session-Id": session_id}

    client.get(f"/v1/sessions/{session_id}", label="session-get", headers=auth_headers)

    client.post_json(
        f"/v1/sessions/{session_id}/exec",
        {
            "command": ["bash", "-lc", "pwd && echo workstation-ok"],
            "cwd": "/workspace",
            "env": {"LANG": "C.UTF-8"},
            "timeoutSec": 30,
        },
        label="exec-success",
        headers=auth_headers,
    )

    client.post_json(
        f"/v1/sessions/{session_id}/exec",
        {
            "command": ["bash", "-lc", "echo stderr-line >&2; exit 7"],
            "cwd": "/workspace",
            "env": {},
            "timeoutSec": 30,
        },
        label="exec-nonzero",
        headers=auth_headers,
    )

    client.post_json(
        f"/v1/sessions/{session_id}/exec",
        {
            "command": ["/missing-command"],
            "cwd": "/workspace",
            "env": {},
            "timeoutSec": 30,
        },
        label="wrapper-execve-failure",
        headers=auth_headers,
    )

    client.post_json(
        f"/v1/sessions/{session_id}/exec",
        {
            "command": ["bash", "-lc", "sudo true"],
            "cwd": "/workspace",
            "env": {},
            "timeoutSec": 30,
        },
        label="policy-deny-command-token",
        headers=auth_headers,
        expected=(403,),
    )

    client.stream_json(
        f"/v1/sessions/{session_id}/exec:stream",
        {
            "command": ["bash", "-lc", "echo stream-one; echo stream-two >&2"],
            "cwd": "/workspace",
            "env": {},
            "timeoutSec": 30,
        },
        label="exec-stream",
        headers=auth_headers,
    )

    client.post_multipart_upload(
        f"/v1/sessions/{session_id}/files:upload",
        virtual_path="/workspace/manual-smoke.txt",
        filename="manual-smoke.txt",
        content=b"hello from manual smoke\n",
        label="file-upload",
        headers=auth_headers,
    )

    client.post_json(
        f"/v1/sessions/{session_id}/files:list",
        {
            "path": "/workspace",
            "recursive": True,
            "maxDepth": 2,
            "includeSha256": True,
        },
        label="file-list",
        headers=auth_headers,
    )

    client.post_json(
        f"/v1/sessions/{session_id}/files:download",
        {"path": "/workspace/manual-smoke.txt"},
        label="file-download",
        headers=auth_headers,
    )

    client.post_json(
        f"/v1/sessions/{session_id}:terminate",
        {},
        label="session-terminate",
        headers=auth_headers,
    )

    print("\nmanual smoke completed")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Exercise workstation API endpoints and log paths.")
    parser.add_argument("--base-url", default=os.getenv("WORKSTATION_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--session-id", default=os.getenv("WORKSTATION_SESSION_ID"))
    parser.add_argument("--pause", type=float, default=float(os.getenv("WORKSTATION_SMOKE_PAUSE", "0")))
    parser.add_argument(
        "--container-name",
        default=os.getenv("WORKSTATION_CONTAINER", "workstation-add-logs-swagger"),
        help="Only printed as a hint for docker logs -f.",
    )
    return parser.parse_args()


def _request_id(label: str) -> str:
    normalized = "".join(char if char.isalnum() or char == "-" else "-" for char in label.lower())
    return f"manual-{normalized}-{uuid.uuid4().hex[:8]}"


def _multipart_body(
    *,
    boundary: str,
    fields: Mapping[str, str],
    files: Mapping[str, tuple[str, str, bytes]],
) -> bytes:
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode(),
                b"\r\n",
            ]
        )
    for name, (filename, content_type, content) in files.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                content,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks)


def _print_body(body: bytes, headers: Mapping[str, str]) -> None:
    if not body:
        return
    content_type = headers.get("content-type", "")
    if "application/json" in content_type:
        _print_json(json.loads(body.decode("utf-8")))
        return
    text = body.decode("utf-8", errors="replace")
    if len(text) > 1200:
        text = f"{text[:1200]}...[truncated {len(text) - 1200} chars]"
    print(text.rstrip())


def _print_json(value: Any) -> None:
    print(json.dumps(_sanitize(value), ensure_ascii=False, indent=2, sort_keys=True))


def _sanitize(value: Any, *, key: str | None = None) -> Any:
    if key is not None and any(marker in key.lower() for marker in ("authorization", "token", "password", "secret")):
        return REDACTED
    if isinstance(value, dict):
        return {item_key: _sanitize(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130) from None
