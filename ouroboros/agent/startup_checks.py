"""Worker-boot checks for usage and memory files."""

from __future__ import annotations

import logging
from typing import Any, Literal, TypedDict

from new_core.identity import OwnerScope, ServiceJob
from ouroboros.agent.interfaces.startup_usage import IStartupUsageRepository
from ouroboros.contracts.owner_scope import service_identity
from ouroboros.observability.event_record import record_event
from ouroboros.observability.interfaces.audit_events import IAuditEventRepository
from ouroboros.storage.core_memory.scratchpad import get_scratchpad
from ouroboros.utils import read_json_dict, utc_now_iso

log = logging.getLogger(__name__)


class UsageCheck(TypedDict, total=False):
    status: Literal["ok", "error"]
    spent_calls: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    error: str


def check_usage(repository: IStartupUsageRepository) -> tuple[UsageCheck, int]:
    """Check persisted token/call counters availability."""
    try:
        # A boot diagnostic has no author, so the widening is spelled out: it reports the
        # counters of the instance's configured owner, not of whoever is talking.
        usage = repository.get_usage(service_identity(job=ServiceJob.STARTUP).as_scope())
        return {
            "status": "ok",
            "spent_calls": usage.spent_calls,
            "prompt_tokens": usage.spent_tokens_prompt,
            "completion_tokens": usage.spent_tokens_completion,
            "cached_tokens": usage.spent_tokens_cached,
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

        continuations, corrupt = list_review_continuations(
            service_identity(job=ServiceJob.STARTUP).as_scope(), env.drive_root
        )
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


def _scratchpad_readable(scope: OwnerScope) -> bool:
    """Scratchpad is available when the service can read its table.

    An empty scratchpad is a healthy state — only an unreadable one is a P1
    issue, so this replaces the old ``scratchpad.md`` existence probe.
    """
    try:
        get_scratchpad().list_blocks(scope)
        return True
    except Exception:
        log.debug("Scratchpad service unreadable during startup check", exc_info=True)
        return False


def verify_system_state(
    env: Any, *, startup_usage_repository: IStartupUsageRepository, audit_event_repository: IAuditEventRepository
) -> None:
    """Verify persistent memory and configured model state on startup."""
    checks: dict[str, Any] = {}
    issues = 0
    checks["usage"], issue_count = check_usage(startup_usage_repository)
    issues += issue_count

    # Boot has no task and no chat, so it names itself as service work — under the same
    # owner and agent the running agent writes as, or the check would probe an empty
    # namespace and report a healthy instance as broken.
    scope = service_identity(job=ServiceJob.STARTUP).as_scope()
    scratchpad_ok = _scratchpad_readable(scope)

    checks["scratchpad"] = {"readable": scratchpad_ok}

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
    record_event("events", "startup_verification", event, audit_event_repository=audit_event_repository)

    if issues > 0:
        log.warning(f"Startup verification found {issues} issue(s): {checks}")


def inject_crash_report(env: Any, *, audit_event_repository: IAuditEventRepository) -> None:
    """If a crash report exists from a rollback, log it to events.

    The file is NOT deleted — it stays so that build_health_invariants()
    shows CRITICAL: RECENT CRASH ROLLBACK on every task until the issue
    is investigated and removed explicitly via run_shell.
    """
    try:
        crash_path = env.drive_path("state") / "crash_report.json"
        if not crash_path.exists():
            return
        crash_data = read_json_dict(service_identity(job=ServiceJob.STARTUP).as_scope(), crash_path)
        if not crash_data:
            record_event(
                "events",
                "crash_report_invalid",
                {
                    "ts": utc_now_iso(),
                    "path": str(crash_path),
                },
                audit_event_repository=audit_event_repository,
            )
            log.warning("Crash report exists but is not valid JSON object: %s", crash_path)
            return
        record_event(
            "events",
            "crash_rollback_detected",
            {
                "ts": utc_now_iso(),
                "crash_data": crash_data,
            },
            audit_event_repository=audit_event_repository,
        )
        log.warning("Crash rollback detected: %s", crash_data)
    except Exception:
        log.debug("Failed to process crash report", exc_info=True)
