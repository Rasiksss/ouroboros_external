"""Skill execution substrate for published script skills.

``skill_exec`` runs only manifest-declared scripts from eligible skills. It uses
a scrubbed env, runtime allowlist, cwd confinement,
timeout, byte caps, and panic-tracked process groups.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import pathlib
import shutil
import sys
import tempfile
from typing import Any

from new_core.skills.models import SkillId
from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.app.config import load_settings
from ouroboros.contracts.plugin_api import FORBIDDEN_SKILL_SETTINGS
from ouroboros.contracts.sandbox_policy import NetworkPolicy, skill_default_policy
from ouroboros.contracts.skill_payload_policy import canonical_declared_script_rel
from ouroboros.sandbox.runner import SandboxRunner
from ouroboros.skills.loader import (
    requested_core_setting_keys,
    skill_state_dir,
    trusted_permissions_for_skill,
    trusted_setting_keys_for_skill,
)
from ouroboros.tools.catalog import DEFAULT_TOOL_RESULT_LIMIT, spec_for_tool
from ouroboros.tools.service.artifacts import materialize, provenance_path, publish

log = logging.getLogger(__name__)

# Execution policy

# Hard ceiling: skill_exec is bounded/synchronous, not a worker substrate.
_HARD_TIMEOUT_CEILING_SEC = 300
_DEFAULT_TIMEOUT_SEC = 60
# Default stream caps; overridable via OUROBOROS_SKILL_EXEC_MAX_{STDOUT,STDERR}_BYTES.
# These cap the subprocess pipes (process is killed on overflow) and are distinct
# from the char limit on the rendered tool result
# (OUROBOROS_SKILL_EXEC_RESULT_LIMIT, enforced in _render_skill_exec_result).
_MAX_STDOUT_BYTES = 256 * 1024
_MAX_STDERR_BYTES = 128 * 1024
# Hard ceiling for configured stream caps: the drained output is buffered in the
# server heap, so oversized values are clamped like _HARD_TIMEOUT_CEILING_SEC.
_HARD_STREAM_CAP_CEILING_BYTES = 8 * 1024 * 1024


def _bound_stream_cap(configured: int, default: int) -> int:
    if configured <= 0:
        return default
    return min(configured, _HARD_STREAM_CAP_CEILING_BYTES)


_ALLOWED_RUNTIMES = {
    # python3 falls back to python for Windows/minimal Linux installs.
    "python": ("python", "python3"),
    "python3": ("python3", "python"),
    "bash": ("bash",),
    "node": ("node",),
    # Additional runtimes still resolve through shutil.which and fail closed.
    "deno": ("deno",),
    "ruby": ("ruby",),
    "go": ("go",),
}

# OS-level env needed for basic runtimes; app secrets require a trusted manifest declaration.
_ALWAYS_FORWARDED_ENV = frozenset(
    {
        "PATH",
        "HOME",  # Unix home dir
        "USERPROFILE",  # Windows equivalent of HOME
        "APPDATA",  # Windows roaming app data (e.g. pip cache)
        "LOCALAPPDATA",  # Windows local app data
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SYSTEMROOT",  # Windows
        "TMPDIR",
        "TMP",
        "TEMP",
    }
)

# Core settings keys are available only to trusted distribution manifests.
_FORBIDDEN_ENV_FORWARD_KEYS = FORBIDDEN_SKILL_SETTINGS


def _resolve_runtime_binary(runtime: str) -> str | None:
    """Return the runtime binary path; Python resolves to the running interpreter."""

    # Bundled skills pin their Python deps in the app environment, while PATH may
    # hold a bare system python (the production image keeps the venv off PATH).
    if runtime in ("python", "python3") and sys.executable:
        app_python = pathlib.Path(sys.executable)
        if app_python.is_file():
            return str(app_python)
    for candidate in _ALLOWED_RUNTIMES.get(runtime or "", ()):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def _scrub_env(
    manifest_env_keys: list[str],
    skill_state_dir_path: pathlib.Path,
    skill_name: str,
    trusted_setting_keys: list[str] | None = None,
    workdir: pathlib.Path | None = None,
) -> dict[str, str]:
    """Build a minimal subprocess env from OS keys and trusted settings."""
    env: dict[str, str] = {}
    for key in _ALWAYS_FORWARDED_ENV:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    if manifest_env_keys:
        settings = load_settings()
        # Compare protected keys case-insensitively so lowercase aliases fail closed.

        protected_upper = {k.upper() for k in _FORBIDDEN_ENV_FORWARD_KEYS}
        protected_upper.update(requested_core_setting_keys(list(manifest_env_keys or [])))
        trusted_upper = {str(k).strip().upper() for k in (trusted_setting_keys or []) if str(k).strip()}
        allow = {str(k).strip() for k in manifest_env_keys if str(k).strip()}
        for key in allow:
            canonical = key.upper()
            if canonical in protected_upper and canonical not in trusted_upper:
                log.warning(
                    "Skill %s asked env_from_settings for %s outside its trusted distribution manifest.",
                    skill_name,
                    key,
                )
                continue
            val = settings.get(canonical) if canonical in protected_upper else settings.get(key)
            if val is None or val == "":
                continue
            env[canonical if canonical in protected_upper else key] = str(val)
    env["OUROBOROS_SKILL_NAME"] = skill_name
    env["OUROBOROS_SKILL_STATE_DIR"] = str(skill_state_dir_path)
    if workdir is not None:
        # The only place a script reads inputs from and writes outputs to; skill_exec
        # fills it before the run and publishes from it after, so no other tool ever
        # depends on this process's disk.
        env["OUROBOROS_SKILL_WORKDIR"] = str(workdir)
    return env


def _bound_timeout(requested_sec: Any) -> int:
    try:
        timeout = int(requested_sec)
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT_SEC
    if timeout <= 0:
        timeout = _DEFAULT_TIMEOUT_SEC
    return min(timeout, _HARD_TIMEOUT_CEILING_SEC)


def _cap(data: bytes, limit: int, label: str) -> str:
    text = data.decode("utf-8", errors="replace")
    if len(data) <= limit:
        return text
    return text[:limit] + f"\n\n⚠️ OMISSION NOTE: skill_exec truncated {label} at {limit} bytes (total {len(data)})."


def _emit_skill_lifecycle_event(
    ctx: ToolContext,
    *,
    event_type: str,
    skill: str,
    script: str,
    exit_code: int | None = None,
    error: str = "",
) -> None:
    payload: dict[str, Any] = {"skill": skill, "script": script}
    if exit_code is not None:
        payload["exit_code"] = int(exit_code)
    if error:
        payload["error"] = str(error)
    # Type, timestamp, task id, delivery, and what to do without a supervisor all belong to
    # the channel. This function's job is what happened.
    ctx.events.lifecycle(event_type, payload)


def _render_skill_exec_result(
    ctx: ToolContext,
    *,
    payload: dict[str, Any],
    stdout_bytes: bytes,
    stderr_bytes: bytes,
    overflowed: bool,
    stdout_cap: int,
    stderr_cap: int,
) -> str:
    skill_name = str(payload.get("skill") or "")
    script_rel = str(payload.get("script") or "")
    returncode = int(payload.get("exit_code") or 0)
    payload = {
        **payload,
        "output_overflow": overflowed,
        "stdout": _cap(stdout_bytes, stdout_cap, "stdout"),
        "stderr": _cap(stderr_bytes, stderr_cap, "stderr"),
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if overflowed:
        _emit_skill_lifecycle_event(
            ctx,
            event_type="skill_exec_failed",
            skill=skill_name,
            script=script_rel,
            exit_code=int(returncode),
            error="stdout/stderr byte cap exceeded",
        )
        return (
            f"⚠️ SKILL_EXEC_OVERFLOW: skill {skill_name!r} script "
            f"{script_rel!r} exceeded stdout/stderr byte caps "
            f"(stdout<={stdout_cap}B, stderr<={stderr_cap}B) "
            "and was killed.\n\n" + rendered
        )
    if returncode != 0:
        _emit_skill_lifecycle_event(
            ctx,
            event_type="skill_exec_failed",
            skill=skill_name,
            script=script_rel,
            exit_code=int(returncode),
            error=f"exited with code {returncode}",
        )
        return (
            f"⚠️ SKILL_EXEC_FAILED: skill {skill_name!r} script "
            f"{script_rel!r} exited with code {returncode}.\n\n" + rendered
        )
    limit = spec_for_tool("skill_exec").result_limit or DEFAULT_TOOL_RESULT_LIMIT
    if len(rendered) > limit:
        error = f"result is {len(rendered)} chars, limit is {limit}"
        _emit_skill_lifecycle_event(
            ctx,
            event_type="skill_exec_failed",
            skill=skill_name,
            script=script_rel,
            exit_code=0,
            error=error,
        )
        published = f" Published artifacts: {', '.join(payload['artifacts'])}." if payload["artifacts"] else ""
        return f"⚠️ TOOL_ERROR (skill_exec): skill {skill_name!r} script {script_rel!r} {error}.{published}"
    _emit_skill_lifecycle_event(
        ctx,
        event_type="skill_exec_finished",
        skill=skill_name,
        script=script_rel,
        exit_code=0,
    )
    return rendered


def _resolve_script_path(
    skill_dir: pathlib.Path,
    script_rel: str,
    *,
    allowed_paths: list[pathlib.Path] | None = None,
) -> pathlib.Path | None:
    """Resolve a script path under skill_dir and optionally require manifest coverage."""
    rel = (script_rel or "").strip()
    if not rel or rel.startswith(("/", "~")):
        return None
    if ".." in pathlib.PurePosixPath(rel).parts:
        return None
    candidate = (skill_dir / rel).resolve()
    try:
        candidate.relative_to(skill_dir.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    if allowed_paths is not None:
        reviewed = {p.resolve() for p in allowed_paths}
        if candidate not in reviewed:
            return None
    return candidate


# Tool handlers


def _handle_skill_exec(
    ctx: ToolContext,
    skill: str = "",
    script: str = "",
    args: list[str] | None = None,
    **_kwargs: Any,
) -> str:
    skill_name = str(skill or "").strip()
    script_rel = str(script or "").strip()
    if not skill_name or not script_rel:
        return "⚠️ SKILL_EXEC_ERROR: both 'skill' and 'script' are required."
    surface = ctx.skill_surface
    if surface is None:
        return "⚠️ SKILLS_UNAVAILABLE: skill surface is not configured"
    if not surface.skills():
        return "⚠️ SKILLS_UNAVAILABLE: No skills were found in the repository skills directory."

    drive_root = pathlib.Path(ctx.drive_root)
    try:
        skill_id = SkillId.parse(skill_name)
    except ValueError:
        return f"⚠️ SKILL_EXEC_ERROR: skill id {skill_name!r} is not canonical."
    loaded = surface.get(skill_id)
    if loaded is None:
        return f"⚠️ SKILL_EXEC_ERROR: skill {skill_name!r} not found in the repository skills directory."
    if not loaded.selected or not loaded.eligible:
        reason = loaded.blocked_reasons[0] if loaded.blocked_reasons else "skill is not selected"
        return f"⚠️ SKILL_EXEC_BLOCKED: skill {skill_name!r} is not eligible for execution ({reason})."
    if loaded.load_error:
        return (
            f"⚠️ SKILL_EXEC_ERROR: skill {skill_name!r} manifest is broken ({loaded.load_error}). Fix the skill package."
        )
    if loaded.manifest.is_extension():
        # Extensions execute in-process through PluginAPI, not subprocesses.
        return (
            f"⚠️ SKILL_EXEC_EXTENSION: skill {skill_name!r} is a "
            "type=extension plugin and does not execute through the "
            "subprocess substrate. Its ``register(api)`` has already "
            "been called; the loader registered whatever ``plugin.py`` "
            "declared (inspect via the snapshot produced by "
            "``ouroboros.skills.extensions.loader.snapshot()``). Use its "
            "provider-safe ``ext_<len>_<token>_*`` tools, "
            "``/api/extensions/<skill>/...`` routes, or provider-safe "
            "extension WebSocket handlers instead."
        )
    # skill_exec only executes manifest script payloads.
    if not loaded.manifest.is_script():
        return (
            f"⚠️ SKILL_EXEC_ERROR: skill {skill_name!r} has type "
            f"{loaded.manifest.type!r}. Only 'script' skills can execute "
            "via skill_exec in Phase 3."
        )
    trusted_permissions = set(trusted_permissions_for_skill(loaded))

    runtime = (loaded.manifest.runtime or "").strip().lower()
    runtime_binary = _resolve_runtime_binary(runtime)
    if runtime_binary is None:
        return (
            f"⚠️ SKILL_EXEC_ERROR: skill {skill_name!r} declared runtime "
            f"{runtime!r} is not in the allowlist {sorted(set(_ALLOWED_RUNTIMES))} "
            "or the matching binary is not on PATH."
        )

    def _canonical_declared_path(declared_name: str) -> pathlib.Path | None:
        rel = canonical_declared_script_rel(declared_name)
        if rel is None:
            return None
        return _resolve_script_path(loaded.skill_dir, rel.as_posix())

    declared_scripts: list[pathlib.Path] = []
    declared_by_name: dict[str, pathlib.Path] = {}
    for entry in loaded.manifest.scripts or []:
        if not isinstance(entry, dict):
            continue
        declared_name = str(entry.get("name") or "").strip()
        if not declared_name:
            continue
        canonical = _canonical_declared_path(declared_name)
        if canonical is None:
            continue
        if canonical not in declared_scripts:
            declared_scripts.append(canonical)
        declared_by_name[declared_name] = canonical
        # Accept both bare and explicit scripts/<name> caller spellings.
        if "/" not in declared_name:
            declared_by_name[f"scripts/{declared_name}"] = canonical

    # Match declared names first, then equivalent reviewed relative paths.
    script_path: pathlib.Path | None = declared_by_name.get(script_rel.strip())
    if script_path is None:
        script_path = _resolve_script_path(loaded.skill_dir, script_rel, allowed_paths=declared_scripts)
    if script_path is None:
        return (
            f"⚠️ SKILL_EXEC_ERROR: script {script_rel!r} is not a declared "
            "script for this skill. Only names listed under the manifest's "
            "``scripts:`` array can execute via skill_exec."
        )

    script_args: list[str] = []
    if args is None:
        extra_args: list[Any] = []
    elif isinstance(args, str):
        # Reject single string args; list(args) would become per-character argv.
        return (
            "⚠️ SKILL_EXEC_ERROR: 'args' must be a list of scalar "
            "strings/numbers, not a single string. Wrap as ['alpha'] "
            "for a one-element argv."
        )
    elif isinstance(args, (list, tuple)):
        extra_args = list(args)
    else:
        return (
            f"⚠️ SKILL_EXEC_ERROR: 'args' must be a list of scalar strings/numbers. Got {type(args).__name__}={args!r}."
        )
    for arg in extra_args:
        if not isinstance(arg, (str, int, float)) or isinstance(arg, bool):
            return (
                "⚠️ SKILL_EXEC_ERROR: args must be a list of scalar "
                f"strings/numbers. Element {arg!r} ({type(arg).__name__}) "
                "is not allowed."
            )
        script_args.append(str(arg))

    timeout = _bound_timeout(loaded.manifest.timeout_sec)

    state_dir = skill_state_dir(drive_root, loaded.skill_id)
    # A per-call directory, only for skills that declared fs: the task's drive artifacts named in argv
    # are copied in before the run and every file the script leaves is published after it.
    workdir: pathlib.Path | None = None
    if "fs" in trusted_permissions:
        (drive_root / "tmp").mkdir(parents=True, exist_ok=True)
        workdir = pathlib.Path(tempfile.mkdtemp(prefix="ouroboros-skill-", dir=drive_root / "tmp"))
    env = _scrub_env(
        manifest_env_keys=list(loaded.manifest.env_from_settings or []),
        skill_state_dir_path=state_dir,
        skill_name=loaded.name,
        trusted_setting_keys=trusted_setting_keys_for_skill(loaded),
        workdir=workdir,
    )
    settings = load_settings()
    extra_deny = tuple(pathlib.Path(p) for p in settings.OUROBOROS_SANDBOX_DENY_PATHS)
    stdout_cap = _bound_stream_cap(settings.OUROBOROS_SKILL_EXEC_MAX_STDOUT_BYTES, _MAX_STDOUT_BYTES)
    stderr_cap = _bound_stream_cap(settings.OUROBOROS_SKILL_EXEC_MAX_STDERR_BYTES, _MAX_STDERR_BYTES)
    network = NetworkPolicy.ALLOW if "net" in trusted_permissions else NetworkPolicy.DENY
    policy = dataclasses.replace(
        skill_default_policy(cwd=loaded.skill_dir, extra_deny=extra_deny),
        timeout_sec=timeout,
        stdout_cap=stdout_cap,
        stderr_cap=stderr_cap,
        network=network,
    )

    inputs: set[pathlib.Path] = set()
    artifacts: list[str] = []
    try:
        if workdir is not None:
            for index, arg in enumerate(script_args):
                local, error = materialize(ctx, arg, workdir)
                if error:
                    return f"⚠️ SKILL_EXEC_ERROR: {error.removeprefix('⚠️ ')}"
                if local is not None:
                    # The script sees the file by its bare name; the drive path stays the model's handle.
                    script_args[index] = local.name
                    inputs.add(local)
                    inputs.add(provenance_path(local))
        cmd = [runtime_binary, str(script_path), *script_args]
        try:
            result = SandboxRunner().run(cmd, policy, env=env, stop_requested=ctx.stop_requested)
        except FileNotFoundError:
            _emit_skill_lifecycle_event(
                ctx,
                event_type="skill_exec_failed",
                skill=loaded.name,
                script=script_rel,
                error=f"runtime binary {runtime_binary!r} unavailable",
            )
            return f"⚠️ SKILL_EXEC_ERROR: runtime binary {runtime_binary!r} is no longer available."
        except OSError as exc:
            _emit_skill_lifecycle_event(
                ctx,
                event_type="skill_exec_failed",
                skill=loaded.name,
                script=script_rel,
                error=f"OS error running skill: {exc}",
            )
            return f"⚠️ SKILL_EXEC_ERROR: OS error running skill: {exc}"
        if workdir is not None and result.returncode == 0 and not (result.timed_out or result.overflowed):
            produced = [path for path in sorted(workdir.rglob("*")) if path.is_file() and path not in inputs]
            try:
                artifacts = publish(ctx, ctx.scope, workdir, produced)
            except ValueError as exc:
                return f"⚠️ SKILL_EXEC_ERROR: {exc}"
    finally:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)

    for event in result.audit_events:
        try:
            ctx.events.audit("events", event["type"], event, scope=None)
        except Exception:
            log.debug("Could not record sandbox audit event", exc_info=True)

    if result.denied:
        _emit_skill_lifecycle_event(
            ctx,
            event_type="skill_exec_failed",
            skill=loaded.name,
            script=script_rel,
            error=result.denial_reason or "denied by sandbox policy",
        )
        return f"⚠️ SKILL_EXEC_DENIED: {result.denial_reason}"

    if result.cancelled:
        return f"⚠️ SKILL_EXEC_CANCELLED: task stop terminated skill {skill_name!r} script {script_rel!r}."

    if result.timed_out:
        _emit_skill_lifecycle_event(
            ctx,
            event_type="skill_exec_failed",
            skill=loaded.name,
            script=script_rel,
            error=f"timeout after {policy.timeout_sec}s",
        )
        return (
            f"⚠️ SKILL_EXEC_TIMEOUT: skill {skill_name!r} script "
            f"{script_rel!r} exceeded {policy.timeout_sec}s limit.\n"
            f"stdout_partial:\n{_cap(result.stdout, stdout_cap, 'stdout')}\n"
            f"stderr_partial:\n{_cap(result.stderr, stderr_cap, 'stderr')}"
        )

    return _render_skill_exec_result(
        ctx,
        payload={
            "skill": loaded.name,
            "script": script_rel,
            "runtime": runtime,
            "exit_code": int(result.returncode),
            "timeout_sec": policy.timeout_sec,
            "artifacts": artifacts,
        },
        stdout_bytes=result.stdout,
        stderr_bytes=result.stderr,
        overflowed=result.overflowed,
        stdout_cap=stdout_cap,
        stderr_cap=stderr_cap,
    )


# Tool registrations

_EXEC_SCHEMA = ToolSchema(
    name="skill_exec",
    description=(
        "Run a script that ships inside an installed skill package. The skill "
        "must be selected and eligible, and the script argument must "
        "match an entry declared in the skill's manifest. Only type=script "
        "skills run through this tool; type=extension skills expose their own "
        "tools instead. Files a script produces come back as drive paths under "
        "'artifacts' (uploads/...): pass one to send_file to deliver it, or "
        "back into args of a later skill_exec call to let a script read it."
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "Canonical source-qualified skill id from the available-skills index.",
            },
            "script": {
                "type": "string",
                "description": (
                    "Relative path of the script inside the skill directory "
                    "(e.g. 'scripts/fetch.py'). Absolute paths and '..' "
                    "traversal are rejected."
                ),
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional argv for the script.",
            },
        },
        "required": ["skill", "script"],
    },
)


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="skill_exec",
            schema=_EXEC_SCHEMA,
            handler=_handle_skill_exec,
            is_code_tool=False,
            timeout_sec=_HARD_TIMEOUT_CEILING_SEC,
        ),
    ]


__all__ = [
    "_ALLOWED_RUNTIMES",
    "_HARD_TIMEOUT_CEILING_SEC",
    "get_tools",
]
