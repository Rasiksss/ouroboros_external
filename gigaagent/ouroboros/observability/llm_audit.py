"""Opt-in full LLM inputs and task timings on an operator-mounted volume."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from ouroboros.observability.log_context import get_correlation_id, get_task_id

log = logging.getLogger(__name__)
_TASK: ContextVar[dict | None] = ContextVar("llm_audit_task", default=None)
_CALL: ContextVar[object | None] = ContextVar("llm_audit_call", default=None)
_STATS_LOCK = threading.Lock()
_INPUT_KEYS = ("messages", "model", "tools", "reasoning_effort", "max_tokens", "tool_choice", "temperature")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _segment(value: str) -> str:
    # Only generated/normalized path components; raw task IDs remain inside JSON.
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(value))[:100] or "unscoped"


def _write(path: Path, data: dict) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temporary, path)
    except Exception as exc:  # Audit storage must not replace the model's result or exception.
        log.warning("event=llm_audit_write_failed record_id=%s error_type=%s",
                    data["record_id"], type(exc).__name__)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class _Record:
    def __init__(self, root: str, kind: str, metadata: dict, inputs: dict):
        identity = uuid.uuid4().hex
        self.data = {"schema_version": 1, "service": "gigaagent", "kind": kind,
                     "record_id": identity, "started_at": _now(), "status": "running",
                     **metadata, "input": inputs}
        self.path = Path(root) / "gigaagent" / self.data["started_at"][:10] / _segment(
            metadata.get("task_id", "")
        ) / f"{kind}-{identity}.json"
        self.dispatched = None
        _write(self.path, self.data)
        log.info("event=%s_audit_start record_id=%s task_id=%s audit_file=%s",
                 kind, identity, metadata.get("task_id", ""), self.path)
        self.started = time.monotonic()

    def finish(self, result=None, error: BaseException | None = None, *, finished=None):
        finished = time.monotonic() if finished is None else finished
        duration = round((finished - self.started) * 1000, 3)
        status = "ok" if error is None else (
            "cancelled" if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, GeneratorExit)) else "error"
        )
        self.data.update(status=status, finished_at=_now(), duration_ms=duration)
        if error is not None:
            self.data["error_type"] = type(error).__name__
        if self.data["kind"] == "llm_call":
            self.data.update(
                pre_dispatch_duration_ms=(round((self.dispatched - self.started) * 1000, 3)
                                          if self.dispatched is not None else None),
                provider_duration_ms=(round((finished - self.dispatched) * 1000, 3)
                                      if self.dispatched is not None else None),
            )
            if error is None and isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
                self.data["usage"] = result[1]
            task = _TASK.get()
            if task is not None:
                with _STATS_LOCK:
                    task["llm_calls"] += 1
                    task["llm_duration_ms"] = round(task["llm_duration_ms"] + duration, 3)
                    task["llm_provider_duration_ms"] = round(
                        task["llm_provider_duration_ms"] + (self.data["provider_duration_ms"] or 0), 3
                    )
                    task["llm_errors"] += int(status != "ok")
        _write(self.path, self.data)
        log.info("event=%s_audited record_id=%s task_id=%s status=%s duration_ms=%.3f audit_file=%s",
                 self.data["kind"], self.data["record_id"], self.data.get("task_id", ""),
                 status, duration, self.path)


def mark_provider_dispatch(target: dict) -> None:
    """Called after concurrency admission, immediately before the provider adapter."""
    call = _CALL.get()
    if isinstance(call, _Record):
        call.dispatched = time.monotonic()
        call.data["provider"] = target.get("provider")
        call.data["resolved_model"] = target.get("resolved_model")
        call.data["dispatched_at"] = _now()


def _call_record(root, signature, args, kwargs):
    bound = signature.bind(*args, **kwargs)
    bound.apply_defaults()
    inputs = {key: bound.arguments[key] for key in _INPUT_KEYS if key in bound.arguments}
    inputs["tools"] = [tool.to_dict() for tool in inputs.get("tools") or []]
    # Snapshot before execution: mutable message/tool lists can change after return.
    inputs = json.loads(json.dumps(inputs, ensure_ascii=False))
    task = _TASK.get()
    metadata = dict(task["metadata"]) if task is not None else {
        "task_id": get_task_id(), "correlation_id": get_correlation_id(), "task_role": "unscoped",
    }
    return _Record(root, "llm_call", metadata, inputs)


def audit_llm(function):
    """Trace each facade call; retries/fallbacks re-enter and get distinct records."""
    signature = inspect.signature(function)

    def start(args, kwargs):
        try:
            return _call_record(os.environ["OUROBOROS_LLM_AUDIT_DIR"], signature, args, kwargs)
        except Exception as exc:
            log.warning("event=llm_audit_input_failed error_type=%s", type(exc).__name__)
            return None

    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def async_wrapper(*args, **kwargs):
            if not os.environ.get("OUROBOROS_LLM_AUDIT_DIR", "").strip():
                return await function(*args, **kwargs)
            record = await asyncio.to_thread(start, args, kwargs)
            token = _CALL.set(record)
            result, error = None, None
            try:
                result = await function(*args, **kwargs)
                return result
            except BaseException as exc:
                error = exc
                raise
            finally:
                try:
                    if record is not None:
                        # Do not count the executor queue for file I/O as model time.
                        await asyncio.to_thread(record.finish, result, error, finished=time.monotonic())
                finally:
                    _CALL.reset(token)
        return async_wrapper

    @wraps(function)
    def wrapper(*args, **kwargs):
        if not os.environ.get("OUROBOROS_LLM_AUDIT_DIR", "").strip():
            return function(*args, **kwargs)
        record = start(args, kwargs)
        token = _CALL.set(record)
        result, error = None, None
        try:
            result = function(*args, **kwargs)
            return result
        except BaseException as exc:
            error = exc
            raise
        finally:
            if record is not None:
                record.finish(result, error)
            _CALL.reset(token)
    return wrapper


def audit_task(function):
    """Measure one execution attempt, without changing task scheduling or terminal states."""
    signature = inspect.signature(function)

    @wraps(function)
    def wrapper(*args, **kwargs):
        root = os.environ.get("OUROBOROS_LLM_AUDIT_DIR", "").strip()
        if not root:
            return function(*args, **kwargs)
        task = signature.bind(*args, **kwargs).arguments["task"]
        scope = task.get("scope") or {}
        description = str(task.get("description") or task.get("text") or "")
        parent_id = str(task.get("parent_task_id") or "")
        role = "subtask" if parent_id else "orchestrator"
        for skill, label in (("aifa-plan-author", "planner"), ("aifa-atom-author", "atom_author")):
            if description.startswith(f"Навык: distribution:{skill}."):
                role = label
        metadata = {"task_id": str(task.get("id") or ""), "parent_task_id": parent_id,
                    "root_task_id": str(task.get("root_task_id") or parent_id or task.get("id") or ""),
                    "task_type": task.get("type", ""), "task_role": role,
                    "owner_id": scope.get("owner_id", ""), "agent_id": scope.get("agent_id", ""),
                    "trace_id": task.get("trace_id", ""), "correlation_id": get_correlation_id()}
        record = _Record(root, "task", metadata, {"description": description})
        record.data.update(llm_calls=0, llm_duration_ms=0.0, llm_provider_duration_ms=0.0, llm_errors=0)
        token = _TASK.set({"metadata": {**metadata, "task_attempt_id": record.data["record_id"]},
                           "llm_calls": 0, "llm_duration_ms": 0.0, "llm_provider_duration_ms": 0.0, "llm_errors": 0})
        result, error = None, None
        try:
            result = function(*args, **kwargs)
            if isinstance(result, list):
                for event in result:
                    if getattr(event, "task_id", None) == metadata["task_id"] and hasattr(event, "status"):
                        record.data["task_status"] = event.status
            return result
        except BaseException as exc:
            error = exc
            raise
        finally:
            stats = _TASK.get()
            record.data.update({key: stats[key] for key in (
                "llm_calls", "llm_duration_ms", "llm_provider_duration_ms", "llm_errors"
            )})
            record.finish(result, error)
            _TASK.reset(token)
    return wrapper
