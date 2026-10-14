import time

import pytest

from infra.sql_connection import store_deadline
from ouroboros.storage.runtime_models import TaskRequest
from ouroboros.storage.runtime_persistence import runtime_persistence
from tests.helpers.shared import TEST_SCOPE


def test_receipts_and_completion_join_outer_rollback():
    tasks = runtime_persistence.current.tasks_repo
    delivery = runtime_persistence.current.result_repo
    tasks.create_task(TaskRequest(id="job", detached=True, scope=TEST_SCOPE.to_dict()), status="completed")
    with pytest.raises(ValueError, match="abort"):
        with delivery.transaction:
            claimed = delivery.claim_terminal_jobs()[0]
            assert delivery.record_terminal_delivery(
                "job", claim=claimed.terminal_publish_claim, item="text", disposition="accepted"
            )
            assert delivery.finish_terminal_publication("job", claim=claimed.terminal_publish_claim, accepted=True)
            assert tasks.get_task("job").terminal_published_at
            raise ValueError("abort")
    task = tasks.get_task("job")
    assert task.terminal_publish_claim is None
    assert task.terminal_published_at is None
    assert task.terminal_delivery == {}
    assert [task.task_id for task in delivery.claim_terminal_jobs()] == ["job"]


def test_receipts_are_monotonic_and_all_attachments_must_be_settled():
    tasks = runtime_persistence.current.tasks_repo
    delivery = runtime_persistence.current.result_repo
    task = tasks.create_task(TaskRequest(id="job", detached=True, scope=TEST_SCOPE.to_dict()))
    task.metadata["terminal_attachments"] = [{"id": "attachment", "kind": "file"}]
    task.status = "completed"
    tasks.upsert_task(task)
    claim = delivery.claim_terminal_jobs()[0].terminal_publish_claim
    assert delivery.record_terminal_delivery("job", claim=claim, item="text", disposition="accepted")
    assert delivery.record_terminal_delivery("job", claim=claim, item="text", disposition="retryable")
    assert not delivery.finish_terminal_publication("job", claim=claim, accepted=True)
    assert delivery.record_terminal_delivery("job", claim=claim, item="attachment", disposition="unavailable")
    assert delivery.record_terminal_delivery("job", claim=claim, item="attachment", disposition="retryable")
    assert tasks.get_task("job").terminal_delivery == {"text": "accepted", "attachment": "unavailable"}
    assert delivery.finish_terminal_publication("job", claim=claim, accepted=True)


def test_expired_deadline_does_not_claim_or_record_delivery():
    tasks = runtime_persistence.current.tasks_repo
    delivery = runtime_persistence.current.result_repo
    tasks.create_task(TaskRequest(id="job", detached=True, scope=TEST_SCOPE.to_dict()), status="completed")
    token = store_deadline.set(time.monotonic() - 1)
    try:
        with pytest.raises(TimeoutError):
            delivery.claim_terminal_jobs()
    finally:
        store_deadline.reset(token)
    assert tasks.get_task("job").terminal_publish_claim is None
    claim = delivery.claim_terminal_jobs()[0].terminal_publish_claim
    token = store_deadline.set(time.monotonic() - 1)
    try:
        with pytest.raises(TimeoutError):
            delivery.record_terminal_delivery("job", claim=claim, item="text", disposition="accepted")
    finally:
        store_deadline.reset(token)
    assert tasks.get_task("job").terminal_delivery == {}
