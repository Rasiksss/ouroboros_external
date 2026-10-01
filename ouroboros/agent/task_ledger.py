"""Bounded task ledger and repeat guard for tool execution."""

from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

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
_SECRET_RE = re.compile(r"(?i)(sk-[A-Za-z0-9_-]{8,}|secret|token|password|private)")


def _normalized_args(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key in sorted(value):
            raw = value[key]
            key_text = str(key)
            if key_text.lower() in _FREE_TEXT_KEYS:
                raw_text = str(raw or "")
                out[key_text] = {
                    "len": len(raw_text),
                    "sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:16],
                }
            else:
                out[key_text] = _normalized_args(raw)
        return out
    if isinstance(value, list):
        return [_normalized_args(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        text = str(value)
        if len(text) > 200 or _SECRET_RE.search(text):
            return {
                "len": len(text),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            }
        return value
    return str(type(value).__name__)


def safe_args_hash(args: dict[str, Any] | None) -> str:
    """Return a stable hash for tool args without embedding raw free text."""
    normalized = _normalized_args(args or {})
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _failure_key(tool_name: str, args: dict[str, Any] | None, failure_class: str) -> tuple[str, str, str]:
    return (
        str(tool_name or "").strip(),
        safe_args_hash(args or {}),
        " ".join(str(failure_class or "").split())[:120],
    )


def _safe_failure_class(value: str) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "unknown"
    if _SECRET_RE.search(text):
        return "redacted"
    return text[:120]


@dataclass
class _LedgerEntry:
    tool_name: str
    args_hash: str
    status: str
    failure_class: str
    count: int = 0


class TaskLedger:
    """Tiny bounded summary of repeated tool outcomes."""

    def __init__(self, max_chars: int = 2000) -> None:
        self.max_chars = max(120, int(max_chars or 2000))
        self._entries: OrderedDict[tuple[str, str, str, str], _LedgerEntry] = OrderedDict()

    def record_tool_result(
        self,
        *,
        tool_name: str,
        args_hash: str,
        status: str,
        failure_class: str = "",
        short_outcome: str = "",
    ) -> None:
        del short_outcome
        safe_tool = str(tool_name or "").strip()[:80]
        safe_args_hash = str(args_hash or "").strip()[:64]
        safe_status = str(status or "unknown").strip()[:80]
        safe_failure = _safe_failure_class(failure_class or safe_status)
        key = (safe_tool, safe_args_hash, safe_status, safe_failure)
        entry = self._entries.get(key)
        if entry is None:
            entry = _LedgerEntry(safe_tool, safe_args_hash, safe_status, safe_failure, 0)
            self._entries[key] = entry
        entry.count += 1
        self._entries.move_to_end(key)
        while len(self.render()) > self.max_chars and len(self._entries) > 1:
            self._entries.popitem(last=False)

    def render(self) -> str:
        if not self._entries:
            return ""
        lines = ["Tool outcome ledger:"]
        for entry in self._entries.values():
            suffix = f" x{entry.count}" if entry.count > 1 else ""
            line = (
                f"- {entry.tool_name} status={entry.status} "
                f"failure={entry.failure_class} args={entry.args_hash}{suffix}"
            )
            lines.append(line)
        text = "\n".join(lines)
        if len(text) <= self.max_chars:
            return text
        return text[: max(0, self.max_chars - 28)].rstrip() + "\n... (ledger truncated)"


class RepeatToolGuard:
    """Block repeated identical failed tool calls until success or user input."""

    def __init__(self, block_after: int = 3) -> None:
        self.block_after = max(2, int(block_after or 3))
        self._failures: dict[tuple[str, str, str], int] = {}

    def preflight(self, tool_name: str, args: dict[str, Any] | None) -> str:
        for (seen_tool, _args_hash, failure_class), count in self._failures.items():
            if (
                seen_tool == str(tool_name or "").strip()
                and _args_hash == safe_args_hash(args or {})
                and count >= self.block_after - 1
            ):
                return (
                    "⚠️ TOOL_REPEAT_BLOCKED: repeated failing tool call was not executed. "
                    f"Last failure class: {failure_class}. "
                    "Try a different approach or ask the user for new input."
                )
        return ""

    def record_failure(self, tool_name: str, args: dict[str, Any] | None, failure_class: str) -> str:
        # A disabled tool fails identically regardless of arguments, so repeats
        # collapse onto one key; otherwise varying args would evade the guard.
        repeat_args = None if str(failure_class or "").strip() == "TOOL_NOT_ENABLED" else args
        key = _failure_key(tool_name, repeat_args, failure_class)
        if key[2] in {"SEND_FILE_DELIVERY_UNKNOWN", "SEND_IMAGE_DELIVERY_UNKNOWN", "A2A_SUBMISSION_UNKNOWN"}:
            self._failures[key] = self.block_after
            return "⚠️ Delivery status is unknown; an identical automatic retry will be blocked."
        count = self._failures.get(key, 0) + 1
        self._failures[key] = count
        if count >= self.block_after:
            return (
                "⚠️ TOOL_REPEAT_BLOCKED: repeated failing tool call was not executed. "
                f"Failure class: {key[2]}. "
                "Try a different approach or ask the user for new input."
            )
        if count == self.block_after - 1:
            return (
                "⚠️ REPEAT_TOOL_WARNING: this tool call failed repeatedly with "
                f"failure class {key[2]}. The next identical failed call will be blocked."
            )
        return ""

    def mark_success(self, tool_name: str, args: dict[str, Any] | None) -> None:
        tool = str(tool_name or "").strip()
        args_hash = safe_args_hash(args or {})
        for key in list(self._failures):
            if key[0] == tool and key[1] == args_hash:
                self._failures.pop(key, None)

    def mark_user_input(self) -> None:
        self._failures.clear()


__all__ = ["RepeatToolGuard", "TaskLedger", "safe_args_hash"]
