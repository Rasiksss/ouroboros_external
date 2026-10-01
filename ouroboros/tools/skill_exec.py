"""Skill execution substrate and skill lifecycle tools.

``skill_exec`` runs only manifest-declared scripts from enabled skills with
grants satisfied. It uses a scrubbed env, runtime allowlist, cwd confinement,
timeout, byte caps, and panic-tracked process groups.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import pathlib
import shutil
from typing import Any

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.app.config import load_settings
from ouroboros.contracts.plugin_api import FORBIDDEN_SKILL_SETTINGS
from ouroboros.contracts.sandbox_policy import skill_default_policy
from ouroboros.observability.event_record import record_event
from ouroboros.sandbox.runner import SandboxRunner
from ouroboros.skills.loader import (
    discover_skills,
    find_skill,
    grant_status_for_skill,
)

log = logging.getLogger(__name__)

# Execution policy

# Hard ceiling: skill_exec is bounded/synchronous, not a worker substrate.
_HARD_TIMEOUT_CEILING_SEC = 300
_DEFAULT_TIMEOUT_SEC = 60
# Stream caps pair with tool_capabilities' 300k-char result limit.
_MAX_STDOUT_BYTES = 256 * 1024
_MAX_STDERR_BYTES = 128 * 1024

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

# OS-level env needed for basic runtimes; app secrets require explicit grants.
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

# Core settings keys require explicit content-bound owner grants.
_FORBIDDEN_ENV_FORWARD_KEYS = FORBIDDEN_SKILL_SETTINGS


def _resolve_runtime_binary(runtime: str) -> str | None:
    """Return the runtime binary path, falling back to sys.executable for Python."""
    import sys

    candidates = _ALLOWED_RUNTIMES.get(runtime or "", ())
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    # Packaged builds can rely on the bundled Python even when PATH lacks one.
    if runtime in ("python", "python3") and sys.executable:
        bundled_python = pathlib.Path(sys.executable)
        if bundled_python.is_file():
            return str(bundled_python)
    return None


def _scrub_env(
    manifest_env_keys: list[str],
    skill_state_dir_path: pathlib.Path,
    skill_name: str,
    granted_keys: list[str] | None = None,
) -> dict[str, str]:
    """Build a minimal subprocess env from OS keys and granted settings."""
    env: dict[str, str] = {}
    for key in _ALWAYS_FORWARDED_ENV:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    if manifest_env_keys:
        settings = load_settings()
        # Compare protected keys case-insensitively so lowercase aliases fail closed.
        from ouroboros.skills.loader import requested_core_setting_keys

        protected_upper = {k.upper() for k in _FORBIDDEN_ENV_FORWARD_KEYS}
        protected_upper.update(requested_core_setting_keys(list(manifest_env_keys or [])))
        granted_upper = {str(k).strip().upper() for k in (granted_keys or []) if str(k).strip()}
        allow = {str(k).strip() for k in manifest_env_keys if str(k).strip()}
        for key in allow:
            canonical = key.upper()
            if canonical in protected_upper and canonical not in granted_upper:
                log.warning(
                    "Skill %s asked env_from_settings for %s; refusing without explicit grant.",
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
) -> str:
    skill_name = str(payload.get("skill") or "")
    script_rel = str(payload.get("script") or "")
    returncode = int(payload.get("exit_code") or 0)
    payload = {
        **payload,
        "output_overflow": overflowed,
        "stdout": _cap(stdout_bytes, _MAX_STDOUT_BYTES, "stdout"),
        "stderr": _cap(stderr_bytes, _MAX_STDERR_BYTES, "stderr"),
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
            f"(stdout<={_MAX_STDOUT_BYTES}B, stderr<={_MAX_STDERR_BYTES}B) "
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


def _skill_tool_preflight(
    ctx: ToolContext,
) -> str | None:
    """Return an error when the distribution contains no skills."""
    if discover_skills(
        pathlib.Path(getattr(ctx, "drive_root", "")),
        workspace_root=pathlib.Path(getattr(ctx, "repo_dir", pathlib.Path.cwd())),
    ):
        return None
    return "⚠️ SKILLS_UNAVAILABLE: No skills were found under OUROBOROS_REPO_DIR/skills."


def _skill_deps_exec_block(drive_root: pathlib.Path, loaded: Any) -> str:
    """Return SKILL_EXEC_BLOCKED when isolated deps are stale or missing."""
    try:
        from ouroboros.skills.dependencies import auto_install_specs_for_skill
        from ouroboros.skills.install_specs import install_specs_hash as _specs_hash
        from ouroboros.skills.isolated_deps import read_deps_state

        auto_specs = auto_install_specs_for_skill(drive_root, loaded)
        if not auto_specs:
            return ""
        deps_state = read_deps_state(drive_root, loaded.name)
        deps_status = str(deps_state.get("status") or "pending")
        if deps_status == "installed" and deps_state.get("specs_hash") == _specs_hash(auto_specs):
            return ""
        return (
            f"⚠️ SKILL_EXEC_BLOCKED: skill {loaded.name!r} isolated "
            f"dependencies are not ready (status={deps_status!r}). "
            "Install isolated dependencies before executing this skill."
        )
    except Exception:
        log.debug("skill_exec deps readiness probe failed", exc_info=True)
        return ""


def _handle_skill_exec(
    ctx: ToolContext,
    skill: str = "",
    script: str = "",
    args: list[str] | None = None,
    **_kwargs: Any,
) -> str:
    # Runtime mode gates repo mutation, not reviewed + enabled skill execution.

    skill_name = str(skill or "").strip()
    script_rel = str(script or "").strip()
    if not skill_name or not script_rel:
        return "⚠️ SKILL_EXEC_ERROR: both 'skill' and 'script' are required."
    err = _skill_tool_preflight(ctx)
    if err:
        return err

    drive_root = pathlib.Path(ctx.drive_root)
    loaded = find_skill(drive_root, skill_name, workspace_root=pathlib.Path(ctx.repo_dir))
    if loaded is None:
        return f"⚠️ SKILL_EXEC_ERROR: skill {skill_name!r} not found in OUROBOROS_REPO_DIR/skills."
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
    if not loaded.enabled:
        return (
            f"⚠️ SKILL_EXEC_BLOCKED: skill {skill_name!r} is disabled. Enable it through the owner/admin skill surface."
        )
    grants = grant_status_for_skill(drive_root, loaded)
    if not grants.get("all_granted", True):
        missing = ", ".join(grants.get("missing_keys") or [])
        missing_perms = ", ".join(grants.get("missing_permissions") or [])
        return (
            f"⚠️ SKILL_EXEC_BLOCKED: skill {skill_name!r} is missing grants "
            f"(keys={missing or 'none'}, permissions={missing_perms or 'none'})."
        )
    deps_block = _skill_deps_exec_block(drive_root, loaded)
    if deps_block:
        return deps_block

    runtime = (loaded.manifest.runtime or "").strip().lower()
    runtime_binary = _resolve_runtime_binary(runtime)
    try:
        from ouroboros.skills.isolated_deps import python_runtime_binary

        if runtime in {"python", "python3"}:
            isolated_python = python_runtime_binary(loaded.skill_dir)
            if isolated_python is not None:
                runtime_binary = str(isolated_python)
    except Exception:
        log.debug("Could not resolve isolated Python runtime", exc_info=True)
    if runtime_binary is None:
        return (
            f"⚠️ SKILL_EXEC_ERROR: skill {skill_name!r} declared runtime "
            f"{runtime!r} is not in the allowlist {sorted(set(_ALLOWED_RUNTIMES))} "
            "or the matching binary is not on PATH."
        )

    # Executable surface is exactly manifest.scripts. Bare names mean
    # scripts/<name>, never a same-named top-level shadow file.
    def _canonical_declared_path(declared_name: str) -> pathlib.Path | None:
        name = declared_name.strip()
        if not name:
            return None
        if "/" in name or name.startswith("."):
            return _resolve_script_path(loaded.skill_dir, name)
        # Bare names always resolve under scripts/.
        return _resolve_script_path(loaded.skill_dir, f"scripts/{name}")

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

    cmd = [runtime_binary, str(script_path)]
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
        cmd.append(str(arg))

    timeout = _bound_timeout(loaded.manifest.timeout_sec)
    from ouroboros.skills.loader import skill_state_dir

    state_dir = skill_state_dir(drive_root, loaded.name)
    grants = grant_status_for_skill(drive_root, loaded)
    missing_core = list(grants.get("missing_keys") or [])
    if missing_core:
        return (
            "⚠️ SKILL_EXEC_GRANT_REQUIRED: skill "
            f"{loaded.name!r} requests core settings keys {missing_core}. "
            "Grant them from the admin surface before execution."
        )
    env = _scrub_env(
        manifest_env_keys=list(loaded.manifest.env_from_settings or []),
        skill_state_dir_path=state_dir,
        skill_name=loaded.name,
        granted_keys=list(grants.get("granted_keys") or []),
    )
    try:
        from ouroboros.skills.isolated_deps import augment_env_for_skill_deps

        env = augment_env_for_skill_deps(env, loaded.skill_dir)
    except Exception:
        log.debug("Could not augment skill env with isolated dependencies", exc_info=True)

    extra_deny = tuple(pathlib.Path(p) for p in load_settings().OUROBOROS_SANDBOX_DENY_PATHS)
    policy = dataclasses.replace(
        skill_default_policy(cwd=loaded.skill_dir, extra_deny=extra_deny),
        timeout_sec=timeout,
    )

    try:
        result = SandboxRunner().run(cmd, policy, env=env)
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

    for event in result.audit_events:
        try:
            record_event(pathlib.Path(ctx.drive_root), "events", event["type"], event)
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
            f"stdout_partial:\n{_cap(result.stdout, _MAX_STDOUT_BYTES, 'stdout')}\n"
            f"stderr_partial:\n{_cap(result.stderr, _MAX_STDERR_BYTES, 'stderr')}"
        )

    return _render_skill_exec_result(
        ctx,
        payload={
            "skill": loaded.name,
            "script": script_rel,
            "runtime": runtime,
            "exit_code": int(result.returncode),
            "timeout_sec": policy.timeout_sec,
        },
        stdout_bytes=result.stdout,
        stderr_bytes=result.stderr,
        overflowed=result.overflowed,
    )


# Tool registrations

_EXEC_SCHEMA = ToolSchema(
    name="skill_exec",
    description=(
        "Run a script that ships inside an installed skill package. The skill "
        "must be enabled with grants satisfied, and the script argument must "
        "match an entry declared in the skill's manifest. Only type=script "
        "skills run through this tool; type=extension skills expose their own "
        "tools instead."
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "Skill name (directory name in OUROBOROS_REPO_DIR/skills).",
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
