"""Send one live request through the dev chat API and record the UI event stream."""

from __future__ import annotations

import base64
import http.cookiejar
import json
import os
import time
import urllib.request


BASE = os.environ.get("AIFA_LIVE_API", "http://127.0.0.1:8000").rstrip("/")


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def json_request(opener, method: str, path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"{BASE}{path}", method=method,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with opener.open(request, timeout=60) as response:
        return json.loads(response.read())


def main() -> int:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    json_request(
        opener, "POST", "/auth/login",
        {"username": require_env("FINENGINE_ADMIN_USERNAME"),
         "password": require_env("FINENGINE_ADMIN_PASSWORD")},
    )
    query = base64.b64decode(require_env("AIFA_LIVE_QUERY_B64")).decode("utf-8")
    doc_id = require_env("AIFA_LIVE_DOC_ID")
    started = time.monotonic()
    turn = json_request(opener, "POST", "/chat/turns", {
        "session_id": "", "message": query, "doc_ids": [doc_id], "arch": "aifa_gateway",
        "project_id": "", "role": "other", "pinned_tool": "", "pinned_tool_args": {},
        "web_search": False, "active_doc_id": doc_id,
    })
    run_id = turn["run_id"]
    request = urllib.request.Request(f"{BASE}/chat/runs/{run_id}/events")
    events = []
    with opener.open(request, timeout=1200) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            event = json.loads(line[5:].strip())
            events.append(event)
            if event.get("event_type") == "completed":
                break
    graphs = [
        event["legacy_event"] for event in events
        if event.get("event_type") == "legacy"
        and (event.get("legacy_event") or {}).get("type") == "plan_graph"
    ]
    completed = next(
        (event for event in reversed(events) if event.get("event_type") == "completed"), {}
    )
    print(json.dumps({
        "query": query,
        "doc_id": doc_id,
        "run_id": run_id,
        "elapsed_s": round(time.monotonic() - started, 3),
        "terminal_status": completed.get("status", "stream_ended"),
        "plan_graphs": graphs,
        "events": events,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
