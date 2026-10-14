from abc import ABC, abstractmethod

from new_core.identity import OwnerScope
from supervisor.interfaces.owner_state import IOwnerStateRepository, OwnerStateDocument
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, scope_with


class OwnerStateRepositoryConformance(ABC):
    @abstractmethod
    def make_repository(self) -> IOwnerStateRepository:
        pass

    @abstractmethod
    def seed(self, repository: IOwnerStateRepository, scope: OwnerScope, document: OwnerStateDocument) -> None:
        pass

    def test_missing_row_stays_missing_on_read(self) -> None:
        repository = self.make_repository()
        assert repository.load(TEST_SCOPE) is None
        assert repository.load(TEST_SCOPE) is None

    def test_repository_does_not_add_defaults_or_drop_unknown_fields(self) -> None:
        repository = self.make_repository()
        document = OwnerStateDocument({"custom": "keep", "spent_calls": 7})

        repository.save(document, TEST_SCOPE)

        assert repository.load(TEST_SCOPE) == document
        assert "session_id" not in document.payload

    @parametrize_from_dict(
        {
            "other owner": {"scope": scope_with(TEST_SCOPE, owner_id="other"), "shared": False},
            "other agent": {"scope": scope_with(TEST_SCOPE, agent_id="other"), "shared": False},
            "other chat": {"scope": scope_with(TEST_SCOPE, chat_id="other"), "shared": True},
            "other surface": {"scope": scope_with(TEST_SCOPE, surface_id="dialogs"), "shared": True},
        }
    )
    def test_state_is_addressed_by_owner_and_agent(self, scope: OwnerScope, shared: bool) -> None:
        repository = self.make_repository()
        mine = OwnerStateDocument({"session_id": "mine"})
        theirs = OwnerStateDocument({"session_id": "theirs"})
        repository.save(mine, TEST_SCOPE)
        assert repository.load(scope) == (mine if shared else None)
        repository.save(theirs, scope)
        assert repository.load(TEST_SCOPE) == (theirs if shared else mine)

    @parametrize_from_dict(
        {
            "saved cursor wins": {"previous": {"dialogs": {"seq": 17}}, "expected": {"dialogs": {"seq": 17}}},
            "empty providers win": {"previous": {}, "expected": {}},
            "null providers": {"previous": None, "expected": {"dialogs": {"seq": 7}}},
            "malformed providers": {"previous": [], "expected": {"dialogs": {"seq": 7}}},
        }
    )
    def test_save_replaces_supervisor_fields_and_preserves_stored_transport_fields(self, previous, expected) -> None:
        repository = self.make_repository()
        self.seed(
            repository,
            TEST_SCOPE,
            OwnerStateDocument({"session_id": "old", "custom": "drop", "provider_state": previous}),
        )
        incoming = OwnerStateDocument({"session_id": "new", "provider_state": {"dialogs": {"seq": 7}}})

        repository.save(incoming, TEST_SCOPE)

        assert repository.load(TEST_SCOPE) == OwnerStateDocument({"session_id": "new", "provider_state": expected})
        assert incoming.payload["provider_state"] == {"dialogs": {"seq": 7}}

    def test_saved_and_loaded_documents_are_independent_snapshots(self) -> None:
        repository = self.make_repository()
        document = OwnerStateDocument({"provider_state": {"dialogs": {"seq": 17}}})
        repository.save(document, TEST_SCOPE)
        document.payload["provider_state"]["dialogs"]["seq"] = 99
        loaded = repository.load(TEST_SCOPE)
        assert loaded == OwnerStateDocument({"provider_state": {"dialogs": {"seq": 17}}})
        loaded.payload["provider_state"]["dialogs"]["seq"] = 100
        assert repository.load(TEST_SCOPE) == OwnerStateDocument({"provider_state": {"dialogs": {"seq": 17}}})

    @parametrize_from_dict(
        {
            "saved checkpoint": {"checkpoint": {"last_run_at": "2026-07-01", "removed": {"scratchpad_blocks": 7}}},
            "empty checkpoint": {"checkpoint": {}},
            "null checkpoint": {"checkpoint": None},
        }
    )
    def test_owner_save_preserves_the_stored_housekeeping_checkpoint(self, checkpoint) -> None:
        repository = self.make_repository()
        self.seed(
            repository, TEST_SCOPE, OwnerStateDocument({"session_id": "old", "working_retrieval_cleanup": checkpoint})
        )

        repository.save(
            OwnerStateDocument({"session_id": "new", "working_retrieval_cleanup": {"stale": True}}), TEST_SCOPE
        )

        assert repository.load(TEST_SCOPE) == OwnerStateDocument(
            {"session_id": "new", "working_retrieval_cleanup": checkpoint}
        )
