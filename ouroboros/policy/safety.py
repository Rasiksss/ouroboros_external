"""
Policy-based safety check for tool calls.

Built-ins use explicit policy entries; unknown tools default to one light-model
check. The registry sandbox still runs first, Claude edits still have protected
path revert guards, and commit review remains separate.
"""

import ast
import json
import logging
import pathlib
import re
import shlex
import time
from typing import Any

from ouroboros.app.config import get_light_model
from ouroboros.llm import LLMClient
from ouroboros.observability.pricing import infer_provider_from_model
from ouroboros.tools.catalog import safety_policy_by_tool_name
from ouroboros.tools.events import QueueToolEvents

log = logging.getLogger(__name__)

# The safety check runs before a tool does, and can be reached without a context at all
# (a bare policy check with ``ctx=None``). Its usage still has to be counted, so it goes
# through a channel with no queue behind it, which accounts locally — the same thing this
# module used to do by importing the supervisor's counters directly.
_NO_CONTEXT_EVENTS = QueueToolEvents(None, None)

# Policy constants.

POLICY_SKIP = "skip"
POLICY_CHECK = "check"
POLICY_CHECK_CONDITIONAL = "check_conditional"

# Unknown/agent-created tools get one cheap LLM recheck.
DEFAULT_POLICY = POLICY_CHECK

# The light model can be slow, so the call itself gets a wide timeout. The
# deadline adds room to acquire a slot of the process-wide LLM semaphore and
# bounds the whole verdict, so an abandoned check cannot camp on that slot.
_SAFETY_LLM_TIMEOUT_SEC = 300
_SAFETY_LLM_DEADLINE_SEC = _SAFETY_LLM_TIMEOUT_SEC + 60

# Must cover every built-in exported from ouroboros/tools; invariant-tested.
TOOL_POLICY: dict[str, str] = safety_policy_by_tool_name()

# run_shell safe-subject whitelist.

# ``pip`` mutates the Python env and must route through the LLM check.
SAFE_SHELL_COMMANDS = frozenset(
    [
        "ls",
        "cat",
        "head",
        "tail",
        "grep",
        "rg",
        "find",
        "wc",
        "git",
        "pytest",
        "pwd",
        "whoami",
        "date",
        "which",
        "file",
        "stat",
        "diff",
        "tree",
        "du",
        "df",
    ]
)

_SAFE_PYTHON_MODULE_ALIASES = {
    "pytest": "pytest",
    "py.test": "pytest",
}


def _split_shell_command(raw_cmd: Any) -> list[str]:
    """Best-effort argv parser for safety whitelist classification."""
    if isinstance(raw_cmd, list):
        return [str(part) for part in raw_cmd if str(part).strip()]
    text = str(raw_cmd or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(part) for part in parsed if str(part).strip()]
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    try:
        parsed = ast.literal_eval(text)
        if isinstance(parsed, list):
            return [str(part) for part in parsed if str(part).strip()]
    except (SyntaxError, ValueError):
        pass
    try:
        return [str(part) for part in shlex.split(text) if str(part).strip()]
    except ValueError:
        return text.split()


def _is_explicit_python_interpreter(executable: str) -> bool:
    """Allow literal Python interpreter tokens, not path/basename lookalikes."""
    token = str(executable or "").strip().lower()
    if not token:
        return False
    if token in {"python", "python3"}:
        return True
    return bool(re.fullmatch(r"python\d+(?:\.\d+)?", token))


def _normalize_safe_shell_subject(raw_cmd: Any) -> str:
    """Return the canonical safe subject for shell allowlisting."""
    argv = _split_shell_command(raw_cmd)
    if not argv:
        return ""

    executable = str(argv[0]).strip().lower()
    if executable in SAFE_SHELL_COMMANDS:
        return executable

    if _is_explicit_python_interpreter(executable):
        for idx, part in enumerate(argv[1:-1], start=1):
            part_str = str(part)
            if part_str == "-m":
                module = str(argv[idx + 1]).lower()
                return _SAFE_PYTHON_MODULE_ALIASES.get(module, "")
            if part_str == "-c":
                break
            # After a script path, later -m/-c belongs to that script.
            if not part_str.startswith("-"):
                break
            # After --, everything belongs to the script.
            if part_str == "--":
                break

    return ""


# LLM check plumbing.


def _get_safety_prompt() -> str:
    """Load the safety system prompt from prompts/SAFETY.md."""
    prompt_path = pathlib.Path(__file__).resolve().parents[2] / "prompts" / "SAFETY.md"
    try:
        return prompt_path.read_text(encoding="utf-8")
    except Exception:
        log.exception("Failed to read SAFETY.md")
        return (
            "You are a security supervisor. Block only clearly destructive commands. "
            "Default to SAFE. Respond with JSON: "
            '{"status": "SAFE"|"SUSPICIOUS"|"DANGEROUS", "reason": "..."}'
        )


# Secret redaction.

# Segment matching avoids false positives like ``override_author``.
_SECRET_KEY_SEGMENTS = frozenset(
    {
        "key",  # only together with prefix segment — see _is_secret_key
        "apikey",
        "secret",
        "token",
        "password",
        "passwd",
        "credential",
        "credentials",
        "cookie",
        "authorization",
    }
)

# Prefix+suffix shapes treated as credential keys.
_SECRET_KEY_COMBO = frozenset(
    {
        ("api", "key"),
        ("access", "key"),
        ("access", "token"),
        ("auth", "token"),
        ("auth", "key"),
        ("session", "token"),
        ("refresh", "token"),
    }
)


def _is_secret_key(key: str) -> bool:
    """Segment-aware credential-key classifier."""
    segments = [s for s in re.split(r"[_\-]+", str(key).lower()) if s]
    if not segments:
        return False
    seg_set = set(segments)
    if any(seg in _SECRET_KEY_SEGMENTS and seg != "key" for seg in seg_set):
        return True
    return any((segments[i], segments[i + 1]) in _SECRET_KEY_COMBO for i in range(len(segments) - 1))


# Known inline secret shapes. Do not boundary-anchor sk-/pk-/rk-/gh* tokens:
# over-redaction is acceptable, under-redaction is not.
_SECRET_INLINE_PATTERNS = (
    re.compile(r"(sk|pk|rk|gh[opsu])[-_][A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}", re.IGNORECASE),
    re.compile(r"\bapi[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9._\-]{16,}['\"]?", re.IGNORECASE),
)


def _redact_secret_value(value: Any) -> Any:
    """Return a JSON-serializable redaction marker for a sensitive value."""
    if isinstance(value, str) and value:
        return f"[REDACTED: {len(value)} chars]"
    if value in (None, "", 0, False):
        return value
    return "[REDACTED]"


def _redact_secrets_in_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Redact secret-like keys and inline secret shapes from tool arguments."""

    def _walk(value: Any) -> Any:
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                if _is_secret_key(k):
                    out[k] = _redact_secret_value(v)
                else:
                    out[k] = _walk(v)
            return out
        if isinstance(value, (list, tuple)):
            return [_walk(v) for v in value]
        if isinstance(value, str):
            return _redact_secrets_in_text(value)
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        # Repr fallback is also scrubbed in case it contains a token.
        return _redact_secrets_in_text(repr(value))

    try:
        return _walk(arguments)
    except Exception:
        # Never let redaction itself block every unknown tool.
        return {"_redacted": "[REDACTION_FAILED]"}


def _redact_secrets_in_text(text: str) -> str:
    """Strip common inline-secret shapes out of a free-form string."""
    redacted = text
    for pattern in _SECRET_INLINE_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _format_messages_for_safety(messages: list[dict[str, Any]]) -> str:
    """Format compact safety context, redacting before truncation."""
    parts = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "")
        if not content or role == "tool":
            continue
        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        text = _redact_secrets_in_text(str(content))
        if len(text) > 500:
            omitted = len(text) - 500
            text = text[:500] + f" [...{omitted} chars omitted]"
        parts.append(f"[{role}] {text}")
    return "\n".join(parts)


def _build_check_prompt(
    tool_name: str,
    arguments: dict[str, Any],
    messages: list[dict[str, Any]] | None = None,
) -> str:
    safe_args = _redact_secrets_in_arguments(arguments or {})
    try:
        args_json = json.dumps(safe_args, indent=2, default=repr)
    except Exception:
        args_json = repr(safe_args)
    prompt = f"Proposed tool call:\nTool: {tool_name}\nArguments:\n```json\n{args_json}\n```\n"
    if messages:
        context = _format_messages_for_safety(messages)
        if context.strip():
            prompt += f"\nConversation context:\n{context}\n"
    prompt += "\nIs this safe?"
    return prompt


def _parse_safety_response(text: str) -> dict[str, Any] | None:
    """Parse JSON from LLM response, handling markdown code fences."""
    clean = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        return None


_REMOTE_PROVIDER_KEYS = (
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "CLOUDRU_FOUNDATION_MODELS_API_KEY",
    "SELFHOSTED_LLM_BASE_URL",
)

_LOCAL_ROUTING_KEYS = (
    "USE_LOCAL_MAIN",
    "USE_LOCAL_LIGHT",
    "USE_LOCAL_FALLBACK",
)

# Provider-specific API key mapped from ``infer_api_key_type`` result.
_PROVIDER_KEY_ENV = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai-compatible": "OPENAI_COMPATIBLE_API_KEY",
    "cloudru": "CLOUDRU_FOUNDATION_MODELS_API_KEY",
    "selfhosted": "SELFHOSTED_LLM_BASE_URL",
}


def _settings_snapshot():
    from ouroboros.storage.settings.store import load_settings

    return load_settings()


def _any_remote_provider_configured() -> bool:
    settings = _settings_snapshot()
    return any(str(settings.get(k, "") or "").strip() for k in _REMOTE_PROVIDER_KEYS)


def _any_local_routing_enabled() -> bool:
    settings = _settings_snapshot()
    return any(str(settings.get(k, "") or "").lower() in ("true", "1") for k in _LOCAL_ROUTING_KEYS)


def _light_model_has_reachable_provider(light_model: str) -> bool:
    """Return whether the light model's direct provider config is reachable."""
    try:
        from ouroboros.observability.pricing import infer_api_key_type

        key_type = infer_api_key_type(light_model)
    except Exception:  # pragma: no cover — defensive
        return True  # don't over-block on classifier failure
    env_key = _PROVIDER_KEY_ENV.get(key_type)
    if env_key is None:
        return True
    settings = _settings_snapshot()
    if not str(settings.get(env_key, "") or "").strip():
        return False
    if key_type == "openai-compatible":
        base_url = str(settings.OPENAI_COMPATIBLE_BASE_URL or "").strip() or str(settings.OPENAI_BASE_URL or "").strip()
        if not base_url:
            return False
    return True


def _resolve_safety_routing() -> tuple[bool, bool, str | None]:
    """Choose local/remote safety backend; unreachable fallback fails open."""
    if str(_settings_snapshot().USE_LOCAL_LIGHT or "").lower() in ("true", "1"):
        return True, False, None

    light_model = get_light_model()

    if _any_remote_provider_configured():
        # The direct light-model provider needs its own key.
        if _light_model_has_reachable_provider(light_model):
            return False, False, None
        if _any_local_routing_enabled():
            # Provider mismatch: local is fallback, so local outage is tolerated.
            return True, True, None
        return (
            False,
            False,
            (
                f"Light model provider key missing for {light_model} "
                f"(other remote keys are set but they don't cover this provider); "
                "skipping check."
            ),
        )

    if _any_local_routing_enabled():
        # Local-only configs should warn, not hard-block, on local outage.
        return True, True, None

    return (
        False,
        False,
        ("No safety LLM available (neither remote provider keys nor local routing are configured); skipping check."),
    )


_UNCHECKED_WARNING_SUFFIX = (
    "The tool call was allowed so the agent is not hard-blocked on a misconfigured "
    "runtime — the hardcoded sandbox (capability policy in tool_authorization.py, "
    "mutative-git via shell, gh repo/auth) still applies to every tool."
)


def _run_llm_check(
    tool_name: str,
    arguments: dict[str, Any],
    messages: list[dict[str, Any]] | None,
    ctx: Any | None,
) -> tuple[bool, str]:
    """Run a single light-model safety check and classify the verdict."""
    _use_local_light, _is_local_fallback, _skip_reason = _resolve_safety_routing()
    if _skip_reason is not None:
        log.warning("Safety backend unavailable for %s: %s", tool_name, _skip_reason)
        return True, (
            f"⚠️ SAFETY_WARNING: Safety backend is not configured "
            f"({_skip_reason.rstrip('.')}). {_UNCHECKED_WARNING_SUFFIX}"
        )

    prompt = _build_check_prompt(tool_name, arguments, messages)
    client = LLMClient()

    light_model = get_light_model()
    log.info(f"Running safety check on {tool_name} using {light_model} (local={_use_local_light})")

    try:
        safety_messages = [
            {"role": "system", "content": _get_safety_prompt()},
            {"role": "user", "content": prompt},
        ]
        try:
            msg, usage = client.chat(
                messages=safety_messages,
                model=light_model,
                use_local=_use_local_light,
                timeout_sec=_SAFETY_LLM_TIMEOUT_SEC,
                # Bounds the concurrency-slot wait too: an abandoned check must
                # not camp on the process-wide LLM semaphore.
                deadline=time.monotonic() + _SAFETY_LLM_DEADLINE_SEC,
            )
        except TypeError as exc:
            if "timeout_sec" not in str(exc) and "deadline" not in str(exc):
                raise
            msg, usage = client.chat(
                messages=safety_messages,
                model=light_model,
                use_local=_use_local_light,
            )
    except Exception as e:
        # Fallback local outage warns instead of blocking all unknown tools.
        if _use_local_light and _is_local_fallback:
            log.warning(
                "Safety local-fallback LLM call failed for %s (%s); proceeding with warning",
                tool_name,
                e,
            )
            return True, (f"⚠️ SAFETY_WARNING: Local safety runtime unreachable ({e}). {_UNCHECKED_WARNING_SUFFIX}")
        log.exception(f"Safety check LLM call failed for {tool_name}")
        return False, f"⚠️ SAFETY_VIOLATION: Safety check failed with error: {e}"

    if usage:
        # Use provider-canonical model identity for cost/events.
        resolved_model = str(usage.get("resolved_model") or light_model)
        if _use_local_light:
            provider = "local"
            model_name = f"{light_model} (local)"
        else:
            provider = str(usage.get("provider") or infer_provider_from_model(light_model))
            model_name = resolved_model
        events = ctx.events if ctx is not None else _NO_CONTEXT_EVENTS
        events.usage(
            model_name,
            usage,
            category="safety",
            provider=provider,
            source="safety_check",
        )

    result = _parse_safety_response(msg.get("content") or "")
    if result is None:
        log.error(f"Safety check returned invalid JSON for {tool_name}: {msg.get('content')}")
        return False, "⚠️ SAFETY_VIOLATION: Safety Supervisor returned unparseable response."

    status = str(result.get("status", "")).upper()
    reason = result.get("reason", "Unknown")

    if status == "SAFE":
        return True, ""

    if status == "SUSPICIOUS":
        log.warning(f"Safety check: {tool_name} is suspicious: {reason}")
        return True, (
            f"⚠️ SAFETY_WARNING: The Safety Supervisor flagged this action as suspicious.\n"
            f"Reason: {reason}\n"
            f"The command was allowed, but consider whether this is the right approach."
        )

    # DANGEROUS or unrecognised status: fail safe.
    log.error(f"Safety check blocked {tool_name}: {reason}")
    return False, (
        f"⚠️ SAFETY_VIOLATION: The Safety Supervisor blocked this command.\n"
        f"Reason: {reason}\n\n"
        f"You must find a different, safer approach to achieve your goal."
    )


# Public entry point.


def check_safety(
    tool_name: str,
    arguments: dict[str, Any],
    messages: list[dict[str, Any]] | None = None,
    ctx: Any | None = None,
    policy: str | None = None,
) -> tuple[bool, str]:
    """Return ``(allowed, warning_or_error)`` for one tool call.

    ``policy`` is the caller-supplied declaration (the tool descriptor's
    ``safety_policy`` — the unified metadata contract, keyed by the canonical
    namespaced name). Without it the legacy name-keyed core table applies, and
    unknown names default to ``check``.
    """
    # Arguments can be None for no-parameter tool calls.
    tool_name = str(tool_name or "").strip()
    arguments = arguments or {}
    declared = str(policy or "").strip().lower()
    # ``check_conditional`` is not accepted as a declaration: its shell-subject
    # whitelist belongs to the name-keyed core tools, and a skill declaring it
    # would let any {"cmd": "ls ..."} argument bypass the LLM check. Core tools
    # that carry it resolve to the same value through the name table below.
    if declared in {POLICY_SKIP, POLICY_CHECK}:
        policy = declared
    else:
        policy = TOOL_POLICY.get(tool_name, DEFAULT_POLICY)

    if policy == POLICY_SKIP:
        return True, ""

    if policy == POLICY_CHECK_CONDITIONAL:
        raw_cmd = arguments.get("cmd", arguments.get("command", ""))
        if _normalize_safe_shell_subject(raw_cmd):
            return True, ""
        return _run_llm_check(tool_name, arguments, messages, ctx)

    return _run_llm_check(tool_name, arguments, messages, ctx)
