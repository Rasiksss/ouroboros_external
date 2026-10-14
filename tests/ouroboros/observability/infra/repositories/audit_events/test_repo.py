import time

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from infra.sql_connection import store_deadline
from infra.sql_transaction import SqlTransactionManager
from ouroboros.observability.infra.repositories.audit_events.repository import SqlAuditEventRepository
from ouroboros.storage.runtime_models import AuditEvent
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.audit_event_conformance import AuditEventRepositoryConformance
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.parametrize import parametrize_from_dict


class TestSqlAuditEventRepository(AuditEventRepositoryConformance):
    def make_reader(self, events: list[AuditEvent]) -> SqlAuditEventRepository:
        for event in events:
            sql_audit_event_repository().append_audit_event(event)
        return sql_audit_event_repository()


def test_shared_manager_makes_reads_see_pending_writes_and_rolls_back_both():
    transaction_manager = SqlTransactionManager(runtime_persistence.current.engine)
    writer = SqlAuditEventRepository(transaction_manager)
    reader = SqlAuditEventRepository(transaction_manager)
    observer = sql_audit_event_repository()
    with pytest.raises(ValueError), writer.transaction:
        writer.append_audit_event(AuditEvent(event_type="pending"))
        assert len(reader.list_audit_events()) == 1
        assert observer.list_audit_events() == []
        reader.append_audit_event(AuditEvent(event_type="also pending"))
        raise ValueError("abort")
    assert observer.list_audit_events() == []


def test_failed_append_does_not_poison_outer_transaction():
    repository = sql_audit_event_repository()
    with repository.transaction:
        repository.append_audit_event(AuditEvent(event_id="same", event_type="first"))
        with pytest.raises(IntegrityError):
            repository.append_audit_event(AuditEvent(event_id="same", event_type="duplicate"))
        repository.append_audit_event(AuditEvent(event_type="last"))
    assert [event.event_type for event in repository.list_audit_events()] == ["first", "last"]


def test_read_respects_deadline():
    token = store_deadline.set(time.monotonic() - 1)
    try:
        with pytest.raises(TimeoutError, match="deadline expired"):
            sql_audit_event_repository().list_audit_events()
    finally:
        store_deadline.reset(token)


def test_nonempty_legacy_payload_keeps_metadata_but_columns_override_axes():
    store = runtime_persistence.current
    sql_audit_event_repository().append_audit_event(
        AuditEvent(
            event_id="a",
            event_type="event",
            owner_id="actual",
            agent_id="actual",
            log_stream="tools",
            metadata={"column": True},
        )
    )
    with store.engine.begin() as conn:
        conn.execute(
            text("UPDATE audit_events SET payload_json=:body"),
            {
                "body": '{"event_id":"a","event_type":"event","owner_id":"old","agent_id":"old","log_stream":"events","metadata":{"payload":true}}'
            },
        )
    event = sql_audit_event_repository().list_audit_events()[0]
    assert (event.owner_id, event.agent_id, event.log_stream, event.metadata) == (
        "actual",
        "actual",
        "tools",
        {"payload": True},
    )


@parametrize_from_dict({"empty": {"body": "{}"}, "broken": {"body": "broken"}, "array": {"body": "[]"}})
def test_invalid_payload_falls_back_to_columns_without_writes(body):
    store = runtime_persistence.current
    original = AuditEvent(
        event_id="a",
        event_type="event",
        owner_id="owner",
        agent_id="agent",
        log_stream="tools",
        metadata={"key": "value"},
    )
    sql_audit_event_repository().append_audit_event(original)
    with store.engine.begin() as conn:
        conn.execute(text("UPDATE audit_events SET payload_json=:body"), {"body": body})
    assert sql_audit_event_repository().list_audit_events() == [original]
    with store.engine.connect() as conn:
        assert conn.execute(text("SELECT payload_json FROM audit_events")).scalar_one() == body
