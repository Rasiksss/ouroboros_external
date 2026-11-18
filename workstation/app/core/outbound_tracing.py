from __future__ import annotations

import os
from uuid import uuid4


def runtime_agent_id() -> str:
    agent_id = os.getenv("AGENT_ID", "").strip()
    if not agent_id:
        raise RuntimeError("AGENT_ID environment variable is not set")
    return agent_id


def outbound_tracing_headers() -> dict[str, str]:
    return {
        "X-Agent-Id": runtime_agent_id(),
        "X-Trace-Id": str(uuid4()),
    }


def add_botocore_tracing_headers(model, params, request_signer, **kwargs) -> None:
    headers = params.setdefault("headers", {})
    headers["X-Agent-Id"] = runtime_agent_id()
    headers["X-Trace-Id"] = str(uuid4())
