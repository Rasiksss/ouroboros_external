"""Declarative metadata for built-in tool policy and LLM surface."""

from __future__ import annotations

import re
from dataclasses import replace

from ouroboros.storage.settings.schema import get_defaults
from ouroboros.storage.settings.store import load_settings
from ouroboros.tools.spec import CapabilityClass, ToolSpec

DEFAULT_TOOL_RESULT_LIMIT = 15_000
# Hard ceiling for any declared per-tool result cap (extension ``result_limit``
# is rejected at registration above this; matches the largest built-in cap).
RESULT_LIMIT_HARD_MAX = 120_000
SKILL_READ_RESULT_LIMIT = 80_000
# Values a skill or extension may declare (#84) — a narrowing of the core
# ToolSafetyPolicy/CapabilityClass vocabulary; shared by both registration
# validators (extension loader and the type=tool skill provider).
DECLARABLE_SAFETY_POLICIES = frozenset({"check", "skip"})
DECLARABLE_CAPABILITY_CLASSES = frozenset({"read_only", "mutative", "external_network"})
#: The five WorkStation-backed tools (workstation spec §19.1), plus the
#: ``workstation_connect`` entry point.  All six are added to the initial
#: tool set when the WorkStation integration is configured
#: (``WORKSTATION_BASE_URL`` non-empty), regardless of whether a session
#: is live — the five file/exec tools return ``TOOL_UNAVAILABLE`` until
#: ``workstation_connect`` has been called this turn.
WORKSTATION_TOOL_NAMES = frozenset(
    {
        "workstation_connect",
        "code_exec",
        "file_read",
        "file_write",
        "file_edit",
        "apply_patch",
    }
)


def _spec(
    name: str,
    *,
    category: str = "task",
    surface: str = "discoverable",
    safety_policy: str = "skip",
    capability_class: CapabilityClass = "read_only",
    parallel: bool = False,
    result_limit: int | None = None,
    untruncated_result: bool = False,
    timeout_sec: int = 360,
    is_code_tool: bool = False,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        category=category,  # type: ignore[arg-type]
        surface=surface,  # type: ignore[arg-type]
        safety_policy=safety_policy,  # type: ignore[arg-type]
        capability_class=capability_class,
        parallel=parallel,
        result_limit=result_limit,
        untruncated_result=untruncated_result,
        timeout_sec=timeout_sec,
        is_code_tool=is_code_tool,
    )


TOOL_SPECS: dict[str, ToolSpec] = {
    "task_plan": _spec(
        "task_plan",
        category="service",
        surface="core",
        capability_class="mutative",
        result_limit=120_000,
        untruncated_result=True,
    ),
    # Core headless surface.
    "data_read": _spec("data_read", surface="core", parallel=True, result_limit=80_000),
    "data_list": _spec("data_list", surface="core", parallel=True),
    "data_write": _spec("data_write", surface="core", capability_class="mutative"),
    "recent_tasks": _spec("recent_tasks", surface="core", parallel=True, result_limit=80_000),
    "memory_save": _spec("memory_save", category="memory", surface="core", capability_class="mutative"),
    "memory_list": _spec("memory_list", category="memory", surface="core"),
    "memory_read": _spec("memory_read", category="memory", surface="core", result_limit=80_000),
    "memory_delete": _spec("memory_delete", category="memory", surface="core", capability_class="mutative"),
    "list_skills": _spec("list_skills", category="execution", surface="core", parallel=True),
    "check_skills": _spec("check_skills", category="execution", surface="core", parallel=True),
    "skill_read": _spec(
        "skill_read",
        category="execution",
        surface="core",
        capability_class="read_only",
        parallel=True,
        result_limit=SKILL_READ_RESULT_LIMIT,
    ),
    "chat_history": _spec("chat_history", category="memory", surface="core", parallel=True),
    "attachment_read": _spec(
        "attachment_read",
        surface="core",
        parallel=True,
        result_limit=100_000,
        timeout_sec=get_defaults().OUROBOROS_ATTACHMENT_READ_TIMEOUT_SEC,
    ),
    "calendar_resolve": _spec("calendar_resolve", surface="core", parallel=True, timeout_sec=10),
    "gigasearch": _spec(
        "gigasearch",
        category="network",
        surface="core",
        capability_class="external_network",
        parallel=True,
        result_limit=30_000,
        timeout_sec=180,
    ),
    "update_scratchpad": _spec(
        "update_scratchpad",
        category="service",
        surface="core",
        capability_class="mutative",
    ),
    "send_user_message": _spec(
        "send_user_message",
        category="service",
        surface="core",
        capability_class="mutative",
    ),
    "send_photo": _spec("send_photo", category="service", surface="core", capability_class="mutative"),
    "send_image": _spec("send_image", category="service", surface="core", capability_class="mutative"),
    "send_file": _spec("send_file", category="service", surface="core", capability_class="mutative"),
    "list_available_tools": _spec("list_available_tools", category="service", surface="core"),
    "enable_tools": _spec("enable_tools", category="service", surface="core", capability_class="mutative"),
    "schedule_task": _spec("schedule_task", category="service", surface="core", capability_class="mutative"),
    "cancel_task": _spec("cancel_task", category="service", surface="core", capability_class="mutative"),
    "get_task_result": _spec(
        "get_task_result",
        category="service",
        surface="core",
        result_limit=120_000,
    ),
    "wait_for_task": _spec(
        "wait_for_task",
        category="service",
        surface="core",
        result_limit=120_000,
    ),
    # Scheduler lifecycle. Discoverable, not core: nine tools in every task's first prompt
    # is context spent on a capability most tasks never use.
    "create_reminder": _spec("create_reminder", category="service", capability_class="mutative", timeout_sec=20),
    "create_scheduled_task": _spec(
        "create_scheduled_task", category="service", capability_class="mutative", timeout_sec=20
    ),
    "edit_schedule": _spec("edit_schedule", category="service", capability_class="mutative", timeout_sec=20),
    "confirm_schedule": _spec("confirm_schedule", category="service", capability_class="mutative", timeout_sec=20),
    "list_schedules": _spec("list_schedules", category="service", timeout_sec=20),
    "cancel_schedule": _spec("cancel_schedule", category="service", capability_class="mutative", timeout_sec=20),
    "pause_schedule": _spec("pause_schedule", category="service", capability_class="mutative", timeout_sec=20),
    "resume_schedule": _spec("resume_schedule", category="service", capability_class="mutative", timeout_sec=20),
    "move_schedule_here": _spec("move_schedule_here", category="service", capability_class="mutative", timeout_sec=20),
    # Discoverable optional tools. They are callable only when explicitly selected.
    "calculator": _spec("calculator", parallel=True),
    "compact_context": _spec("compact_context", category="memory", timeout_sec=15),
    "skill_exec": _spec(
        "skill_exec",
        category="execution",
        safety_policy="check",
        capability_class="skill",
        result_limit=120_000,
        timeout_sec=300,
    ),
    "vlm_query": _spec("vlm_query", timeout_sec=90),
    "image_generate": _spec(
        "image_generate",
        category="network",
        capability_class="external_network",
        timeout_sec=300,
    ),
    "browse_page": _spec("browse_page", category="network", capability_class="external_network", timeout_sec=180),
    "browser_action": _spec("browser_action", category="network", capability_class="external_network", timeout_sec=180),
    # WorkStation-backed (workstation spec §19) — surface stays "discoverable" (the
    # default); gating into the initial tool set is by `WORKSTATION_BASE_URL` being
    # configured, not by a live session (see WORKSTATION_TOOL_NAMES above).
    "workstation_connect": _spec(
        "workstation_connect",
        category="execution",
        timeout_sec=30,
    ),
    "code_exec": _spec(
        "code_exec",
        category="execution",
        safety_policy="check",
        capability_class="external_shell",
        is_code_tool=True,
        timeout_sec=360,
    ),
    # 50_000, not the 80_000 used by data_read/skill payloads — spec §19.3's `read`
    # truncates at 2000 lines OR 50KB, whichever hits first; this is that 50KB cap.
    "file_read": _spec("file_read", result_limit=50_000, timeout_sec=60),
    "file_write": _spec("file_write", capability_class="mutative", timeout_sec=60),
    "file_edit": _spec("file_edit", capability_class="mutative", timeout_sec=60),
    "apply_patch": _spec("apply_patch", capability_class="mutative", timeout_sec=120),
    # Excluded from headless production surface (policy metadata only).
    "repo_read": _spec("repo_read", surface="excluded"),
    "repo_list": _spec("repo_list", surface="excluded"),
    "code_search": _spec("code_search", surface="excluded"),
    "codebase_health": _spec("codebase_health", surface="excluded"),
    "codebase_digest": _spec("codebase_digest", surface="excluded"),
    "run_shell": _spec(
        "run_shell",
        category="execution",
        surface="excluded",
        safety_policy="check",
        capability_class="shell",
    ),
    "repo_write": _spec("repo_write", surface="excluded", capability_class="git"),
    "repo_commit": _spec("repo_commit", surface="excluded", capability_class="git"),
    "str_replace_editor": _spec("str_replace_editor", surface="excluded", capability_class="git"),
    "git_status": _spec("git_status", surface="excluded", capability_class="git"),
    "git_diff": _spec("git_diff", surface="excluded", capability_class="git"),
    "rollback_to_target": _spec("rollback_to_target", surface="excluded", capability_class="control"),
    "plan_task": _spec("plan_task", surface="excluded", capability_class="control"),
    "toggle_evolution": _spec("toggle_evolution", surface="excluded", capability_class="control"),
}


def disabled_tool_names() -> frozenset[str]:
    """Tool names disabled via ``OUROBOROS_DISABLED_TOOLS`` (comma/space separated).

    Read lazily so this stays a runtime layer on top of the static ``TOOL_SPECS``
    catalog. Any failure (e.g. settings not bootstrapped yet during import) falls
    back to "nothing disabled" so the kill switch can never take tools offline by
    accident.
    """
    try:
        raw = load_settings().OUROBOROS_DISABLED_TOOLS
    except Exception:
        return frozenset()
    return frozenset(part.strip() for part in re.split(r"[\s,]+", str(raw or "")) if part.strip())


def spec_for_tool(name: str) -> ToolSpec:
    clean = str(name or "").strip()
    spec = TOOL_SPECS.get(clean, ToolSpec(name=clean))
    if clean in {"attachment_read", "send_file"}:
        settings = load_settings()
        io_budget = settings.OUROBOROS_ATTACHMENT_READ_TIMEOUT_SEC if clean == "attachment_read" else spec.timeout_sec
        spec = replace(spec, timeout_sec=io_budget + settings.attachment_parser_budget_sec)
    if clean == "skill_exec" and (limit := load_settings().OUROBOROS_SKILL_EXEC_RESULT_LIMIT) > 0:
        spec = replace(spec, result_limit=limit)
    if clean and clean in disabled_tool_names():
        return replace(spec, surface="excluded")
    return spec


def core_tool_names() -> frozenset[str]:
    return frozenset(name for name, spec in TOOL_SPECS.items() if spec.surface == "core")


def meta_tool_names() -> frozenset[str]:
    return frozenset()


def discoverable_tool_names() -> frozenset[str]:
    return frozenset(name for name, spec in TOOL_SPECS.items() if spec.surface == "discoverable")


def excluded_tool_names() -> frozenset[str]:
    return frozenset(name for name, spec in TOOL_SPECS.items() if spec.surface == "excluded")


def read_only_parallel_tool_names() -> frozenset[str]:
    return frozenset(name for name, spec in TOOL_SPECS.items() if spec.parallel)


def stateful_tool_names() -> frozenset[str]:
    return frozenset(name for name, spec in TOOL_SPECS.items() if spec.stateful)


def untruncated_tool_names() -> frozenset[str]:
    return frozenset(name for name, spec in TOOL_SPECS.items() if spec.untruncated_result)


def tool_result_limits() -> dict[str, int]:
    return {name: spec.result_limit for name, spec in TOOL_SPECS.items() if spec.result_limit is not None}


def reviewed_mutative_tool_names() -> frozenset[str]:
    return frozenset(name for name, spec in TOOL_SPECS.items() if spec.reviewed_mutative)


def safety_policy_by_tool_name() -> dict[str, str]:
    return {name: spec.safety_policy for name, spec in TOOL_SPECS.items()}


def capability_class_by_tool_name() -> dict[str, str]:
    return {name: spec.capability_class for name, spec in TOOL_SPECS.items()}
