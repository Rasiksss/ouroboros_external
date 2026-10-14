from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, call

from sqlalchemy import text

from new_core.identity import OwnerScope
from ouroboros.agent_events.interfaces import LlmUsageReported
from ouroboros.storage.runtime_models import LlmUsageRecord
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.events import _handle_llm_usage
from supervisor.interfaces.owner_state import OwnerStateDocument
from supervisor.state import RuntimeStateRepository
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.sql_faults import fail_sql
from tests.helpers.supervisor_owner_state import sql_owner_state_repository
from tests.helpers.usage import sql_usage_repository

_SCOPE = OwnerScope(owner_id="owner", agent_id="agent", surface_id="browser", chat_id="1")
_OTHER = OwnerScope(owner_id="other", agent_id="agent", surface_id="browser", chat_id="1")


def test_state_reads_usage_from_the_injected_repository_and_keeps_it_when_scoped(tmp_path) -> None:
    states = sql_owner_state_repository()
    usage = Mock(wraps=sql_usage_repository())
    states.save(OwnerStateDocument({"session_id": "session"}), _SCOPE)
    states.save(OwnerStateDocument({"session_id": "session"}), _OTHER)
    usage.record_usage(
        LlmUsageRecord(owner_id="owner", agent_id="agent", prompt_tokens=17, metadata={"session_id": "session"})
    )
    usage.record_usage(
        LlmUsageRecord(owner_id="other", agent_id="agent", prompt_tokens=23, metadata={"session_id": "session"})
    )
    repo = RuntimeStateRepository(
        tmp_path,
        owner_state_repository=states,
        usage_repository=usage,
        scope=_SCOPE,
        audit_event_repository=sql_audit_event_repository(),
    )

    assert repo.load().spent_tokens_prompt == 17
    assert repo.for_scope(_OTHER).load().spent_tokens_prompt == 23
    assert usage.sum_usage_stats.call_args_list == [
        call(session_id="session", scope=_SCOPE),
        call(session_id="session", scope=_OTHER),
    ]


def test_usage_event_records_through_the_injected_repository_in_the_existing_order(tmp_path) -> None:
    usage = Mock(wraps=sql_usage_repository())
    before = []
    ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        usage_repository=usage,
        state_repo=SimpleNamespace(for_scope=lambda scope: SimpleNamespace(session_id=lambda: "session")),
        update_usage_from_llm=lambda *_args, **_kwargs: before.append(
            sql_usage_repository().sum_usage_stats()["spent_calls"]
        ),
    )

    _handle_llm_usage(LlmUsageReported(scope=_SCOPE, task_id="task", model="model", prompt_tokens=17), ctx)

    assert before == [0]
    assert sql_usage_repository().sum_usage_stats()["spent_calls"] == 1
    with runtime_persistence.current.engine.connect() as conn:
        record = LlmUsageRecord.model_validate_json(
            conn.execute(text("SELECT payload_json FROM llm_usage")).scalar_one()
        )
    assert (record.owner_id, record.agent_id, record.prompt_tokens, record.metadata["session_id"]) == (
        "owner",
        "agent",
        17,
        "session",
    )


def test_usage_read_failure_keeps_the_persisted_state_counters(tmp_path) -> None:
    states = sql_owner_state_repository()
    states.save(OwnerStateDocument({"session_id": "session", "spent_calls": 8}), _SCOPE)
    usage = Mock(wraps=sql_usage_repository())
    with fail_sql("llm_usage", OSError("unavailable")):
        repo = RuntimeStateRepository(
            tmp_path,
            owner_state_repository=states,
            usage_repository=usage,
            scope=_SCOPE,
            audit_event_repository=sql_audit_event_repository(),
        )

        assert repo.load().spent_calls == 8
