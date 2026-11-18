"""Shared low-level utilities with no ouroboros.* imports."""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import json
import logging
import os
import pathlib
import subprocess
import threading
import uuid
from collections.abc import Callable, Collection, Coroutine
from typing import Any, TypeVar

import anyio

from new_core.identity import OwnerScope

log = logging.getLogger(__name__)

_log_sink: Callable[[dict[str, Any]], None] | None = None


def set_log_sink(fn: Callable[[dict[str, Any]], None] | None) -> None:
    global _log_sink
    _log_sink = fn


def emit_ui_log(obj: dict[str, Any]) -> None:
    """Send one event to the live UI log sink, if a client has installed one."""
    if _log_sink is None:
        return
    try:
        _log_sink(obj)
    except Exception:
        log.debug("Failed to emit live log event", exc_info=True)


_T = TypeVar("_T")


async def finish_file_io(operation: Coroutine[object, object, _T]) -> _T:
    """Keep filesystem resources alive until the worker settles, including on repeated cancellation."""
    pending = asyncio.create_task(operation)
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            while True:
                try:
                    await asyncio.shield(pending)
                    break
                except asyncio.CancelledError:
                    if pending.cancelled():
                        break
                except Exception:
                    log.warning("Cancelled file I/O failed", exc_info=True)
                    break
        raise


def utc_now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()


def file_sha256(path: pathlib.Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _configured_data_dir() -> pathlib.Path:
    from ouroboros.storage.settings.paths import load_path_settings

    return load_path_settings().data_dir.resolve()


def _drive_storage_for_path(path: pathlib.Path) -> Any | None:
    try:
        from ouroboros.storage.blob.drive import get_drive_storage

        resolved = pathlib.Path(path).resolve()
        drive_root = _configured_data_dir()
        if resolved == drive_root or drive_root in resolved.parents:
            return get_drive_storage(drive_root)
    except Exception:
        log.debug("Drive storage resolution failed for %s", path, exc_info=True)
    return None


def drive_aware_exists(owner_scope: OwnerScope, path: pathlib.Path) -> bool:
    """Return True when a path exists on its authoritative storage tier (S3 or file)."""
    drive = _drive_storage_for_path(path)
    if drive is not None:
        rel = drive.rel_from_path(pathlib.Path(path))
        if rel is not None:
            return bool(drive.path_exists(owner_scope, pathlib.Path(path)))
    return pathlib.Path(path).is_file()


def read_bytes(owner_scope: OwnerScope, path: pathlib.Path) -> bytes:
    """Read binary content via DriveStorage when under DATA_DIR."""
    drive = _drive_storage_for_path(path)
    if drive is not None:
        rel = drive.rel_from_path(pathlib.Path(path))
        if rel is not None:
            data = drive.read_bytes(owner_scope, pathlib.Path(path))
            if data is not None:
                return data
            if drive.path_exists(owner_scope, pathlib.Path(path)):
                return b""
            raise FileNotFoundError(path)
    return pathlib.Path(path).read_bytes()


def write_bytes(
    owner_scope: OwnerScope,
    path: pathlib.Path,
    data: bytes,
    *,
    content_type: str = "application/octet-stream",
) -> None:
    """Write binary content via DriveStorage when under DATA_DIR."""
    drive = _drive_storage_for_path(path)
    if drive is not None and drive.rel_from_path(pathlib.Path(path)) is not None:
        drive.write_bytes(owner_scope, pathlib.Path(path), data, content_type=content_type)
        return
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def read_text(owner_scope: OwnerScope, path: pathlib.Path) -> str:
    drive = _drive_storage_for_path(path)
    if drive is not None:
        rel = drive.rel_from_path(pathlib.Path(path))
        if rel is not None:
            stored = drive.read_text(owner_scope, pathlib.Path(path))
            if stored is not None:
                return stored
            if drive.exists_rel(owner_scope, rel):
                return ""
            raise FileNotFoundError(path)
    return path.read_text(encoding="utf-8")


def write_text(owner_scope: OwnerScope, path: pathlib.Path, content: str) -> None:
    drive = _drive_storage_for_path(path)
    if drive is not None and drive.rel_from_path(pathlib.Path(path)) is not None:
        drive.write_text(owner_scope, pathlib.Path(path), content)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def atomic_write_json(
    owner_scope: OwnerScope,
    path: pathlib.Path,
    payload: dict[str, Any],
    *,
    trailing_newline: bool = False,
    fsync: bool = False,
) -> None:
    """Atomically persist a JSON object using a unique sibling temp file."""
    path = pathlib.Path(path)
    drive = _drive_storage_for_path(path)
    rel = drive.rel_from_path(path) if drive is not None else None
    if drive is not None and rel is not None:
        drive.atomic_write_json(owner_scope, path, payload)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}"
    tmp = path.with_name(tmp_name)
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    if trailing_newline:
        content += "\n"
    try:
        if fsync:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                os.write(fd, content.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
        else:
            tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def read_json_dict(owner_scope: OwnerScope, path: pathlib.Path) -> dict[str, Any] | None:
    """Return a JSON object from ``path`` or ``None`` when absent/invalid."""
    path = pathlib.Path(path)
    drive = _drive_storage_for_path(path)
    if drive is not None and drive.rel_from_path(path) is not None:
        return drive.read_json_dict(owner_scope, path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        log.warning("Failed to parse JSON file %s", path, exc_info=True)
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def safe_relpath(p: str) -> str:
    """Normalize relative paths and reject traversal/NUL/control-char payloads."""
    if not isinstance(p, str):
        raise ValueError("Path must be a string.")
    for ch in p:
        if ch == "\x00":
            raise ValueError("Path contains NUL byte.")
        if ord(ch) < 0x20 and ch not in ("\t", "\n", "\r"):
            raise ValueError(f"Path contains control character U+{ord(ch):04X}.")
    p = p.replace("\\", "/").lstrip("/")
    if ".." in pathlib.PurePosixPath(p).parts:
        raise ValueError("Path traversal is not allowed.")
    return p


def truncate_for_log(s: str, max_chars: int = 4000) -> str:
    if len(s) <= max_chars:
        return s
    return s[: max_chars // 2] + "\n...\n" + s[-max_chars // 2 :]


def clip_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    half = max(200, max_chars // 2)
    return text[:half] + "\n...(truncated)...\n" + text[-half:]


def short(s: Any, n: int = 120) -> str:
    t = str(s or "")
    return t[:n] + "..." if len(t) > n else t


def estimate_tokens(text: str) -> int:
    """Rough token estimate aware of Cyrillic-heavy content.

    Latin text averages ~4 chars/token, Cyrillic ~1.5 chars/token.
    We count Cyrillic characters and blend the two rates so mixed
    Russian/English content gets a realistic estimate.
    """
    s = str(text or "")
    n = len(s)
    if n == 0:
        return 1
    cyrillic = sum(1 for ch in s if "Ѐ" <= ch <= "ӿ")
    latin = n - cyrillic
    return max(1, int(cyrillic / 1.5 + latin / 4) + 1)


def is_tool_success(result: str) -> bool:
    """Return False for error-prefix results and JSON {"ok": false}."""
    _err_prefixes = ("\u26a0\ufe0f", "Error:", "[TIMEOUT", "Failed")
    if result.startswith(_err_prefixes):
        return False
    if result.startswith("{"):
        try:
            data = json.loads(result)
            if isinstance(data, dict) and data.get("ok") is False:
                return False
        except (json.JSONDecodeError, ValueError):
            pass
    return True


def run_cmd(cmd: list[str], cwd: pathlib.Path | None = None) -> str:
    res = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n\nSTDOUT:\n{res.stdout}\n\nSTDERR:\n{res.stderr}")
    return res.stdout.strip()


def sanitize_task_for_event(
    task: dict[str, Any],
    threshold: int = 4000,
) -> dict[str, Any]:
    """Sanitize a task for a runtime event: preview, length and hash, never the full text."""
    try:
        sanitized = task.copy()

        keys_to_strip = [k for k in sanitized if k.endswith("_base64")]
        for key in keys_to_strip:
            value = sanitized.pop(key)
            sanitized[f"{key}_present"] = True
            if isinstance(value, str):
                sanitized[f"{key}_len"] = len(value)

        text = task.get("text")
        if not isinstance(text, str):
            return sanitized

        text_len = len(text)
        text_hash = sha256_text(text)
        sanitized["text_len"] = text_len
        sanitized["text_sha256"] = text_hash

        if text_len > threshold:
            sanitized["text"] = truncate_for_log(text, threshold)
            sanitized["text_truncated"] = True
        else:
            sanitized["text_truncated"] = False

        return sanitized
    except Exception:
        return task


_SECRET_KEYS = frozenset(
    [
        "token",
        "api_key",
        "apikey",
        "authorization",
        "secret",
        "password",
        "passwd",
        "passphrase",
    ]
)

import re as _re

_SECRET_PATTERNS = _re.compile(
    r"ghp_[A-Za-z0-9]{30,}"  # GitHub personal access token
    r"|gh[ousr]_[A-Za-z0-9]{30,}"  # GitHub OAuth/user/server/refresh tokens
    r"|github_pat_[A-Za-z0-9_]{30,}"  # GitHub fine-grained personal access token
    r"|AKIA[0-9A-Z]{16}"  # AWS access key id
    r"|sk_live_[A-Za-z0-9]{24,}"  # Stripe live secret key
    r"|sk_test_[A-Za-z0-9]{24,}"  # Stripe test secret key
    r"|sk-ant-[A-Za-z0-9_\-]{30,}"  # Anthropic API key
    r"|sk-or-[A-Za-z0-9\-]{30,}"  # OpenRouter API key
    r"|sk-proj-[A-Za-z0-9_\-]{30,}"  # OpenAI project key
    r"|sk-svcacct-[A-Za-z0-9_\-]{30,}"  # OpenAI service account key
    r"|sk-admin-[A-Za-z0-9_\-]{30,}"  # OpenAI admin key
    r"|gsk_[A-Za-z0-9]{30,}"  # Groq API key
    r"|sk-[A-Za-z0-9]{40,}"  # OpenAI API key
    r"|\b[0-9]{8,}:[A-Za-z0-9_\-]{30,}\b"  # Telegram bot token (digits:alphanum)
)
_SECRET_BEARER_RE = _re.compile(r"(?i)\bBearer\s+([A-Za-z0-9_\-./+=]{24,})")
_SECRET_URL_CREDENTIAL_RE = _re.compile(
    r"(?i)\b(?:postgres|postgresql|mysql|mariadb|mongodb(?:\+srv)?|redis)://[^/\s:@]+:[^/\s@]+@"
)
_SECRET_LITERAL_FIELDS_RE = _re.compile(
    r'(?im)(?:^|[\s,{])["\']?([A-Za-z_][A-Za-z0-9_-]*)["\']?\s*[:=]\s*["\']([^"\']+)["\']'
)
_SECRET_BRACKET_LITERAL_RE = _re.compile(
    r'(?im)\[\s*["\']([A-Za-z_][A-Za-z0-9_-]*)["\']\s*\]\s*[:=]\s*["\']([^"\']+)["\']'
)
_SECRET_UNQUOTED_ASSIGNMENT_RE = _re.compile(r"(?im)^([A-Za-z_][A-Za-z0-9_-]*)\s*[:=]\s*([A-Za-z0-9_\-./+=]{16,})\s*$")
_SECRET_FALLBACK_LITERAL_RE = _re.compile(
    r'(?i)(?:os\.getenv|os\.environ\.get|settings\.get)\(\s*[\'"]([^\'"]+)[\'"][^)]*,\s*[\'"]([^\'"]+)[\'"]'
    r'|api\.get_settings\([^)]*\)\.get\(\s*[\'"]([^\'"]+)[\'"][^)]*,\s*[\'"]([^\'"]+)[\'"]'
    r'|process\.env\.([A-Z0-9_]+)\s*(?:\|\||\?\?)\s*[\'"]([^\'"]+)[\'"]'
)
_SECRET_KEY_NAME_RE = _re.compile(
    r"(?i)^(?:"
    r"token|access_token|refresh_token|auth_token|secret|secret_key|password|passwd|passphrase|authorization|"
    r"api[_-]?key|database_url|db_url|ouroboros_network_password|aws_access_key_id|aws_secret_access_key|stripe_secret_key|"
    r"[a-z0-9_-]+(?:[_-](?:token|secret|password|passwd|passphrase|api[_-]?key))"
    r")$"
)


def _secret_key_name(key: str) -> bool:
    raw = str(key or "").strip()
    snake = raw.lower() if raw.upper() == raw else _re.sub(r"(?<!^)(?=[A-Z])", "_", raw).lower()
    normalized = _re.sub(r"[^a-z0-9]+", "_", snake).strip("_")
    return bool(_SECRET_KEY_NAME_RE.match(normalized))


def _secret_placeholder_value(value: str) -> bool:
    cleaned = str(value or "").strip().rstrip(",}]").strip().strip("'\"").strip()
    if not cleaned:
        return True
    lowered = cleaned.lower()
    if lowered in {"redacted", "***redacted***", "set_via_env", "set-in-settings", "changeme", "example"}:
        return True
    if lowered == "bearer":
        return True
    if lowered.startswith("bearer "):
        bearer_value = cleaned[7:].strip()
        if _secret_placeholder_value(bearer_value):
            return True
    if lowered in {"str", "string", "int", "float", "bool", "none", "null", "undefined"}:
        return True
    if lowered.startswith(
        ("str ", "str|", "str |", "str)", "str):", "string ", "string|", "string |", "string)", "string):")
    ):
        return True
    if lowered.startswith(("os.environ", "os.getenv", "process.env", "settings.", "api.get_settings")):
        for literal in _re.findall(r"['\"]([^'\"]*)['\"]", cleaned):
            if literal and not _secret_placeholder_value(literal) and not _secret_key_name(literal):
                return False
        return True
    if lowered.startswith(('f"', "f'")) and "{" in cleaned:
        return True
    if "settings" in lowered and any(
        word in lowered for word in ("configure", "configured", "set", "enter", "provide")
    ):
        return True
    if "+" in cleaned and any(part in lowered for part in ("token", "key", "secret", "settings", "env")):
        return True
    if cleaned.startswith(("<", "${", "{")) and (cleaned.endswith((">", "}")) or cleaned.count("{") == 1):
        return True
    if _re.match(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\(?[^)]*\)?$", cleaned):
        return True
    if _re.match(r"^[A-Za-z_][A-Za-z0-9_]*\([^)]*\)$", cleaned):
        return True
    if _re.match(r"^[a-z_][a-z0-9_]*$", cleaned) and cleaned in {
        "password",
        "token",
        "secret",
        "api_key",
        "auth_header",
        "access_token",
        "refresh_token",
    }:
        return True
    return bool(
        cleaned.isupper() and "_" in cleaned and not any(ch.isdigit() for ch in cleaned) and _secret_key_name(cleaned)
    )


def _redact_url_credential(match: _re.Match[str]) -> str:
    """Cut only the password out of scheme://user:pass@ — scheme and user are
    non-secret and debug-relevant."""
    scheme, rest = match.group(0).split("://", 1)
    user = rest.split(":", 1)[0]
    return f"{scheme}://{user}:***REDACTED***@"


def _scrub_secret_text(value: str) -> str:
    """One string pass shared by the arg- and result-side sanitizers."""
    value = _SECRET_PATTERNS.sub("***REDACTED***", value)
    value = _SECRET_BEARER_RE.sub("Bearer ***REDACTED***", value)
    return _SECRET_URL_CREDENTIAL_RE.sub(_redact_url_credential, value)


def sanitize_tool_result_for_log(result: str) -> str:
    """Redact potential secrets from tool result before logging."""
    if not isinstance(result, str) or len(result) < 20:
        return result
    return _scrub_secret_text(result)


def contains_real_secret_value(text: str) -> tuple[bool, list[str]]:
    """Detect concrete secret values by format and simple literal assignments."""
    if not isinstance(text, str) or not text:
        return False, []
    matches = [
        *_SECRET_PATTERNS.findall(text),
        *_SECRET_BEARER_RE.findall(text),
        *_SECRET_URL_CREDENTIAL_RE.findall(text),
    ]
    matches.extend(
        literal
        for env_key, env_literal, api_key, api_literal, js_key, js_literal in _SECRET_FALLBACK_LITERAL_RE.findall(text)
        for key, literal in ((env_key, env_literal), (api_key, api_literal), (js_key, js_literal))
        if key and literal and _secret_key_name(key) and not _secret_placeholder_value(literal)
    )
    matches.extend(
        value.strip()
        for key, value in _SECRET_LITERAL_FIELDS_RE.findall(text)
        if _secret_key_name(key) and not _secret_placeholder_value(value)
    )
    matches.extend(
        value.strip()
        for key, value in _SECRET_BRACKET_LITERAL_RE.findall(text)
        if _secret_key_name(key) and not _secret_placeholder_value(value)
    )
    matches.extend(
        value.strip()
        for key, value in _SECRET_UNQUOTED_ASSIGNMENT_RE.findall(text)
        if _secret_key_name(key) and not _secret_placeholder_value(value)
    )
    return bool(matches), list(matches)


def sanitize_tool_args_for_log(
    fn_name: str,
    args: dict[str, Any],
    threshold: int = 3000,
    redact_keys: Collection[str] | None = (),
) -> dict[str, Any]:
    """Sanitize tool arguments for logging: redact secrets, truncate large fields.

    ``redact_keys`` holds argument names the tool declared as log-redacted;
    their values are omitted entirely. ``None`` means the tool's redaction
    metadata was unavailable: fail closed and omit every argument value.
    """
    if redact_keys is None:
        return {str(key): "[OMITTED: redacted argument]" for key in args}

    def _sanitize_value(key: Any, value: Any, depth: int) -> Any:
        # Result mappings routed through here can carry non-string keys
        # (int status codes); the checks coerce so one odd key does not throw
        # the whole payload into the fail-closed branch below.
        key = str(key)
        if depth > 3:
            return {"_depth_limit": True}
        if key in redact_keys:
            return "[OMITTED: redacted argument]"
        # _secret_key_name is the wide detector (access_token, client_secret,
        # aws_secret_access_key, CamelCase/hyphen variants); the narrow
        # _SECRET_KEYS set alone let those through.
        if key.lower() in _SECRET_KEYS or _secret_key_name(key):
            return "*** REDACTED ***"
        if isinstance(value, str) and len(value) > threshold:
            return f"<TRUNCATED:{key}:{len(value)}ch:sha={sha256_text(value)[:12]}>"
        if isinstance(value, str):
            # Same pass as the result-side sanitizer: secrets embedded in
            # free-text values must not survive into logs or traces.
            return _scrub_secret_text(value)
        if isinstance(value, dict):
            return {k: _sanitize_value(k, v, depth + 1) for k, v in value.items()}
        if isinstance(value, list):
            sanitized = [_sanitize_value(key, item, depth + 1) for item in value[:50]]
            if len(value) > 50:
                sanitized.append({"_truncated": f"... {len(value) - 50} more items"})
            return sanitized
        try:
            json.dumps(value, ensure_ascii=False)
            return value
        except (TypeError, ValueError):
            log.debug("Failed to JSON serialize value in sanitize_tool_args", exc_info=True)
            return {"_repr": repr(value)}

    try:
        return {k: _sanitize_value(k, v, 0) for k, v in args.items()}
    except Exception:
        # Fail closed: a raw round-trip of args here would hand every log and
        # trace consumer the unsanitized values this function exists to hide.
        log.debug("Failed to sanitize tool arguments for logging", exc_info=True)
        return {"_error": "sanitization_failed"}


def build_trace_summary(llm_trace: dict) -> str:
    """Return a compact human-readable summary of tool calls and agent notes.

    Shared rather than agent-owned: the agent renders it into the stored task
    result, and background memory renders it into a distillation request, and
    neither package may import the other.
    """
    tool_calls = llm_trace.get("tool_calls", []) or []
    notes = llm_trace.get("reasoning_notes", []) or []

    n = len(tool_calls)
    errors = sum(1 for tc in tool_calls if isinstance(tc, dict) and tc.get("is_error"))

    lines: list[str] = [f"## Tool trace ({n} calls, {errors} errors)"]

    if not tool_calls:
        lines.append("No tool calls.")
    else:

        def _fmt_call(idx: int, tc: dict) -> str:
            name = tc.get("tool", "unknown")
            args = tc.get("args", {})
            if isinstance(args, dict):
                parts = []
                for k, v in list(args.items())[:2]:
                    v_str = str(v)
                    if len(v_str) > 60:
                        v_str = v_str[:57] + "..."
                    parts.append(f"{k}={v_str!r}")
                if len(args) > 2:
                    parts.append(f"... (+{len(args) - 2} more args)")
                args_str = ", ".join(parts)
            else:
                args_str = repr(args)
                if len(args_str) > 80:
                    args_str = args_str[:77] + "..."
            facts = []
            status = str(tc.get("status") or "").strip()
            if status and status != "ok":
                facts.append(f"status={status}")
            if tc.get("exit_code") not in (None, 0):
                facts.append(f"exit_code={tc.get('exit_code')}")
            if tc.get("signal"):
                facts.append(f"signal={tc.get('signal')}")
            fact_suffix = f" [{', '.join(facts)}]" if facts else ""
            suffix = " → ERROR" if tc.get("is_error") else ""
            return f"{idx}. {name}({args_str}){fact_suffix}{suffix}"

        if n > 30:
            shown = (
                [_fmt_call(i + 1, tool_calls[i]) for i in range(15)]
                + [f"... ({n - 30} more calls) ..."]
                + [_fmt_call(n - 14 + i, tool_calls[n - 15 + i]) for i in range(15)]
            )
        else:
            shown = [_fmt_call(i + 1, tool_calls[i]) for i in range(n)]
        lines.extend(shown)

    if notes:
        lines.append("\n## Agent notes (supplementary, not source of truth)")
        lines.extend(f"- {note}" for note in notes)

    summary = "\n".join(lines)
    if len(summary) > 4000:
        summary = summary[:3997] + "..."
    return summary


def truncate_review_artifact(text: str | None, limit: int = 4000) -> str:
    """Return a display-safe preview with explicit OMISSION NOTE, never silent clipping."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n⚠️ OMISSION NOTE: truncated at {limit} chars; original length {len(text)}"


def truncate_review_reason(text: str, limit: int = 120) -> str:
    """Compact reviewer reason preview with explicit omission note."""
    return truncate_review_artifact(text, limit=limit)
