"""Opt-in planner replay against an OpenAI-compatible endpoint; no task execution."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TypedDict

import httpx
import jsonschema


class PlannerCase(TypedDict):
    messages: list[dict[str, str]]
    schema: dict[str, object]


def load_case(path: Path) -> PlannerCase:
    """Reject malformed inputs before making any paid requests."""
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise SystemExit("Case must be a JSON object")
    messages = value.get("messages")
    schema = value.get("schema")
    if (
        not isinstance(messages, list)
        or not messages
        or any(
            not isinstance(message, dict)
            or not isinstance(message.get("role"), str)
            or not isinstance(message.get("content"), str)
            for message in messages
        )
        or not isinstance(schema, dict)
    ):
        raise SystemExit(
            "Case requires nonempty messages with role/content and a schema"
        )
    try:
        jsonschema.validators.validator_for(schema).check_schema(schema)
    except jsonschema.SchemaError:
        raise SystemExit("Case contains an invalid JSON Schema") from None
    return {"messages": messages, "schema": schema}


@dataclass
class Sample:
    model: str
    attempt: int
    reasoning: str
    provider_requested: str
    provider: str = ""
    http: int = 0
    elapsed_sec: float = 0
    first_delta_sec: float | None = None
    first_content_sec: float | None = None
    finish_reason: str = ""
    text: str = ""
    usage: dict[str, object] = field(default_factory=dict)
    schema_valid: bool = False
    accepted: bool | None = None
    error: str = ""


def validate_contract(text: str) -> bool | None:
    """Use the runtime's validators when installed in this container."""
    try:
        # Optional dependency: engine deliberately does not import the Ouroboros runtime.
        from ouroboros.contracts.task_plan import AcceptPlan, PlanProposal, PlanState
    except ImportError:
        return None
    try:
        proposal = PlanProposal.model_validate_json(text)
        PlanState().accept(
            proposal,
            AcceptPlan(
                expected_version=0,
                proposal_task_id="benchmark",
                reason="Offline validation",
            ),
        )
    except ValueError:
        return False
    return True


async def measure(
    client: httpx.AsyncClient,
    slots: asyncio.Semaphore,
    args: argparse.Namespace,
    case: PlannerCase,
    model: str,
    attempt: int,
) -> Sample:
    async with slots:
        result = Sample(model, attempt, args.reasoning, args.provider or "auto")
        reasoning = (
            {"enabled": False}
            if args.reasoning == "none"
            else (
                {"enabled": True}
                if args.reasoning == "on"
                else {"effort": args.reasoning}
            )
        )
        body: dict[str, object] = {
            "model": model,
            "messages": case["messages"],
            "max_tokens": args.max_tokens,
            "reasoning": reasoning,
            "response_format": {"type": "json_object"},
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if args.provider:
            body["provider"] = {"only": [args.provider], "allow_fallbacks": False}
        headers = {
            "Authorization": "Bearer " + os.environ[args.key_env],
            "X-Trace-Id": "planner-benchmark-" + uuid.uuid4().hex,
            "X-Agent-Id": os.environ.get("AGENT_SYSTEM_CONFIGURATION_AGENT_ID")
            or "main",
        }
        started = time.monotonic()
        try:
            async with asyncio.timeout(args.timeout):
                async with client.stream(
                    "POST",
                    args.base_url.rstrip("/") + "/chat/completions",
                    json=body,
                    headers=headers,
                ) as response:
                    result.http = response.status_code
                    if result.http != 200:
                        result.error = "HTTP " + str(result.http)
                    else:
                        async for line in response.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            raw = line[6:].strip()
                            if raw == "[DONE]":
                                break
                            chunk = json.loads(raw)
                            result.provider = chunk.get("provider") or result.provider
                            result.usage = chunk.get("usage") or result.usage
                            if chunk.get("error"):
                                result.error = json.dumps(
                                    chunk["error"], ensure_ascii=False
                                )[:500]
                            for choice in chunk.get("choices", []):
                                delta = choice.get("delta", {})
                                elapsed = round(time.monotonic() - started, 3)
                                if (
                                    delta.get("reasoning") or delta.get("content")
                                ) and result.first_delta_sec is None:
                                    result.first_delta_sec = elapsed
                                if delta.get("content"):
                                    if result.first_content_sec is None:
                                        result.first_content_sec = elapsed
                                    result.text += delta["content"]
                                result.finish_reason = (
                                    choice.get("finish_reason") or result.finish_reason
                                )
        except TimeoutError:
            result.error = f"Wall timeout {args.timeout}s"
        except (httpx.HTTPError, ValueError) as error:
            result.error = type(error).__name__
        result.elapsed_sec = round(time.monotonic() - started, 3)
        try:
            jsonschema.validate(json.loads(result.text), case["schema"])
            result.schema_valid = True
        except (ValueError, jsonschema.ValidationError):
            result.schema_valid = False
        result.accepted = validate_contract(result.text)
        name = (
            re.sub(r"[^A-Za-z0-9_.-]", "_", model)
            + f"-{attempt}-{uuid.uuid4().hex[:8]}.json"
        )
        (args.out / name).write_text(
            json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    key: value
                    for key, value in asdict(result).items()
                    if key not in ("text", "usage")
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return result


async def run(args: argparse.Namespace) -> None:
    if os.environ.get("AIFA_LIVE_MODEL") != "1":
        raise SystemExit("Set AIFA_LIVE_MODEL=1 to enable live paid calls")
    if not args.base_url or not os.environ.get(args.key_env):
        raise SystemExit("Endpoint or API-key environment variable is missing")
    if min(args.repeat, args.concurrency, args.timeout, args.max_tokens) < 1:
        raise SystemExit(
            "Repeat, concurrency, timeout and token limit must be positive"
        )
    case = load_case(args.case)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "case.json").write_text(
        json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    slots = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(args.timeout, connect=15), trust_env=False
    ) as client:
        tasks = [
            measure(client, slots, args, case, model, attempt)
            for attempt in range(1, args.repeat + 1)
            for model in args.models
        ]
        await asyncio.gather(*tasks)


def validate_directory(directory: Path) -> None:
    """Validate previously saved replies inside the Ouroboros container."""
    failed = 0
    checked = 0
    for path in sorted(directory.glob("*.json")):
        if path.name == "case.json":
            continue
        result = json.loads(path.read_text(encoding="utf-8"))
        checked += 1
        accepted = validate_contract(result["text"])
        if accepted is None:
            raise SystemExit(
                "Ouroboros validators unavailable: use the Ouroboros container"
            )
        result["accepted"] = accepted
        path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(path.name, "ACCEPT" if accepted else "REJECT")
        failed += not accepted
    if not checked:
        raise SystemExit("No result files found")
    raise SystemExit(1 if failed else 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--validate",
        type=Path,
        help="Validate a saved result directory; no network calls",
    )
    parser.add_argument(
        "--case", type=Path, help="JSON containing messages and proposal schema"
    )
    parser.add_argument("--out", type=Path, default=Path("planner-results"))
    parser.add_argument("--models", nargs="+", default=["z-ai/glm-5.3"])
    parser.add_argument(
        "--reasoning", choices=("none", "low", "medium", "high", "on"), default="low"
    )
    parser.add_argument(
        "--provider", help="Provider slug; disables fallback for this request only"
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--max-tokens", type=int, default=14000)
    parser.add_argument(
        "--base-url", default=os.environ.get("FINENGINE_OPENROUTER_BASE_URL", "")
    )
    parser.add_argument("--key-env", default="OPENROUTER_API_KEY")
    args = parser.parse_args()
    if args.validate:
        validate_directory(args.validate)
    elif args.case:
        asyncio.run(run(args))
    else:
        parser.error("--case or --validate is required")


if __name__ == "__main__":
    main()
