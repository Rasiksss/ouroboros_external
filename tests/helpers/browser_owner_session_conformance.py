from abc import ABC, abstractmethod

from new_core.identity import OwnerScope
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, scope_with
from transport.browser.interfaces.owner_session import IOwnerSessionRepository


class OwnerSessionRepositoryConformance(ABC):
    @abstractmethod
    def make_repository(self) -> IOwnerSessionRepository:
        pass

    @abstractmethod
    def put_session(self, repository: IOwnerSessionRepository, scope: OwnerScope, session_id: str) -> None:
        pass

    def test_missing_owner_has_no_session(self) -> None:
        repository = self.make_repository()
        assert repository.get_session_id(TEST_SCOPE) == ""
        assert repository.get_session_id(TEST_SCOPE) == ""

    @parametrize_from_dict(
        {
            "other owner": {"scope": scope_with(TEST_SCOPE, owner_id="other"), "expected": "other session"},
            "other agent": {"scope": scope_with(TEST_SCOPE, agent_id="other"), "expected": "agent session"},
            "other chat": {"scope": scope_with(TEST_SCOPE, chat_id="another"), "expected": "session"},
            "other surface": {"scope": scope_with(TEST_SCOPE, surface_id="dialogs"), "expected": "session"},
        }
    )
    def test_session_is_selected_by_owner_and_agent(self, scope: OwnerScope, expected: str) -> None:
        repository = self.make_repository()
        self.put_session(repository, TEST_SCOPE, "session")
        self.put_session(repository, scope_with(TEST_SCOPE, owner_id="other"), "other session")
        self.put_session(repository, scope_with(TEST_SCOPE, agent_id="other"), "agent session")

        assert repository.get_session_id(scope) == expected

    def test_reader_observes_a_session_change(self) -> None:
        repository = self.make_repository()
        self.put_session(repository, TEST_SCOPE, "before")
        assert repository.get_session_id(TEST_SCOPE) == "before"
        self.put_session(repository, TEST_SCOPE, "after")
        assert repository.get_session_id(TEST_SCOPE) == "after"

    def test_reader_preserves_the_session_string(self) -> None:
        repository = self.make_repository()
        self.put_session(repository, TEST_SCOPE, "  сессия  ")
        assert repository.get_session_id(TEST_SCOPE) == "  сессия  "
