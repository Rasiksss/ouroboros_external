from abc import ABC, abstractmethod

from new_core.identity import OwnerScope
from supervisor.interfaces.cleanup_checkpoint import CleanupCheckpoint, ICleanupCheckpointRepository
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, scope_with


class CleanupCheckpointRepositoryConformance(ABC):
    @abstractmethod
    def make_repository(self) -> ICleanupCheckpointRepository:
        pass

    @abstractmethod
    def read_saved(self, repository: ICleanupCheckpointRepository, scope: OwnerScope) -> CleanupCheckpoint | None:
        pass

    def test_empty_checkpoint_is_read_without_recording_one(self) -> None:
        repository = self.make_repository()
        assert repository.get_last_run_at(TEST_SCOPE) == ""
        assert self.read_saved(repository, TEST_SCOPE) is None

    def test_checkpoint_time_and_removal_counts_roundtrip(self) -> None:
        repository = self.make_repository()
        checkpoint = CleanupCheckpoint("2026-07-01T00:00:00+00:00", {"tool_invocations": 7, "scratchpad_blocks": 2})

        repository.save_checkpoint(checkpoint, TEST_SCOPE)

        assert repository.get_last_run_at(TEST_SCOPE) == checkpoint.last_run_at
        assert self.read_saved(repository, TEST_SCOPE) == checkpoint

    @parametrize_from_dict(
        {
            "other owner": {"scope": scope_with(TEST_SCOPE, owner_id="other"), "shared": False},
            "other agent": {"scope": scope_with(TEST_SCOPE, agent_id="other"), "shared": False},
            "other chat": {"scope": scope_with(TEST_SCOPE, chat_id="other"), "shared": True},
            "other surface": {"scope": scope_with(TEST_SCOPE, surface_id="dialogs"), "shared": True},
        }
    )
    def test_checkpoints_are_addressed_by_owner_and_agent(self, scope: OwnerScope, shared: bool) -> None:
        repository = self.make_repository()
        mine = CleanupCheckpoint("2026-07-01", {"scratchpad_blocks": 1})
        theirs = CleanupCheckpoint("2026-07-02", {"scratchpad_blocks": 2})
        repository.save_checkpoint(mine, TEST_SCOPE)
        assert repository.get_last_run_at(scope) == (mine.last_run_at if shared else "")
        repository.save_checkpoint(theirs, scope)
        assert self.read_saved(repository, TEST_SCOPE) == (theirs if shared else mine)

    def test_save_replaces_checkpoint_instead_of_accumulating_counts(self) -> None:
        repository = self.make_repository()
        repository.save_checkpoint(CleanupCheckpoint("2026-07-01", {"tool_invocations": 9}), TEST_SCOPE)
        latest = CleanupCheckpoint("2026-07-02", {"scratchpad_blocks": 1})

        repository.save_checkpoint(latest, TEST_SCOPE)

        assert self.read_saved(repository, TEST_SCOPE) == latest

    def test_saved_checkpoint_is_independent_of_the_input(self) -> None:
        repository = self.make_repository()
        checkpoint = CleanupCheckpoint("2026-07-01", {"tool_invocations": 7})
        repository.save_checkpoint(checkpoint, TEST_SCOPE)
        checkpoint.last_run_at = "changed"
        checkpoint.removed["tool_invocations"] = 99

        assert self.read_saved(repository, TEST_SCOPE) == CleanupCheckpoint("2026-07-01", {"tool_invocations": 7})
