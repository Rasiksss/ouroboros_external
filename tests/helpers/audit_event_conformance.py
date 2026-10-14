from abc import ABC, abstractmethod

import pytest

from ouroboros.observability.interfaces.audit_events import IAuditEventRepository
from ouroboros.storage.runtime_models import AuditEvent
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE


class AuditEventRepositoryConformance(ABC):
    @abstractmethod
    def make_reader(self, events: list[AuditEvent]) -> IAuditEventRepository:
        pass

    def test_append_assigns_identity_and_preserves_payload(self):
        repository = self.make_reader([])
        original = AuditEvent(event_id="", timestamp="", event_type="written", metadata={"value": 1})
        assert repository.append_audit_event(original) is original
        assert original.event_id and original.timestamp
        assert repository.list_audit_events() == [original]
        original.metadata["value"] = 2
        assert repository.list_audit_events()[0].metadata == {"value": 1}

    def test_transaction_rollback_removes_all_appended_events(self):
        repository = self.make_reader([])
        with pytest.raises(ValueError), repository.transaction:
            repository.append_audit_event(AuditEvent(event_type="first"))
            repository.append_audit_event(AuditEvent(event_type="second"))
            assert len(repository.list_audit_events()) == 2
            raise ValueError("abort")
        assert repository.list_audit_events() == []

    def test_nested_rollback_preserves_outer_writes(self):
        repository = self.make_reader([])
        with repository.transaction:
            repository.append_audit_event(AuditEvent(event_type="before"))
            with pytest.raises(ValueError), repository.transaction:
                repository.append_audit_event(AuditEvent(event_type="aborted"))
                raise ValueError("abort nested")
            repository.append_audit_event(AuditEvent(event_type="after"))
        assert [event.event_type for event in repository.list_audit_events()] == ["before", "after"]

    def test_nested_success_remains_part_of_outer_rollback(self):
        repository = self.make_reader([])
        with pytest.raises(ValueError), repository.transaction:
            with repository.transaction:
                repository.append_audit_event(AuditEvent(event_type="nested"))
            raise ValueError("abort outer")
        assert repository.list_audit_events() == []

    @parametrize_from_dict(
        {
            "global": {"filters": {}, "expected": ["a", "b", "c", "d"]},
            "owner": {"filters": {"scope": TEST_SCOPE}, "expected": ["a", "b"]},
            "type": {"filters": {"event_type": "first"}, "expected": ["a", "c"]},
            "resource type": {"filters": {"resource_type": "tool"}, "expected": ["b"]},
            "resource id": {"filters": {"resource_id": "one"}, "expected": ["a"]},
            "stream": {"filters": {"log_stream": "tools"}, "expected": ["b"]},
            "intersection": {"filters": {"scope": TEST_SCOPE, "event_type": "first"}, "expected": ["a"]},
            "latest then chronological": {"filters": {"limit": 2}, "expected": ["c", "d"]},
            "zero limit": {"filters": {"limit": 0}, "expected": ["d"]},
            "negative limit": {"filters": {"limit": -1}, "expected": ["d"]},
        }
    )
    def test_filters_and_order(self, filters, expected):
        reader = self.make_reader(
            [
                AuditEvent(
                    event_id="a",
                    timestamp="1",
                    owner_id=TEST_SCOPE.owner_id,
                    agent_id=TEST_SCOPE.agent_id,
                    event_type="first",
                    resource_id="one",
                    resource_type="task",
                    log_stream="events",
                ),
                AuditEvent(
                    event_id="b",
                    timestamp="2",
                    owner_id=TEST_SCOPE.owner_id,
                    agent_id=TEST_SCOPE.agent_id,
                    event_type="second",
                    resource_type="tool",
                    log_stream="tools",
                ),
                AuditEvent(
                    event_id="c",
                    timestamp="2",
                    owner_id="other",
                    agent_id=TEST_SCOPE.agent_id,
                    event_type="first",
                    log_stream="events",
                ),
                AuditEvent(
                    event_id="d",
                    timestamp="3",
                    owner_id=TEST_SCOPE.owner_id,
                    agent_id="other",
                    event_type="second",
                    log_stream="events",
                ),
            ]
        )
        assert [item.event_id for item in reader.list_audit_events(**filters)] == expected
