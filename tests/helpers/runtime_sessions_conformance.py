from abc import ABC, abstractmethod

from ouroboros.storage.runtime_models import RuntimeSession
from supervisor.interfaces.runtime_sessions import IRuntimeSessionRepository

CREATED_AT = "2026-01-01T00:00:00+00:00"
UPDATED_AT = "2026-01-02T00:00:00+00:00"


class RuntimeSessionRepositoryConformance(ABC):
    @abstractmethod
    def make_repository(self) -> IRuntimeSessionRepository:
        pass

    @abstractmethod
    def read_session(self, repository: IRuntimeSessionRepository, session_id: str) -> RuntimeSession:
        pass

    def test_insert_fills_missing_timestamps_and_returns_the_input(self) -> None:
        repository = self.make_repository()
        session = RuntimeSession(session_id="new", source="dialogs", owner_external_id="owner")

        assert repository.create_or_update_session(session) is session

        assert session.created_at
        assert session.last_seen_at == session.created_at
        assert self.read_session(repository, "new") == session

    def test_explicit_timestamps_are_preserved(self) -> None:
        repository = self.make_repository()
        session = RuntimeSession(session_id="explicit", created_at=CREATED_AT, last_seen_at=UPDATED_AT)

        repository.create_or_update_session(session)

        assert self.read_session(repository, "explicit") == session
        assert (session.created_at, session.last_seen_at) == (CREATED_AT, UPDATED_AT)

    def test_update_replaces_metadata_but_keeps_original_creation_time(self) -> None:
        repository = self.make_repository()
        original = RuntimeSession(session_id="same", created_at=CREATED_AT)
        repository.create_or_update_session(original)
        update = RuntimeSession(
            session_id="same",
            source="browser",
            owner_external_id="new owner",
            auth_context_hash="new hash",
            status="revoked",
            created_at=UPDATED_AT,
            last_seen_at=UPDATED_AT,
            expires_at=UPDATED_AT,
        )

        assert repository.create_or_update_session(update) is update

        assert update.created_at == UPDATED_AT
        assert self.read_session(repository, "same") == update.model_copy(update={"created_at": CREATED_AT})

    def test_sessions_are_independent_snapshots(self) -> None:
        repository = self.make_repository()
        first = RuntimeSession(session_id="first", owner_external_id="first owner")
        repository.create_or_update_session(first)
        second = RuntimeSession(session_id="second", owner_external_id="second owner")
        repository.create_or_update_session(second)

        first.owner_external_id = "not saved"

        assert self.read_session(repository, "first").owner_external_id == "first owner"
        assert self.read_session(repository, "second") == second
