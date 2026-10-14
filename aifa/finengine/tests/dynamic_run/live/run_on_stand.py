"""Record one live DynamicRun execution inside the dev backend container.

The script is intentionally test-only.  It patches the shared model gateway and
the station client only for this process, then prints one JSON object to stdout.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import sys
import time
import uuid
from dataclasses import asdict, is_dataclass
from typing import Any

from finengine.ingestion.document_store import get_store
from finengine.llm import openrouter, pricing
from finengine.llm.openclaw import set_web_search
from finengine.skills.atoms.intent import IngressContext, parse_message
from finengine.skills.production_runtime import execute_routed_goal
from finengine.skills.spec import sealed_room
from finengine.store.blobs import document_text


MODEL_CALLS: list[dict[str, Any]] = []
ROOM_CALLS: list[dict[str, Any]] = []
ORIGINAL_COMPLETE_WITH_USAGE = openrouter.complete_with_usage
ORIGINAL_OPEN_ROOM = getattr(sealed_room, "_open_room", None)
ORIGINAL_EXEC = getattr(sealed_room, "_exec", None)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def query_from_env() -> str:
    encoded = require_env("AIFA_LIVE_QUERY_B64")
    return base64.b64decode(encoded).decode("utf-8").strip()


def model_role(system: str, stack: list[inspect.FrameInfo]) -> str:
    lowered = system.lower()
    if "планировщик aifa-claw" in lowered:
        return "planner"
    if "автор временных атомов" in lowered:
        return "author"
    if any(frame.filename.replace("\\", "/").endswith("/skills/atoms/intent.py") for frame in stack):
        return "intent"
    return "atom"


async def recorded_complete(*args, **kwargs) -> str:
    prompt = str(kwargs.get("prompt", args[0] if args else ""))
    system = str(kwargs.get("system") or "")
    stack = inspect.stack(context=0)
    role = model_role(system, stack)
    started = time.monotonic()
    record: dict[str, Any] = {
        "index": len(MODEL_CALLS) + 1,
        "role": role,
        "system": system,
        "prompt": prompt,
        "system_chars": len(system),
        "prompt_chars": len(prompt),
    }
    print(f"model_call_start role={role} index={record['index']}", file=sys.stderr, flush=True)
    try:
        answer, usage = await ORIGINAL_COMPLETE_WITH_USAGE(*args, **kwargs)
    except Exception as exc:
        record.update(
            elapsed_s=round(time.monotonic() - started, 3),
            error=f"{type(exc).__name__}: {exc}",
        )
        MODEL_CALLS.append(record)
        print(f"model_call_error role={role} index={record['index']}", file=sys.stderr, flush=True)
        raise
    record.update(
        elapsed_s=round(time.monotonic() - started, 3),
        response=answer,
        usage=usage,
        cost_usd=pricing.cost_usd(usage),
    )
    MODEL_CALLS.append(record)
    print(f"model_call_done role={role} index={record['index']}", file=sys.stderr, flush=True)
    return answer


def recorded_open_room(*args, **kwargs):
    started = time.monotonic()
    try:
        return ORIGINAL_OPEN_ROOM(*args, **kwargs)
    finally:
        ROOM_CALLS.append({"phase": "create", "elapsed_s": round(time.monotonic() - started, 3)})


def recorded_exec(*args, **kwargs):
    started = time.monotonic()
    try:
        return ORIGINAL_EXEC(*args, **kwargs)
    finally:
        ROOM_CALLS.append({"phase": "exec", "elapsed_s": round(time.monotonic() - started, 3)})


def jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def totals() -> dict[str, Any]:
    usage = {"in": 0, "out": 0, "cached": 0}
    cost = 0.0
    for call in MODEL_CALLS:
        item = call.get("usage") or {}
        for key in usage:
            usage[key] += int(item.get(key, 0) or 0)
        cost += float(call.get("cost_usd", 0.0) or 0.0)
    return {"usage": usage, "cost_usd": round(cost, 6)}


def install_recorders() -> None:
    openrouter.complete = recorded_complete
    if ORIGINAL_OPEN_ROOM is not None:
        sealed_room._open_room = recorded_open_room
    if ORIGINAL_EXEC is not None:
        sealed_room._exec = recorded_exec


def main() -> int:
    doc_id = require_env("AIFA_LIVE_DOC_ID")
    owner_id = require_env("AIFA_LIVE_OWNER_ID")
    query = query_from_env()
    label = os.environ.get("AIFA_LIVE_LABEL", "live").strip() or "live"
    case_id = os.environ.get("AIFA_LIVE_CASE_ID", "").strip() or f"p6-{label}-{uuid.uuid4().hex[:10]}"

    meta = get_store().get_document(doc_id)
    if meta is None:
        raise RuntimeError(f"document not found: {doc_id}")
    if str(meta.owner_id) != owner_id:
        raise RuntimeError(f"document owner mismatch for {doc_id}")
    text = document_text(doc_id)
    if not text:
        raise RuntimeError(f"document text is empty: {doc_id}")

    install_recorders()
    # Тумблер «Поиск в интернете»: как у ручки чата; AIFA_LIVE_WEB_SEARCH=0 — прогон с выключенным.
    web_search = os.environ.get("AIFA_LIVE_WEB_SEARCH", "1").strip() != "0"
    set_web_search(web_search)
    events: list[dict[str, Any]] = []
    started = time.monotonic()
    decision = asyncio.run(
        parse_message(
            query,
            context=IngressContext(documents=((doc_id, True),), ui_role="other"),
            allow_answer=False,
        )
    )
    if decision.route != "plan" or decision.goal is None:
        raise RuntimeError(f"intent did not produce a plan: {decision.route}")
    diagnostics: dict[str, Any] = {}
    outcome = execute_routed_goal(
        owner_id=owner_id,
        case_id=case_id,
        document_id=doc_id,
        document_text=text,
        goal=decision.goal,
        event_sink=events.append,
        diagnostics=diagnostics,
    )
    elapsed = round(time.monotonic() - started, 3)
    manifest = outcome.manifest if outcome is not None else {}
    dynamic = manifest.get("dynamic_run") or {}
    result = {
        "schema_version": 1,
        "label": label,
        "case_id": case_id,
        "document": {
            "doc_id": doc_id,
            "owner_id": owner_id,
            "filename": meta.filename,
            "sha256": meta.sha256,
            "text_chars": len(text),
        },
        "query": query,
        "web_search": web_search,
        "intent": jsonable(decision),
        "diagnostics": diagnostics,
        "model_calls": MODEL_CALLS,
        "room_calls": ROOM_CALLS,
        "events": jsonable(events),
        "manifest": jsonable(manifest),
        "summary": {
            "status": dynamic.get("status") or (manifest.get("error") or {}).get("code") or "no_outcome",
            "moves": len(dynamic.get("moves") or ()),
            "author_attempts": sum(call["role"] == "author" for call in MODEL_CALLS),
            "model_calls": len(MODEL_CALLS),
            "elapsed_s": elapsed,
            **totals(),
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def failed_result(exc: Exception) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "label": os.environ.get("AIFA_LIVE_LABEL", "live"),
        "query": query_from_env(),
        "model_calls": MODEL_CALLS,
        "room_calls": ROOM_CALLS,
        "error": f"{type(exc).__name__}: {exc}",
        "summary": {
            "status": "harness_error",
            "moves": 0,
            "author_attempts": sum(call["role"] == "author" for call in MODEL_CALLS),
            "model_calls": len(MODEL_CALLS),
            **totals(),
        },
    }


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps(failed_result(error), ensure_ascii=False, indent=2, default=str))
        raise SystemExit(1) from None
