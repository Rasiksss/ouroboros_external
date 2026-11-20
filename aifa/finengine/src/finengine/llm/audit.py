"""Full LLM inputs on a mounted volume; no response bodies or credentials."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

log = logging.getLogger(__name__)
_CONTEXT: ContextVar[dict] = ContextVar("aifa_llm_audit_context", default={})
_PARENT: ContextVar[str] = ContextVar("aifa_llm_audit_parent", default="")
_INPUT_KEYS = {
    "prompt", "system", "system_prompt", "model", "messages", "response_format", "retries",
    "max_output", "max_completion_tokens", "temperature", "seed", "pin_provider", "reasoning_effort",
    "omit_max_output", "extra_body", "stream", "stream_options",
}


@contextmanager
def atom_context(ctx, atom_name, args):
    extra = ctx.inputs.extra
    token = _CONTEXT.set({"run_id": ctx.run_id, "atom_name": atom_name,
                          "concepts": sorted({str(a.concept) for a in args if a.concept}),
                          "document_ids": sorted({str(a.entity_ref) for a in args if a.entity_ref}),
                          **{key: extra[key] for key in ("owner_id", "project_id", "trace_id", "agent_id") if key in extra}})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _write(path, data):
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temporary, path)
    except Exception as exc:  # Audit failures must preserve the model result/exception.
        log.warning("event=llm_audit_write_failed record_id=%s error_type=%s", data["record_id"], type(exc).__name__)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _usage(result):
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        return result[1]
    usage = getattr(result, "usage", None)
    if usage is not None:
        return {key: getattr(usage, key, None) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    return None


class _Record:
    def __init__(self, root, kind, function, arguments):
        inputs = {**arguments, **arguments.get("kwargs", {})}
        # Only known model-input fields: the SDK client and headers are excluded.
        payload = json.loads(json.dumps({key: value for key, value in inputs.items() if key in _INPUT_KEYS}))
        self.data = {"schema_version": 1, "service": "aifa", "kind": kind, "record_id": uuid.uuid4().hex,
                     "parent_call_id": _PARENT.get(), "function": function,
                     "started_at": _now(), "status": "running", **_CONTEXT.get(), "input": payload}
        header = inputs.get("extra_headers") or {}
        if header.get("X-Trace-Id"):
            self.data.setdefault("trace_id", header["X-Trace-Id"])
        identity = str(self.data.get("run_id") or self.data.get("trace_id") or "unscoped")
        segment = re.sub(r"[^A-Za-z0-9_-]", "_", identity)[:100] or "unscoped"
        self.path = Path(root) / "aifa" / self.data["started_at"][:10] / segment / f"{kind}-{self.data['record_id']}.json"
        _write(self.path, self.data)
        log.info("event=%s_audit_start record_id=%s audit_file=%s", kind, self.data["record_id"], self.path)
        self.started = time.monotonic()

    def finish(self, result, error, *, finished, usage=None):
        self.data.update(finished_at=_now(), duration_ms=round((finished - self.started) * 1000, 3),
                         status="ok" if error is None else (
                             "cancelled" if isinstance(error, (asyncio.CancelledError, GeneratorExit, KeyboardInterrupt)) else "error"))
        if error is not None:
            self.data["error_type"] = type(error).__name__
        tokens = usage if usage is not None else _usage(result)
        if tokens is not None:
            self.data["usage"] = tokens
        _write(self.path, self.data)
        log.info("event=%s_audited record_id=%s status=%s duration_ms=%.3f audit_file=%s",
                 self.data["kind"], self.data["record_id"], self.data["status"], self.data["duration_ms"], self.path)


def audit_llm(*, kind="llm_call"):
    """Decorate completion, native sync attempt, or a lazy streaming iterator."""
    def decorate(function):
        signature = inspect.signature(function)

        def start(args, kwargs):
            root = os.environ.get("FINENGINE_LLM_AUDIT_DIR", "").strip()
            if not root:
                return None
            try:
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                return _Record(root, kind, function.__qualname__, bound.arguments)
            except Exception as exc:
                log.warning("event=llm_audit_input_failed error_type=%s", type(exc).__name__)
                return None

        if inspect.isasyncgenfunction(function):
            @wraps(function)
            async def stream(*args, **kwargs):
                record = (await asyncio.to_thread(start, args, kwargs)
                          if os.environ.get("FINENGINE_LLM_AUDIT_DIR", "").strip() else None)
                source = function(*args, **kwargs)
                error, usage = None, None
                try:
                    while True:
                        token = _PARENT.set(record.data["record_id"]) if record is not None and kind == "llm_call" else None
                        try:
                            piece = await anext(source)
                        except StopAsyncIteration:
                            break
                        finally:
                            if token is not None:
                                _PARENT.reset(token)
                        usage = _usage(piece) or usage
                        yield piece
                except BaseException as exc:
                    error = exc
                    raise
                finally:
                    finished = time.monotonic()
                    try:
                        await source.aclose()
                    finally:
                        if record is not None:
                            await asyncio.to_thread(record.finish, None, error, finished=finished, usage=usage)
            return stream

        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def async_wrapper(*args, **kwargs):
                record = (await asyncio.to_thread(start, args, kwargs)
                          if os.environ.get("FINENGINE_LLM_AUDIT_DIR", "").strip() else None)
                token = _PARENT.set(record.data["record_id"]) if record is not None and kind == "llm_call" else None
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
                            await asyncio.to_thread(record.finish, result, error, finished=time.monotonic())
                    finally:
                        if token is not None:
                            _PARENT.reset(token)
            return async_wrapper

        @wraps(function)
        def wrapper(*args, **kwargs):
            record = start(args, kwargs)
            result, error = None, None
            try:
                result = function(*args, **kwargs)
                return result
            except BaseException as exc:
                error = exc
                raise
            finally:
                if record is not None:
                    record.finish(result, error, finished=time.monotonic())
        return wrapper
    return decorate
