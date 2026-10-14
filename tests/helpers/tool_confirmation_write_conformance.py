from abc import ABC, abstractmethod

import pytest

from tests.helpers.runtime_store import make_tool_confirmation
from tests.helpers.shared import TEST_SCOPE


class ToolConfirmationWriteConformance(ABC):
    @abstractmethod
    def make_repository(self):
        pass

    def park(self, repository):
        return repository.park_and_open(
            task_id="task1",
            claim_token="",
            confirmation=make_tool_confirmation(),
            snapshot={"round_idx": 1, "messages": ["private"]},
            workspace="workspace",
            arguments={},
        )

    def test_answer_is_applied_only_once_and_snapshot_is_handed_over_once(self):
        repository = self.make_repository()
        request = self.park(repository)
        assert request is not None
        assert repository.resolve_tool_confirmation(request.confirmation_id, decision="approved", now_iso="now")
        assert not repository.resolve_tool_confirmation(request.confirmation_id, decision="denied", now_iso="later")
        assert not repository.expire_tool_confirmation(request.confirmation_id, now_iso="later")
        assert repository.list_parked_tasks_without_pending_request() == ["task1"]
        assert repository.requeue_parked_task("task1")
        assert not repository.requeue_parked_task("task1")
        assert repository.take_parked_snapshot("task1") == {"round_idx": 1, "messages": ["private"]}
        assert repository.take_parked_snapshot("task1") is None
        assert repository.spend_round_decisions("task1", round_idx=1, now_iso="now") == 1
        assert repository.spend_round_decisions("task1", round_idx=1, now_iso="later") == 0
        assert repository.read_tool_confirmation_decisions("task1", round_idx=1) == {}

    def test_outer_rollback_restores_question_and_snapshot(self):
        repository = self.make_repository()
        request = self.park(repository)
        with pytest.raises(ValueError, match="abort"):
            with repository.transaction:
                repository.expire_tool_confirmation(request.confirmation_id, now_iso="now")
                assert repository.take_parked_snapshot("task1") is not None
                raise ValueError("abort")
        assert repository.read_open_tool_confirmation(TEST_SCOPE) == request
        assert repository.take_parked_snapshot("task1") is not None

    def test_nested_rollback_preserves_the_outer_decision(self):
        repository = self.make_repository()
        request = self.park(repository)
        with repository.transaction:
            assert repository.resolve_tool_confirmation(request.confirmation_id, decision="denied", now_iso="now")
            with pytest.raises(ValueError, match="nested"):
                with repository.transaction:
                    assert repository.spend_round_decisions("task1", round_idx=1, now_iso="now") == 1
                    raise ValueError("nested")
            assert repository.read_tool_confirmation_decisions("task1", round_idx=1)
        assert repository.read_tool_confirmation_status(request.confirmation_id) == "denied"
        assert repository.read_tool_confirmation_decisions("task1", round_idx=1)

    def test_withdrawal_resumes_and_spends_the_round(self):
        repository = self.make_repository()
        request = self.park(repository)
        assert repository.expire_tool_confirmation(request.confirmation_id, now_iso="now")
        assert repository.unpark_task(request.confirmation_id, now_iso="now")
        assert repository.read_tool_confirmation_decisions("task1", round_idx=1) == {}
        assert repository.take_parked_snapshot("task1") is None
        assert repository.list_parked_tasks_without_pending_request() == []

    def test_missing_writes_do_not_invent_tasks_or_questions(self):
        repository = self.make_repository()
        assert not repository.unpark_task("missing", now_iso="now")
        assert not repository.requeue_parked_task("missing")
        assert not repository.expire_tool_confirmation("missing", now_iso="now")
        assert not repository.resolve_tool_confirmation("missing", decision="approved", now_iso="now")
        assert repository.spend_round_decisions("missing", round_idx=1, now_iso="now") == 0
        assert repository.take_parked_snapshot("missing") is None
        assert repository.forget_snapshots_of_finished_runs() == 0
        repository.close_confirmations_nobody_is_parked_on(now_iso="now")
