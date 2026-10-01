"""Settings an owner may change for themselves, resolved over the instance values.

Overrides live in the sparse ``user_settings`` table: a row exists only for a key
the owner changed, so its absence means the instance value applies. Every other
``AppSettings`` field stays instance-only — those decide where data goes, which
credentials sign it, and how the deployment behaves as a whole.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from sqlalchemy import text

from ouroboros.storage.runtime_engine import resolve_runtime_engine
from ouroboros.storage.settings.schema import AppSettings
from ouroboros.storage.settings.store import load_settings

USER_SETTING_KEYS = (
    "OUROBOROS_EFFORT_TASK",
    "OUROBOROS_LOCALE",
    "OUROBOROS_MEMORY_DISTILL",
    "OUROBOROS_MODEL",
    "OUROBOROS_MODEL_FALLBACK",
    "OUROBOROS_MODEL_LIGHT",
    "OUROBOROS_TIMEZONE",
)

# Owner resolution is not built yet, so every reader passes this bucket — the same
# single-user convention as agent/memory_owner.py.
SETTINGS_OWNER_ID = "global"

_SELECT_SQL = "SELECT key, value FROM user_settings WHERE owner_id = :owner_id"


class UserSettingsStore(ABC):
    """Rows of the ``user_settings`` table that belong to one owner."""

    @abstractmethod
    def overrides_for(self, owner_id: str) -> dict[str, str]: ...


class UserSettingsCache(ABC):
    """Overrides already read in this process; an empty dict is a cached "no overrides"."""

    @abstractmethod
    def get(self, owner_id: str) -> dict[str, str] | None: ...

    @abstractmethod
    def put(self, owner_id: str, overrides: dict[str, str]) -> None: ...


class SqlUserSettingsStore(UserSettingsStore):
    def overrides_for(self, owner_id: str) -> dict[str, str]:
        with resolve_runtime_engine("user settings").connect() as conn:
            rows = conn.execute(text(_SELECT_SQL), {"owner_id": owner_id}).mappings().fetchall()
        # A key the deployment stopped offering stays in the table but is ignored here.
        return {str(row["key"]): str(row["value"]) for row in rows if row["key"] in USER_SETTING_KEYS}


class InMemoryUserSettingsCache(UserSettingsCache):
    def __init__(self) -> None:
        self._overrides: dict[str, dict[str, str]] = {}

    def get(self, owner_id: str) -> dict[str, str] | None:
        return self._overrides.get(owner_id)

    def put(self, owner_id: str, overrides: dict[str, str]) -> None:
        self._overrides[owner_id] = dict(overrides)


class UserSettings:
    """The settings in force for an owner: their stored overrides, else the instance values."""

    def __init__(self, instance: AppSettings, store: UserSettingsStore, cache: UserSettingsCache) -> None:
        self._instance = instance
        self._store = store
        self._cache = cache

    def OUROBOROS_EFFORT_TASK(self, owner_id: str) -> str:
        return self._overrides(owner_id).get("OUROBOROS_EFFORT_TASK") or self._instance.OUROBOROS_EFFORT_TASK

    def OUROBOROS_LOCALE(self, owner_id: str) -> str:
        return self._overrides(owner_id).get("OUROBOROS_LOCALE") or self._instance.OUROBOROS_LOCALE

    def OUROBOROS_MEMORY_DISTILL(self, owner_id: str) -> bool:
        # A malformed override is ignored the same way a retired key is: the instance value applies.
        override = self._overrides(owner_id).get("OUROBOROS_MEMORY_DISTILL", "").strip().lower()
        if override in {"true", "1"}:
            return True
        if override in {"false", "0"}:
            return False
        return self._instance.OUROBOROS_MEMORY_DISTILL

    def OUROBOROS_MODEL(self, owner_id: str) -> str:
        return self._overrides(owner_id).get("OUROBOROS_MODEL") or self._instance.OUROBOROS_MODEL

    def OUROBOROS_MODEL_FALLBACK(self, owner_id: str) -> str:
        return self._overrides(owner_id).get("OUROBOROS_MODEL_FALLBACK") or self._instance.OUROBOROS_MODEL_FALLBACK

    def OUROBOROS_MODEL_LIGHT(self, owner_id: str) -> str:
        return self._overrides(owner_id).get("OUROBOROS_MODEL_LIGHT") or self._instance.OUROBOROS_MODEL_LIGHT

    def OUROBOROS_TIMEZONE(self, owner_id: str) -> str:
        return self._overrides(owner_id).get("OUROBOROS_TIMEZONE") or self._instance.OUROBOROS_TIMEZONE

    def _overrides(self, owner_id: str) -> dict[str, str]:
        cached = self._cache.get(owner_id)
        if cached is not None:
            return cached
        loaded = self._store.overrides_for(owner_id)
        self._cache.put(owner_id, loaded)
        return loaded


user_settings = UserSettings(load_settings(), SqlUserSettingsStore(), InMemoryUserSettingsCache())


def reset_user_settings_for_tests(store: UserSettingsStore | None = None) -> None:
    """Rebuild the process-wide object from the current settings and an empty cache."""
    # Rebinding the name would strand readers that already imported the object.
    user_settings._instance = load_settings()
    user_settings._store = store if store is not None else SqlUserSettingsStore()
    user_settings._cache = InMemoryUserSettingsCache()


__all__ = [
    "SETTINGS_OWNER_ID",
    "USER_SETTING_KEYS",
    "InMemoryUserSettingsCache",
    "SqlUserSettingsStore",
    "UserSettings",
    "UserSettingsCache",
    "UserSettingsStore",
    "reset_user_settings_for_tests",
    "user_settings",
]
