"""Read-only skill payload syntax/contract preflight for heal-mode agents.

Preflight uses argv-only subprocesses, cwd=skill_dir, scrubbed env, 30s timeout,
in-process Python compile(), and does not mutate enablement or grant state.
"""

from __future__ import annotations

import ast
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
from ouroboros.contracts.sandbox_policy import preflight_default_policy
from ouroboros.contracts.skill_manifest import (
    SkillManifest,
    SkillManifestError,
    parse_skill_manifest_text,
)
from ouroboros.observability.event_record import record_event
from ouroboros.sandbox.runner import SandboxRunner

log = logging.getLogger(__name__)

_PREFLIGHT_MAX_OUTPUT_BYTES = 16 * 1024
_PREFLIGHT_HARD_FILE_LIMIT = 60  # mirrors skill_review's _MAX_SKILL_FILES headroom

# Extension -> argv template + runtime; {path} is substituted into argv only.
_VALIDATORS: dict[str, tuple[list[str], str]] = {
    ".js": (["node", "--check", "{path}"], "node"),
    ".mjs": (["node", "--check", "{path}"], "node"),
    ".cjs": (["node", "--check", "{path}"], "node"),
    ".sh": (["bash", "-n", "{path}"], "bash"),
    ".bash": (["bash", "-n", "{path}"], "bash"),
}


def _resolve_runtime(runtime: str) -> str | None:
    if runtime == "python3":
        path = shutil.which("python3") or shutil.which("python")
    else:
        path = shutil.which(runtime)
    return path


def _preflight_scrub_env() -> dict[str, str]:
    """Minimal OS env for validator subprocesses; no host secrets merged by runner."""
    env: dict[str, str] = {}
    for key in ("PATH", "HOME", "LANG"):
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    env.setdefault("LANG", "C.UTF-8")
    return env


def _decode_capped(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")[:_PREFLIGHT_MAX_OUTPUT_BYTES]


def _operator_extra_deny_paths() -> tuple[pathlib.Path, ...]:
    return tuple(pathlib.Path(p) for p in load_settings().OUROBOROS_SANDBOX_DENY_PATHS)


def _record_sandbox_audit_events(drive_root: pathlib.Path, audit_events: list[dict]) -> None:
    for event in audit_events:
        try:
            record_event(drive_root, "events", event["type"], event)
        except Exception:
            log.debug("Could not record sandbox audit event", exc_info=True)


def _run_check(
    cmd: list[str],
    cwd: pathlib.Path,
    *,
    drive_root: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Run validator argv through SandboxRunner; never raises."""
    policy = preflight_default_policy(cwd=cwd, extra_deny=_operator_extra_deny_paths())
    try:
        result = SandboxRunner().run(cmd, policy, env=_preflight_scrub_env())
    except FileNotFoundError as exc:
        return {"returncode": -1, "stdout": "", "stderr": f"runtime not found: {exc}", "timeout": False}
    if drive_root is not None:
        _record_sandbox_audit_events(drive_root, result.audit_events)
    if result.denied:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": result.denial_reason or "denied by sandbox policy",
            "timeout": False,
        }
    if result.timed_out:
        return {
            "returncode": -9,
            "stdout": _decode_capped(result.stdout),
            "stderr": _decode_capped(result.stderr),
            "timeout": True,
        }
    return {
        "returncode": int(result.returncode or 0),
        "stdout": _decode_capped(result.stdout),
        "stderr": _decode_capped(result.stderr),
        "timeout": False,
    }


def _run_python_syntax_check(path: pathlib.Path) -> dict[str, Any]:
    """Use compile() so Python syntax checks stay read-only."""
    try:
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        return {"returncode": 0, "stdout": "", "stderr": "", "timeout": False}
    except Exception as exc:
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
            "timeout": False,
        }


def _validate_widget_render(render: Any, *, source: str) -> dict[str, Any]:
    """Headless runtime does not host browser widget surfaces."""
    del render
    return {
        "item": "widget_schema",
        "source": source,
        "ok": False,
        "detail": "widget/ui_tab render is not supported in headless runtime",
    }


def _literal_widget_renders_from_plugin(plugin_path: pathlib.Path) -> list[dict[str, Any]]:
    """Extract literal top-level widget render dicts without importing plugin.py."""
    try:
        tree = ast.parse(plugin_path.read_text(encoding="utf-8"), filename=str(plugin_path))
    except Exception:
        return []
    renders: list[dict[str, Any]] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        try:
            value = ast.literal_eval(node.value)
        except Exception:
            continue
        if not isinstance(value, dict):
            continue
        kind = str(value.get("kind") or "").strip()
        if kind not in {"declarative", "module", "iframe"}:
            continue
        targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
        source = targets[0] if targets else f"line {getattr(node, 'lineno', '?')}"
        renders.append({"source": source, "render": value})
    return renders


def _widget_schema_findings(skill_dir: pathlib.Path, manifest: SkillManifest | None) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if manifest is not None and isinstance(manifest.ui_tab, dict):
        render = manifest.ui_tab.get("render")
        findings.append(_validate_widget_render(render, source="manifest.ui_tab.render"))
    plugin = skill_dir / "plugin.py"
    if plugin.is_file():
        for item in _literal_widget_renders_from_plugin(plugin):
            findings.append(
                _validate_widget_render(
                    item.get("render"),
                    source=f"plugin.py:{item.get('source')}",
                )
            )
    return findings


def _plugin_permission_findings(skill_dir: pathlib.Path, manifest: SkillManifest | None) -> list[dict[str, Any]]:
    """Statically catch common PluginAPI calls whose manifest permission is missing."""
    if manifest is None or not manifest.is_extension():
        return []
    plugin = skill_dir / (manifest.entry or "plugin.py")
    if not plugin.is_file():
        return []
    required_by_call = {
        "register_tool": "tool",
        "inject_chat": "inject_chat",
        "subscribe_event": "subscribe_event",
        "register_companion_process": "companion_process",
        "register_supervised_task": "supervised_task",
        "get_settings": "read_settings",
    }
    try:
        tree = ast.parse(plugin.read_text(encoding="utf-8"), filename=str(plugin))
    except Exception:
        return []
    seen: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            perm = required_by_call.get(func.attr)
            if perm and perm not in seen:
                seen[perm] = getattr(node, "lineno", 0)
    declared = set(manifest.permissions or [])
    findings: list[dict[str, Any]] = []
    for perm, line in sorted(seen.items()):
        findings.append(
            {
                "item": "permission_static",
                "source": f"{plugin.name}:{line}" if line else plugin.name,
                "permission": perm,
                "ok": perm in declared,
                "detail": "ok" if perm in declared else f"plugin calls PluginAPI surface requiring permission {perm!r}",
            }
        )
    return findings


def _handle_skill_preflight(
    ctx: ToolContext,
    skill: str = "",
    paths: list[str] | None = None,
    **_kwargs: Any,
) -> str:
    skill_name = str(skill or "").strip()
    if not skill_name:
        return "⚠️ SKILL_PREFLIGHT_ERROR: 'skill' is required."

    from ouroboros.skills.loader import find_skill

    drive_root = pathlib.Path(ctx.drive_root)
    loaded = find_skill(drive_root, skill_name)
    if loaded is None:
        return f"⚠️ SKILL_PREFLIGHT_ERROR: skill {skill_name!r} not found."

    skill_dir = loaded.skill_dir.resolve()

    # Broken manifests still become findings; keep other validators running.
    manifest_findings: list[dict[str, Any]] = []
    widget_findings: list[dict[str, Any]] = []
    permission_findings: list[dict[str, Any]] = []
    manifest: SkillManifest | None = None
    manifest_path = None
    for candidate in ("SKILL.md", "skill.json"):
        cand = skill_dir / candidate
        if cand.is_file():
            manifest_path = cand
            break
    if manifest_path is None:
        manifest_findings.append({"item": "manifest_present", "ok": False, "detail": "no SKILL.md / skill.json"})
    else:
        try:
            text = manifest_path.read_text(encoding="utf-8")
            manifest = parse_skill_manifest_text(text)
            manifest_findings.append({"item": "manifest_parse", "ok": True, "detail": "ok"})
            widget_findings.extend(_widget_schema_findings(skill_dir, manifest))
            permission_findings.extend(_plugin_permission_findings(skill_dir, manifest))
            if manifest.entry:
                entry = (skill_dir / manifest.entry).resolve()
                ok = entry.is_file()
                try:
                    entry.relative_to(skill_dir)
                except ValueError:
                    ok = False
                manifest_findings.append(
                    {
                        "item": "manifest_entry_exists",
                        "ok": ok,
                        "detail": manifest.entry if ok else f"missing or escaping entry: {manifest.entry}",
                    }
                )
            for script in manifest.scripts or []:
                name = str(script.get("name") or "").strip()
                if not name:
                    continue
                rel = name if "/" in name or name.startswith(".") else f"scripts/{name}"
                script_path = (skill_dir / rel).resolve()
                ok = script_path.is_file()
                try:
                    script_path.relative_to(skill_dir)
                except ValueError:
                    ok = False
                manifest_findings.append(
                    {
                        "item": "manifest_script_exists",
                        "ok": ok,
                        "detail": rel if ok else f"missing or escaping script: {rel}",
                    }
                )
        except (OSError, UnicodeDecodeError, SkillManifestError) as exc:
            manifest_findings.append(
                {
                    "item": "manifest_parse",
                    "ok": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            widget_findings.extend(_widget_schema_findings(skill_dir, None))

    # paths scopes recent edits; otherwise walk the reviewable payload surface.
    files_to_check: list[pathlib.Path] = []
    path_findings: list[dict[str, Any]] = []
    if paths:
        for raw in paths:
            rel = str(raw or "").strip()
            if not rel or rel.startswith(("/", "~")) or ".." in rel.split("/"):
                path_findings.append(
                    {
                        "path": rel,
                        "runtime": "",
                        "ok": False,
                        "stderr": "invalid or escaping path",
                        "stdout": "",
                        "timeout": False,
                    }
                )
                continue
            target = (skill_dir / rel).resolve()
            try:
                target.relative_to(skill_dir)
            except ValueError:
                path_findings.append(
                    {
                        "path": rel,
                        "runtime": "",
                        "ok": False,
                        "stderr": "path escapes skill directory",
                        "stdout": "",
                        "timeout": False,
                    }
                )
                continue
            if target.is_file():
                files_to_check.append(target)
            else:
                path_findings.append(
                    {
                        "path": rel,
                        "runtime": "",
                        "ok": False,
                        "stderr": "path not found",
                        "stdout": "",
                        "timeout": False,
                    }
                )
    else:
        try:
            from ouroboros.skills.loader import _iter_payload_files  # pylint: disable=W0212

            for path in _iter_payload_files(
                skill_dir,
                manifest_entry=loaded.manifest.entry,
                manifest_scripts=loaded.manifest.scripts,
            ):
                files_to_check.append(path.resolve())
        except Exception as exc:
            log.debug("preflight discovery failed", exc_info=True)
            return f"⚠️ SKILL_PREFLIGHT_ERROR: payload discovery failed: {exc}"

    omitted_count = 0
    omitted_files: list[str] = []
    if len(files_to_check) > _PREFLIGHT_HARD_FILE_LIMIT:
        omitted = files_to_check[_PREFLIGHT_HARD_FILE_LIMIT:]
        omitted_count = len(omitted)
        omitted_files = [str(path.relative_to(skill_dir)) for path in omitted[:20]]
        files_to_check = files_to_check[:_PREFLIGHT_HARD_FILE_LIMIT]

    file_findings: list[dict[str, Any]] = list(path_findings)
    for path in files_to_check:
        suffix = path.suffix.lower()
        if suffix == ".py":
            result = _run_python_syntax_check(path)
            ok = result["returncode"] == 0 and not result["timeout"]
            file_findings.append(
                {
                    "path": str(path.relative_to(skill_dir)),
                    "runtime": "python",
                    "ok": ok,
                    "returncode": result["returncode"],
                    "timeout": result["timeout"],
                    "stderr": result["stderr"][:2000],
                    "stdout": result["stdout"][:2000],
                }
            )
            continue
        validator = _VALIDATORS.get(suffix)
        if validator is None:
            continue
        argv_template, runtime = validator
        runtime_path = _resolve_runtime(runtime)
        if runtime_path is None:
            file_findings.append(
                {
                    "path": str(path.relative_to(skill_dir)),
                    "runtime": runtime,
                    "ok": False,
                    "detail": f"runtime {runtime!r} is not on PATH (skipped)",
                    "skipped": True,
                }
            )
            continue
        cmd = [runtime_path] + [str(path) if part == "{path}" else part for part in argv_template[1:]]
        result = _run_check(cmd, cwd=skill_dir, drive_root=drive_root)
        ok = result["returncode"] == 0 and not result["timeout"]
        file_findings.append(
            {
                "path": str(path.relative_to(skill_dir)),
                "runtime": runtime,
                "ok": ok,
                "returncode": result["returncode"],
                "timeout": result["timeout"],
                "stderr": result["stderr"][:2000],
                "stdout": result["stdout"][:2000],
            }
        )

    overall_ok = (
        all(f.get("ok") for f in manifest_findings)
        and all(f.get("ok") for f in widget_findings)
        and all(f.get("ok") for f in permission_findings)
        and all(f.get("ok") for f in file_findings)
        and omitted_count == 0
        and (not paths or any(f.get("ok") for f in file_findings))
    )
    payload = {
        "skill": skill_name,
        "skill_dir": str(skill_dir),
        "manifest": manifest_findings,
        "widgets": widget_findings,
        "permissions": permission_findings,
        "files": file_findings,
        "files_checked": len(file_findings),
        "files_failed": sum(1 for f in file_findings if not f.get("ok") and not f.get("skipped")),
        "omitted_count": omitted_count,
        "omitted_files": omitted_files,
        "ok": bool(overall_ok),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


_PREFLIGHT_SCHEMA = ToolSchema(
    name="skill_preflight",
    description=(
        "Read-only integrity check for one installed skill package. Validates each "
        "reviewable payload file (or just the ones in `paths` if provided), plus a "
        "manifest parse and static widget render-schema validation. Cheap and "
        "offline (no LLM, no review.json mutation, no review status change). Use it "
        "to confirm a skill is intact before running it. Argv-only "
        "subprocess invocation, cwd=skill_dir, scrubbed env, 30s "
        "per-file cap, panic-tracked process group."
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "Skill name (directory basename in the skills tree).",
            },
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional subset of payload files (relative to the "
                    "skill dir) to validate. Empty = walk the same "
                    "surface skill_review reads."
                ),
            },
        },
        "required": ["skill"],
    },
)


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="skill_preflight",
            schema=_PREFLIGHT_SCHEMA,
            handler=_handle_skill_preflight,
            is_code_tool=False,
            timeout_sec=120,
        ),
    ]


__all__ = ["get_tools"]
