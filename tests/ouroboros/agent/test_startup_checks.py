from supervisor.interfaces.owner_state import OwnerStateDocument
from tests.helpers.sql_faults import fail_sql
from tests.helpers.supervisor_owner_state import sql_owner_state_repository

"""Startup health does not require or create durable self-description memory."""

from ouroboros.agent import Env
from ouroboros.agent.interfaces.startup_usage import StartupUsage
from ouroboros.agent.startup_checks import check_usage, verify_system_state
from ouroboros.contracts.owner_scope import ServiceJob, service_identity
from ouroboros.observability.event_record import list_runtime_events
from ouroboros.storage.core_memory.memory import get_memory
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.startup_usage import sql_startup_usage_repository


def test_usage_diagnostic_reads_only_its_injected_counters() -> None:
    scope = service_identity(job=ServiceJob.STARTUP).as_scope()
    repository = sql_startup_usage_repository()
    sql_owner_state_repository().save(
        OwnerStateDocument(
            StartupUsage(
                spent_calls=7, spent_tokens_prompt=11, spent_tokens_completion=13, spent_tokens_cached=17
            ).model_dump()
        ),
        scope,
    )

    result, issues = check_usage(repository)

    assert result == {
        "status": "ok",
        "spent_calls": 7,
        "prompt_tokens": 11,
        "completion_tokens": 13,
        "cached_tokens": 17,
    }
    assert issues == 0
    assert repository.get_usage(scope).spent_calls == 7


def test_usage_diagnostic_reports_repository_failure() -> None:
    repository = sql_startup_usage_repository()
    with fail_sql("owner_runtime_state", OSError("usage unavailable")):
        assert check_usage(repository) == ({"status": "error", "error": "usage unavailable"}, 1)


def test_startup_is_healthy_with_no_durable_memories(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_MODEL", "test-model")
    env = Env(repo_dir=tmp_path / "repo", drive_root=tmp_path)
    assert get_memory().list_headers(TEST_SCOPE) == []

    audit = sql_audit_event_repository()
    verify_system_state(
        env,
        startup_usage_repository=sql_startup_usage_repository(),
        audit_event_repository=audit,
    )

    event = list_runtime_events(event_type="startup_verification", limit=1, audit_event_repository=audit)[0]
    assert set(event["checks"]) == {"usage", "scratchpad", "model"}
    assert event["issues_count"] == 0
    assert get_memory().list_headers(TEST_SCOPE) == []
