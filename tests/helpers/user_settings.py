"""Test double for the user settings store."""

from __future__ import annotations

from ouroboros.storage.settings.user_settings import SETTINGS_OWNER_ID, UserSettingsStore


class StubUserSettingsStore(UserSettingsStore):
    """In-memory ``UserSettingsStore`` that counts how often the table is read."""

    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        self.reads = 0
        self._overrides = overrides or {}

    def overrides_for(self, owner_id: str) -> dict[str, str]:
        assert owner_id == SETTINGS_OWNER_ID
        self.reads += 1
        return dict(self._overrides)
