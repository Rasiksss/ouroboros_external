"""Test doubles for the user settings stores."""

from __future__ import annotations

from config_registry.user_settings import reader
from config_registry.user_settings.infra.repositories.repo import (
    SqlAsyncUserSettingsStore,
    SqlSyncUserSettingsRepo,
    SqlUserSettingsStore,
)
from config_registry.user_settings.interfaces import (
    IAsyncUserSettingsStore,
    ISyncUserSettingsRepo,
    UserSettingsStore,
)
from infra.cache.interface import IAsyncCache, ICache
from infra.cache.null import AsyncNullCache, NullCache
from new_core.identity import OwnerScope
from ouroboros.storage.settings.store import load_settings


class StubMcpServerIdsReader:
    def __init__(
        self,
        server_ids: list[str] | None = None,
        *,
        by_pair: dict[tuple[str, str], list[str]] | None = None,
    ) -> None:
        self.server_ids = list(server_ids or ())
        self.by_pair = by_pair or {}

    def __call__(self, scope: OwnerScope) -> list[str]:
        return list(self.by_pair.get((scope.owner_id, scope.agent_id), self.server_ids))


class StubUserSettingsStore(UserSettingsStore):
    """In-memory store that counts reads.

    ``overrides`` is the flat case: served to every owner and agent. ``by_pair`` replaces it
    when a test needs owners or agents to differ; a pair it does not name holds nothing.
    """

    def __init__(
        self,
        overrides: dict[str, str] | None = None,
        *,
        by_pair: dict[tuple[str, str], dict[str, str]] | None = None,
    ) -> None:
        self.reads = 0
        self._overrides = overrides or {}
        self._by_pair = by_pair or {}

    def get_setting_value(self, owner_id: str, agent_id: str, key: str) -> str | None:
        self.reads += 1
        return self._pair(owner_id, agent_id).get(key)

    def put(self, owner_id: str, agent_id: str, key: str, value: str) -> None:
        """The rows a writer left behind, readable through the same stub."""
        rows = self._by_pair.setdefault((owner_id, agent_id), {}) if self._by_pair else self._overrides
        rows[key] = value

    def _pair(self, owner_id: str, agent_id: str) -> dict[str, str]:
        return self._by_pair.get((owner_id, agent_id), {}) if self._by_pair else self._overrides


class NoUserSettingsWrites(ISyncUserSettingsRepo):
    """The write face for a reader under test: reaching it is the failure."""

    def set_override(self, owner_id: str, agent_id: str, key: str, value: str) -> None:
        raise AssertionError(f"the reader under test wrote {key} for {owner_id}/{agent_id}")


class StubSyncUserSettingsRepo(ISyncUserSettingsRepo):
    """The chat command's write face, landing on a synchronous stub's rows."""

    def __init__(self, mirror: StubUserSettingsStore) -> None:
        self.writes: list[tuple[str, str, str, str]] = []
        self._mirror = mirror

    def set_override(self, owner_id: str, agent_id: str, key: str, value: str) -> None:
        self.writes.append((owner_id, agent_id, key, value))
        self._mirror.put(owner_id, agent_id, key, value)


class StubAsyncUserSettingsStore(IAsyncUserSettingsStore):
    """The async face with its own read counter and rows of its own.

    ``overrides`` is the flat case and ``by_pair`` the keyed one, exactly as on the
    synchronous stub; ``mirror`` hands the whole map over to a synchronous stub's rows,
    so the two faces of one test can never drift apart.
    """

    def __init__(
        self,
        overrides: dict[str, str] | None = None,
        *,
        by_pair: dict[tuple[str, str], dict[str, str]] | None = None,
        mirror: StubUserSettingsStore | None = None,
    ) -> None:
        self.reads = 0
        self._mirror = mirror
        self._overrides = overrides or {}
        self._by_pair = by_pair or {}

    async def get_setting_values(self, owner_id: str, agent_id: str) -> dict[str, str]:
        self.reads += 1
        return self._pair(owner_id, agent_id)

    async def get_setting_value(self, owner_id: str, agent_id: str, key: str) -> str | None:
        self.reads += 1
        return self._pair(owner_id, agent_id).get(key)

    def _pair(self, owner_id: str, agent_id: str) -> dict[str, str]:
        if self._mirror is not None:
            return dict(self._mirror._pair(owner_id, agent_id))
        return dict(self._by_pair.get((owner_id, agent_id), {}) if self._by_pair else self._overrides)


def _async_face(store: UserSettingsStore) -> IAsyncUserSettingsStore:
    if isinstance(store, StubUserSettingsStore):
        return StubAsyncUserSettingsStore(mirror=store)
    return SqlAsyncUserSettingsStore()


def _writer_face(store: UserSettingsStore) -> ISyncUserSettingsRepo:
    if isinstance(store, StubUserSettingsStore):
        return StubSyncUserSettingsRepo(mirror=store)
    return SqlSyncUserSettingsRepo()


def reset_user_settings_for_tests(
    store: UserSettingsStore | None = None,
    cache: ICache | None = None,
    async_store: IAsyncUserSettingsStore | None = None,
    writer: ISyncUserSettingsRepo | None = None,
    async_cache: IAsyncCache | None = None,
) -> None:
    """Put a settings object over these faces into the holder, or back the real one.

    ``load_settings`` by attribute on this module: tests monkeypatch that name to hand the
    object an instance of their own, the way a root hands it the one it read. The cache is
    replaced along with the store, so no entry a previous test filled is reachable from the
    next one — a root would have composed both at start-up. The async face and the write face
    default to stubs over the given store's rows, so neither an aggregate read nor a chat
    command's write touches a real engine unless a test composed one on purpose.
    """
    store = store if store is not None else SqlUserSettingsStore()
    reader.user_settings_factory.reset_for_tests(
        reader.UserSettings(
            load_settings(),
            store,
            async_store if async_store is not None else _async_face(store),
            writer if writer is not None else _writer_face(store),
            cache if cache is not None else NullCache(),
            async_cache if async_cache is not None else AsyncNullCache(),
        )
    )


def stub_user_settings(
    *,
    main_model: str = "test-model",
    light_model: str = "test-model",
    fallback_model: str = "",
    distillation_enabled: bool = True,
) -> reader.UserSettings:
    """The four keys a background handler reads, pinned so no test reaches a configured provider."""
    store = StubUserSettingsStore()
    return reader.UserSettings(
        load_settings().model_copy(
            update={
                "OUROBOROS_MODEL": main_model,
                "OUROBOROS_MODEL_LIGHT": light_model,
                "OUROBOROS_MODEL_FALLBACK": fallback_model,
                "OUROBOROS_MEMORY_DISTILL": distillation_enabled,
            }
        ),
        store,
        StubAsyncUserSettingsStore(mirror=store),
        NoUserSettingsWrites(),
        NullCache(),
        AsyncNullCache(),
    )
