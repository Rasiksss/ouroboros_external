"""Safe receipts and optional artifacts for compacted tool results."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import shutil
from dataclasses import dataclass, field
from typing import Any

from ouroboros.agent.task_ledger import safe_args_hash
from ouroboros.storage.blob.keys import safe_key_segment
from ouroboros.storage.settings.store import load_settings

_FREE_TEXT_KEYS = frozenset(
    {
        "content",
        "description",
        "query",
        "text",
        "prompt",
        "command",
        "script",
        "message",
        "body",
    }
)
_SAFE_VALUE_KEYS = frozenset(
    {
        "path",
        "dir",
        "file_path",
        "skill",
        "skill_name",
        "bucket",
        "task_id",
        "parent_task_id",
        "root_task_id",
        "url",
        "action",
    }
)
_SECRET_RE = re.compile(r"(?i)(sk-[A-Za-z0-9_-]{8,}|secret[_-]?token|api[_-]?key|password|bearer\s+[A-Za-z0-9._-]+)")
_TASK_QUEUED_RE = re.compile(r"\bqueued\s+([A-Za-z0-9_.-]{3,80})\b", re.IGNORECASE)


@dataclass
class ToolResultReplacementState:
    """Keeps compaction receipts stable across repeated micro-compaction runs."""

    receipts: dict[str, str] = field(default_factory=dict)


def _result_hash(result: Any) -> tuple[str, int]:
    data = str(result or "").encode("utf-8", errors="replace")
    return hashlib.sha256(data).hexdigest(), len(data)


def _hash_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def _safe_arg_parts(args: dict[str, Any] | None) -> list[str]:
    parts: list[str] = []
    for key in sorted((args or {}).keys()):
        key_text = str(key)
        value = (args or {}).get(key)
        low = key_text.lower()
        if low in _FREE_TEXT_KEYS:
            value_text = str(value or "")
            if low in {"command", "script", "query", "prompt", "text", "description"}:
                parts.append(f"{key_text}_sha256={_hash_text(value_text)}")
            else:
                parts.append(f"{key_text}_len={len(value_text)}")
            continue
        if low not in _SAFE_VALUE_KEYS:
            continue
        value_text = str(value or "")
        if not value_text:
            continue
        if _SECRET_RE.search(value_text) or len(value_text) > 180:
            parts.append(f"{key_text}_sha256={_hash_text(value_text)}")
        else:
            parts.append(f"{key_text}={value_text}")
    return parts


def _fact_summary(tool_name: str, result: Any) -> list[str]:
    text = " ".join(str(result or "").split())
    facts: list[str] = []
    if tool_name == "schedule_task":
        match = _TASK_QUEUED_RE.search(text)
        if match:
            facts.append("fact.status=queued")
            facts.append(f"fact.task_id={match.group(1)}")
    return facts


def _write_text_artifact(path: pathlib.Path, result: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(result or ""), encoding="utf-8")


def receipt_for_tool_result(
    *,
    drive_root: pathlib.Path,
    task_id: str,
    tool_call_id: str,
    tool_name: str,
    tool_args: dict[str, Any] | None,
    result: Any,
    spill_threshold: int = 10_000,
) -> str:
    """Return a prompt-safe receipt for a tool result, optionally writing artifacts."""
    root = pathlib.Path(drive_root)
    safe_task = safe_key_segment(task_id, "task")
    safe_call = safe_key_segment(tool_call_id, "call")
    result_sha, result_bytes = _result_hash(result)
    args_hash = safe_args_hash(tool_args or {})
    tool = str(tool_name or "unknown_tool").strip() or "unknown_tool"

    lines = [
        "[Tool receipt]",
        f"tool={tool}",
        f"tool_call_id={safe_call}",
        f"args_hash={args_hash}",
        f"sha256={result_sha}",
        f"bytes={result_bytes}",
    ]
    arg_parts = _safe_arg_parts(tool_args)
    if arg_parts:
        lines.append("args." + " ".join(arg_parts))
    facts = _fact_summary(tool, result)
    if facts:
        lines.append(" ".join(facts))

    should_spill = result_bytes > int(spill_threshold or 0)
    if should_spill and load_settings().OUROBOROS_TOOL_LOCAL_TMP_ARTIFACTS_ENABLED:
        artifact_path = root / "runtime" / "tool_artifacts" / "tmp" / safe_task / f"{safe_call}.txt"
        _write_text_artifact(artifact_path, result)

    if should_spill:
        lines.append("full result omitted")
    else:
        lines.append("result recorded")

    return "\n".join(lines)


def receipt_from_message(
    *,
    drive_root: pathlib.Path | None,
    task_id: str,
    message: dict[str, Any],
    messages: list[dict[str, Any]],
    tool_name: str,
    state: ToolResultReplacementState | None = None,
) -> str:
    """Build or reuse a stable receipt for an existing tool-result message."""
    call_id = str(message.get("tool_call_id") or "")
    if state is not None and call_id and call_id in state.receipts:
        return state.receipts[call_id]
    tool_args: dict[str, Any] = {}
    if call_id:
        for idx, item in enumerate(messages):
            if item is message:
                for prev in reversed(messages[:idx]):
                    if prev.get("role") != "assistant":
                        continue
                    for tc in prev.get("tool_calls") or []:
                        if tc.get("id") == call_id:
                            raw_args = tc.get("function", {}).get("arguments") or "{}"
                            try:
                                parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                                if isinstance(parsed, dict):
                                    tool_args = parsed
                            except Exception:
                                tool_args = {}
                            break
                    break
                break
    receipt = receipt_for_tool_result(
        drive_root=pathlib.Path(drive_root or "."),
        task_id=task_id,
        tool_call_id=call_id,
        tool_name=tool_name,
        tool_args=tool_args,
        result=message.get("content") or "",
        spill_threshold=0,
    )
    if state is not None and call_id:
        state.receipts[call_id] = receipt
    return receipt


def cleanup_tmp_tool_artifacts(drive_root: pathlib.Path, *, task_id: str = "") -> int:
    """Delete local debug spill artifacts for a task or all tmp tool artifacts."""
    root = pathlib.Path(drive_root) / "runtime" / "tool_artifacts" / "tmp"
    target = root / safe_key_segment(task_id, "task") if task_id else root
    if not target.exists():
        return 0
    if target.is_file():
        target.unlink(missing_ok=True)
        return 1
    deleted = sum(1 for path in target.rglob("*") if path.is_file())
    shutil.rmtree(target, ignore_errors=True)
    return deleted


__all__ = [
    "ToolResultReplacementState",
    "cleanup_tmp_tool_artifacts",
    "receipt_for_tool_result",
    "receipt_from_message",
]
