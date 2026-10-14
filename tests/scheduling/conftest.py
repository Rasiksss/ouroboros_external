"""Shared fixtures: a migrated runtime schema on a per-test PostgreSQL engine."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from config_registry.chat_registry.domain import ChatEntity, TitleSource
from config_registry.chat_registry.interfaces import ChatRegistryStore
from infra.migrations.runner import upgrade_head
from new_core.identity import OwnerScope
from ouroboros.scheduling.store import SqlScheduleStore


@pytest.fixture
def engine(blank_schema):
    engine = blank_schema()
    upgrade_head(engine)
    return engine


@pytest.fixture
def store(engine) -> SqlScheduleStore:
    return SqlScheduleStore(engine)


class RecordedChats(ChatRegistryStore):
    def __init__(self) -> None:
        self.records: dict[tuple[str, str, str], ChatEntity] = {}
        self.read: list[OwnerScope] = []

    @staticmethod
    def _key(scope: OwnerScope) -> tuple[str, str, str]:
        return (scope.owner_id, scope.agent_id, scope.chat_id)

    def record(self, scope: OwnerScope, *, is_active: bool) -> None:
        self.records[self._key(scope)] = ChatEntity(
            owner_id=scope.owner_id,
            agent_id=scope.agent_id,
            chat_id=scope.chat_id,
            title="разговор",
            title_source=TitleSource.PROVISIONAL,
            title_attempts=0,
            is_active=is_active,
            created_at="2026-01-01T00:00:00+00:00",
        )

    def get_chat(self, scope: OwnerScope) -> ChatEntity | None:
        self.read.append(scope)
        return self.records.get(self._key(scope))

    def ensure_chat(self, scope: OwnerScope, *, provisional_title: str) -> ChatEntity:
        raise NotImplementedError

    def claim_title_attempt(self, scope: OwnerScope, *, max_attempts: int) -> bool:
        raise NotImplementedError

    def store_generated_title(self, scope: OwnerScope, *, title: str) -> bool:
        raise NotImplementedError

    def delete_owner_chats(self, *, owner_id: str, agent_id: str) -> int:
        raise NotImplementedError


@pytest.fixture
def chats() -> RecordedChats:
    return RecordedChats()


@pytest.fixture
def make_scope():
    def make(owner: str = "owner-a", chat: str = "100", surface: str = "dialogs") -> OwnerScope:
        return OwnerScope(owner_id=owner, agent_id="main", surface_id=surface, chat_id=chat)

    return make


#: Far enough ahead that a schedule made "in an hour" is still in the future.
_NOW = 1_900_000_000.0


@pytest.fixture
def clock() -> list[float]:
    """One mutable instant the whole test moves; `frozen_clock` is what the package reads."""
    return [_NOW]


@pytest.fixture(autouse=True)
def frozen_clock(clock, monkeypatch):
    """The package reads its module's clock, the way `background_memory` and `llm` do."""
    for module in ("ouroboros.scheduling.dispatch", "ouroboros.scheduling.service"):
        monkeypatch.setattr(f"{module}.time_module", SimpleNamespace(time=lambda: clock[0]))
