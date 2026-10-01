"""Per-owner setting overrides: what the settings object resolves and caches."""

from __future__ import annotations

from sqlalchemy import text

from ouroboros.storage.runtime_engine import resolve_runtime_engine
from ouroboros.storage.settings.store import load_settings
from ouroboros.storage.settings.user_settings import (
    SETTINGS_OWNER_ID,
    InMemoryUserSettingsCache,
    SqlUserSettingsStore,
    UserSettings,
    UserSettingsStore,
)
from tests.helpers.user_settings import StubUserSettingsStore


def _user_settings(store: UserSettingsStore) -> UserSettings:
    return UserSettings(load_settings(), store, InMemoryUserSettingsCache())


def _insert_row(owner_id: str, key: str, value: str) -> None:
    with resolve_runtime_engine("user settings").begin() as conn:
        conn.execute(
            text("INSERT INTO user_settings (owner_id, key, value) VALUES (:owner_id, :key, :value)"),
            {"owner_id": owner_id, "key": key, "value": value},
        )


class _PerOwnerStore(UserSettingsStore):
    def __init__(self, by_owner: dict[str, dict[str, str]]) -> None:
        self._by_owner = by_owner

    def overrides_for(self, owner_id: str) -> dict[str, str]:
        return dict(self._by_owner.get(owner_id, {}))


def test_without_overrides_the_instance_values_apply():
    instance = load_settings()
    settings = _user_settings(StubUserSettingsStore())
    assert settings.OUROBOROS_TIMEZONE(SETTINGS_OWNER_ID) == instance.OUROBOROS_TIMEZONE
    assert settings.OUROBOROS_LOCALE(SETTINGS_OWNER_ID) == instance.OUROBOROS_LOCALE
    assert settings.OUROBOROS_EFFORT_TASK(SETTINGS_OWNER_ID) == instance.OUROBOROS_EFFORT_TASK


def test_an_override_replaces_only_its_own_key():
    settings = _user_settings(StubUserSettingsStore({"OUROBOROS_TIMEZONE": "Asia/Vladivostok"}))
    assert settings.OUROBOROS_TIMEZONE(SETTINGS_OWNER_ID) == "Asia/Vladivostok"
    assert settings.OUROBOROS_LOCALE(SETTINGS_OWNER_ID) == load_settings().OUROBOROS_LOCALE


def test_overrides_are_read_once():
    store = StubUserSettingsStore({"OUROBOROS_TIMEZONE": "Asia/Vladivostok"})
    settings = _user_settings(store)
    assert settings.OUROBOROS_TIMEZONE(SETTINGS_OWNER_ID) == "Asia/Vladivostok"
    assert settings.OUROBOROS_LOCALE(SETTINGS_OWNER_ID) == load_settings().OUROBOROS_LOCALE
    assert settings.OUROBOROS_EFFORT_TASK(SETTINGS_OWNER_ID) == load_settings().OUROBOROS_EFFORT_TASK
    assert store.reads == 1


def test_an_owner_without_overrides_is_cached_too():
    """A cached empty dict means "no overrides", not "go read the table again"."""
    store = StubUserSettingsStore()
    settings = _user_settings(store)
    assert settings.OUROBOROS_TIMEZONE(SETTINGS_OWNER_ID) == load_settings().OUROBOROS_TIMEZONE
    assert settings.OUROBOROS_LOCALE(SETTINGS_OWNER_ID) == load_settings().OUROBOROS_LOCALE
    assert store.reads == 1


def test_each_owner_resolves_its_own_overrides():
    settings = _user_settings(_PerOwnerStore({"alice": {"OUROBOROS_TIMEZONE": "Asia/Vladivostok"}}))
    assert settings.OUROBOROS_TIMEZONE("alice") == "Asia/Vladivostok"
    assert settings.OUROBOROS_TIMEZONE("bob") == load_settings().OUROBOROS_TIMEZONE


def test_a_bool_override_replaces_the_instance_value():
    settings = _user_settings(StubUserSettingsStore({"OUROBOROS_MEMORY_DISTILL": "false"}))
    assert settings.OUROBOROS_MEMORY_DISTILL(SETTINGS_OWNER_ID) is False


def test_a_model_override_replaces_the_instance_value():
    settings = _user_settings(
        StubUserSettingsStore(
            {
                "OUROBOROS_MODEL": "gigachat-b2bank::GigaChat-Max",
                "OUROBOROS_MODEL_LIGHT": "gigachat-b2bank::GigaChat-Lite",
            }
        )
    )
    assert settings.OUROBOROS_MODEL(SETTINGS_OWNER_ID) == "gigachat-b2bank::GigaChat-Max"
    assert settings.OUROBOROS_MODEL_LIGHT(SETTINGS_OWNER_ID) == "gigachat-b2bank::GigaChat-Lite"
    assert settings.OUROBOROS_MODEL_FALLBACK(SETTINGS_OWNER_ID) == load_settings().OUROBOROS_MODEL_FALLBACK


def test_a_malformed_override_falls_back_to_the_instance_value():
    settings = _user_settings(StubUserSettingsStore({"OUROBOROS_MEMORY_DISTILL": "maybe"}))
    assert settings.OUROBOROS_MEMORY_DISTILL(SETTINGS_OWNER_ID) == load_settings().OUROBOROS_MEMORY_DISTILL


def test_sql_store_reads_one_owner_and_known_keys_only():
    _insert_row(SETTINGS_OWNER_ID, "OUROBOROS_TIMEZONE", "Asia/Vladivostok")
    _insert_row(SETTINGS_OWNER_ID, "OUROBOROS_RETIRED_KEY", "whatever")
    _insert_row("someone-else", "OUROBOROS_LOCALE", "en-US")

    assert SqlUserSettingsStore().overrides_for(SETTINGS_OWNER_ID) == {"OUROBOROS_TIMEZONE": "Asia/Vladivostok"}
