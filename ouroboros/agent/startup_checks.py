"""Worker-boot checks for usage and memory files."""

from __future__ import annotations

import logging
from typing import Any

from ouroboros.observability.event_record import record_event
from ouroboros.storage.core_memory.memory import get_memory
from ouroboros.storage.core_memory.scratchpad import get_scratchpad
from ouroboros.utils import read_json_dict, utc_now_iso

log = logging.getLogger(__name__)


def check_usage(env: Any) -> tuple[dict, int]:
    """Check persisted token/call counters availability."""
    try:
        from ouroboros.storage.owner_state import load_owner_state_dict

        state_data = load_owner_state_dict(env.drive_root)
        return {
            "status": "ok",
            "spent_calls": int(state_data.get("spent_calls", 0) or 0),
            "prompt_tokens": int(state_data.get("spent_tokens_prompt", 0) or 0),
            "completion_tokens": int(state_data.get("spent_tokens_completion", 0) or 0),
            "cached_tokens": int(state_data.get("spent_tokens_cached", 0) or 0),
        }, 0
    except Exception as e:
        return {"status": "error", "error": str(e)}, 1


def check_review_continuations(env: Any) -> tuple[dict, int]:
    try:
        from ouroboros.agent.task_continuation import list_review_continuations
        from ouroboros.storage.task_results import (
            STATUS_CANCELLED,
            STATUS_COMPLETED,
            STATUS_FAILED,
            STATUS_INTERRUPTED,
            STATUS_REJECTED_DUPLICATE,
            STATUS_REQUESTED,
            STATUS_RUNNING,
            STATUS_SCHEDULED,
            list_task_results,
        )

        continuations, corrupt = list_review_continuations(env.drive_root)
        task_rows = list_task_results(
            env.drive_root,
            statuses=[
                STATUS_REQUESTED,
                STATUS_SCHEDULED,
                STATUS_RUNNING,
                STATUS_INTERRUPTED,
                STATUS_COMPLETED,
                STATUS_FAILED,
                STATUS_CANCELLED,
                STATUS_REJECTED_DUPLICATE,
            ],
        )
        task_by_id = {
            str(item.get("task_id") or ""): item for item in task_rows if str(item.get("task_id") or "").strip()
        }

        rows = []
        interrupted = []
        for item in continuations:
            task_status = str((task_by_id.get(item.task_id) or {}).get("status") or "")
            row = {
                "task_id": item.task_id,
                "task_status": task_status or "missing",
                "source": item.source,
                "stage": item.stage,
                "repo_key": item.repo_key,
                "tool_name": item.tool_name,
                "attempt": item.attempt,
                "block_reason": item.block_reason,
                "obligation_ids": list(item.obligation_ids or []),
                "critical_findings": len(item.critical_findings or []),
                "advisory_findings": len(item.advisory_findings or []),
                "updated_ts": item.updated_ts,
            }
            rows.append(row)
            if task_status == STATUS_INTERRUPTED:
                interrupted.append(row)

        status = "ok"
        issues = 0
        if rows or corrupt:
            status = "warning"
        if rows:
            issues += 1
        if corrupt:
            status = "error"
            issues += 1

        return {
            "status": status,
            "open_review_continuations": rows[:20],
            "interrupted_tasks": interrupted[:20],
            "corrupt": corrupt[:20],
        }, issues
    except Exception as e:
        return {"status": "error", "error": str(e)}, 1


def _scratchpad_readable() -> bool:
    """Scratchpad is available when the service can read its table.

    An empty scratchpad is a healthy state — only an unreadable one is a P1
    issue, so this replaces the old ``scratchpad.md`` existence probe.
    """
    try:
        get_scratchpad().list_blocks()
        return True
    except Exception:
        log.debug("Scratchpad service unreadable during startup check", exc_info=True)
        return False


def _identity_content() -> str:
    """Identity is available when the service returns content on any backend.

    Asking the service rather than probing the filesystem is what makes the
    check true under the ``s3`` backend, where there is no local file to stat.
    """
    try:
        return get_memory().load_identity()
    except Exception:
        log.debug("Identity service unreadable during startup check", exc_info=True)
        return ""


def verify_system_state(env: Any) -> None:
    """Verify persistent memory and configured model state on startup."""
    checks: dict[str, Any] = {}
    issues = 0
    checks["usage"], issue_count = check_usage(env)
    issues += issue_count

    identity_ok = bool(_identity_content().strip())
    scratchpad_ok = _scratchpad_readable()

    checks["identity"] = {"non_empty": identity_ok}
    checks["scratchpad"] = {"readable": scratchpad_ok}

    if not identity_ok:
        issues += 1
        log.warning("identity unreadable or empty — continuity at risk (Bible P1)")
    if not scratchpad_ok:
        issues += 1
        log.warning("scratchpad unreadable — working memory not available (Bible P1)")

    from ouroboros.storage.settings.store import load_settings

    configured_model = load_settings().OUROBOROS_MODEL or ""
    checks["model"] = {"configured": configured_model or "(not set)"}
    if not configured_model:
        issues += 1

    event = {
        "ts": utc_now_iso(),
        "type": "startup_verification",
        "checks": checks,
        "issues_count": issues,
    }
    record_event(env.drive_root, "events", "startup_verification", event)

    if issues > 0:
        log.warning(f"Startup verification found {issues} issue(s): {checks}")


def inject_crash_report(env: Any) -> None:
    """If a crash report exists from a rollback, log it to events.

    The file is NOT deleted — it stays so that build_health_invariants()
    shows CRITICAL: RECENT CRASH ROLLBACK on every task until the issue
    is investigated and removed explicitly via run_shell.
    """
    try:
        crash_path = env.drive_path("state") / "crash_report.json"
        if not crash_path.exists():
            return
        crash_data = read_json_dict(crash_path)
        if not crash_data:
            record_event(
                env.drive_root,
                "events",
                "crash_report_invalid",
                {
                    "ts": utc_now_iso(),
                    "path": str(crash_path),
                },
            )
            log.warning("Crash report exists but is not valid JSON object: %s", crash_path)
            return
        record_event(
            env.drive_root,
            "events",
            "crash_rollback_detected",
            {
                "ts": utc_now_iso(),
                "crash_data": crash_data,
            },
        )
        log.warning("Crash rollback detected: %s", crash_data)
    except Exception:
        log.debug("Failed to process crash report", exc_info=True)
