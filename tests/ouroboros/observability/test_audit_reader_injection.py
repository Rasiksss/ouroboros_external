from unittest.mock import Mock

from ouroboros.observability.event_record import list_runtime_events
from ouroboros.storage.runtime_models import AuditEvent
from tests.helpers.audit_events import sql_audit_event_repository


def test_runtime_events_read_from_the_injected_reader(tmp_path):
    repository = sql_audit_event_repository()
    repository.append_audit_event(AuditEvent(event_type="test", metadata={"value": "injected"}))
    reader = Mock(wraps=repository)

    assert list_runtime_events(audit_event_repository=reader) == [{"value": "injected"}]
    reader.list_audit_events.assert_called_once()
