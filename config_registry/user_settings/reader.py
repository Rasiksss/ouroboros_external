from __future__ import annotations

import json
from typing import cast, get_args

from pydantic import BaseModel, ConfigDict

from config_registry.user_settings.infra.cache import settings_cache_key
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
from infra.cache import cache_factory
from infra.cache.interface import IAsyncCache, ICache
from new_core.identity import OwnerScope
from new_core.skills.models import SkillId
from ouroboros.storage.settings.schema import AppSettings, EffortLevel
from ouroboros.storage.settings.store import load_settings

_FLAG_TEXTS: dict[str, bool] = {"true": True, "false": False, "1": True, "0": False}

REASONING_PROGRESS_KEY = "OUROBOROS_REASONING_PROGRESS_ENABLED"
USER_ENABLED_SKILL_IDS_KEY = "OUROBOROS_USER_ENABLED_SKILL_IDS"


class ResolvedUserSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    OUROBOROS_USER_ENABLED_MCP_IDS: list[str]
    OUROBOROS_EFFORT_TASK: EffortLevel
    OUROBOROS_LOCALE: str
    OUROBOROS_MEMORY_DISTILL: bool
    OUROBOROS_MODEL: str
    OUROBOROS_MODEL_FALLBACK: str
    OUROBOROS_MODEL_LIGHT: str
    OUROBOROS_REASONING_PROGRESS_ENABLED: bool
    OUROBOROS_TIMEZONE: str
    OUROBOROS_TOOL_CONFIRMATION_ENABLED: bool
    OUROBOROS_USER_ENABLED_SKILL_IDS: list[str]


class UserSettings:
    def __init__(
        self,
        instance: AppSettings,
        store: UserSettingsStore,
        async_store: IAsyncUserSettingsStore,
        writer: ISyncUserSettingsRepo,
        cache: ICache,
        async_cache: IAsyncCache,
    ) -> None:
        self._instance = instance
        self._store = store
        self._async_store = async_store
        self._writer = writer
        self._cache = cache
        self._async_cache = async_cache

    async def user_settings(self, owner_id: str, agent_id: str) -> ResolvedUserSettings:
        stored = await self._async_store.get_setting_values(owner_id, agent_id)
        instance = self._instance
        return ResolvedUserSettings(
            OUROBOROS_USER_ENABLED_MCP_IDS=self._resolve_mcp_ids(
                stored.get("OUROBOROS_USER_ENABLED_MCP_IDS"), instance.OUROBOROS_USER_ENABLED_MCP_IDS
            ),
            OUROBOROS_EFFORT_TASK=self._resolve_effort(
                stored.get("OUROBOROS_EFFORT_TASK"), instance.OUROBOROS_EFFORT_TASK
            ),
            OUROBOROS_LOCALE=self._resolve_text(stored.get("OUROBOROS_LOCALE"), instance.OUROBOROS_LOCALE),
            OUROBOROS_MEMORY_DISTILL=self._resolve_flag(
                stored.get("OUROBOROS_MEMORY_DISTILL"), instance.OUROBOROS_MEMORY_DISTILL
            ),
            OUROBOROS_MODEL=self._resolve_text(stored.get("OUROBOROS_MODEL"), instance.OUROBOROS_MODEL),
            OUROBOROS_MODEL_FALLBACK=self._resolve_text(
                stored.get("OUROBOROS_MODEL_FALLBACK"), instance.OUROBOROS_MODEL_FALLBACK
            ),
            OUROBOROS_MODEL_LIGHT=self._resolve_text(
                stored.get("OUROBOROS_MODEL_LIGHT"), instance.OUROBOROS_MODEL_LIGHT
            ),
            OUROBOROS_REASONING_PROGRESS_ENABLED=self._resolve_flag(
                stored.get(REASONING_PROGRESS_KEY), instance.OUROBOROS_REASONING_PROGRESS_ENABLED
            ),
            OUROBOROS_TIMEZONE=self._resolve_text(stored.get("OUROBOROS_TIMEZONE"), instance.OUROBOROS_TIMEZONE),
            OUROBOROS_TOOL_CONFIRMATION_ENABLED=self._resolve_flag(
                stored.get("OUROBOROS_TOOL_CONFIRMATION_ENABLED"), instance.OUROBOROS_TOOL_CONFIRMATION_ENABLED
            ),
            OUROBOROS_USER_ENABLED_SKILL_IDS=self._resolve_canonical_skill_ids(
                stored.get(USER_ENABLED_SKILL_IDS_KEY),
                instance.OUROBOROS_USER_ENABLED_SKILL_IDS,
            ),
        )

    async def mcp_user_server_ids(self, owner_id: str, agent_id: str) -> list[str]:
        stored = await self._async_store.get_setting_values(owner_id, agent_id)
        return self._resolve_mcp_ids(
            stored.get("OUROBOROS_USER_ENABLED_MCP_IDS"), self._instance.OUROBOROS_USER_ENABLED_MCP_IDS
        )

    async def enabled_skill_ids(self, owner_id: str, agent_id: str) -> list[str]:
        stored = await self._async_store.get_setting_values(owner_id, agent_id)
        return self._resolve_canonical_skill_ids(
            stored.get(USER_ENABLED_SKILL_IDS_KEY), self._instance.OUROBOROS_USER_ENABLED_SKILL_IDS
        )

    @staticmethod
    def _resolve_text(stored: str | None, instance: str) -> str:
        return (str(stored).strip() if stored else instance).strip()

    @staticmethod
    def _resolve_flag(stored: str | None, instance: bool) -> bool:
        if not stored:
            return instance
        text = str(stored).strip().lower()
        if text not in _FLAG_TEXTS:
            raise ValueError(f"boolean setting value must be one of {sorted(_FLAG_TEXTS)}, got {text!r}")
        return _FLAG_TEXTS[text]

    @staticmethod
    def _resolve_effort(stored: str | None, instance: EffortLevel) -> EffortLevel:
        text = UserSettings._resolve_text(stored, instance)
        return cast(EffortLevel, text) if text in get_args(EffortLevel) else instance

    @staticmethod
    def _parse_string_list(text: str | None, *, reject_malformed_json: bool = False) -> list[str] | None:
        text = str(text or "").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            if reject_malformed_json and text.startswith(("[", "{")):
                return None
            parsed = text.split(",")
        if not isinstance(parsed, list):
            return None
        normalized: list[str] = []
        for item in parsed:
            if not isinstance(item, str) or not item.strip():
                return None
            entry = item.strip()
            if entry not in normalized:
                normalized.append(entry)
        return normalized

    @staticmethod
    def _resolve_comma_sep_values(text: str | None) -> list[str]:
        text = str(text or "").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = text.split(",")
        if not isinstance(parsed, list):
            return []
        normalized: list[str] = []
        for item in parsed:
            if not isinstance(item, str) or not item.strip():
                return []
            server_id = item.strip()
            if server_id not in normalized:
                normalized.append(server_id)
        return normalized

    @staticmethod
    def _resolve_mcp_ids(stored: str | None, instance: list[str]) -> list[str]:
        if stored is None:
            return instance
        return UserSettings._resolve_comma_sep_values(stored)

    @staticmethod
    def _resolve_canonical_skill_ids(stored: str | None, instance: list[str] | None = None) -> list[str]:
        if stored is None:
            values: list[str] = list(instance or [])
        else:
            parsed = UserSettings._parse_string_list(stored, reject_malformed_json=True)
            if parsed is None:
                raise ValueError("enabled skill ids must be a list of canonical skill ids")
            values = parsed
        try:
            for value in values:
                SkillId.parse(value)
        except ValueError as failure:
            raise ValueError("enabled skill ids must be a list of canonical skill ids") from failure
        return values

    def OUROBOROS_USER_ENABLED_MCP_IDS(self, scope: OwnerScope) -> list[str]:
        stored = self._override(scope, "OUROBOROS_USER_ENABLED_MCP_IDS")
        return self._resolve_mcp_ids(stored, self._instance.OUROBOROS_USER_ENABLED_MCP_IDS)

    def OUROBOROS_EFFORT_TASK(self, scope: OwnerScope) -> str:
        stored = self._override(scope, "OUROBOROS_EFFORT_TASK")
        return self._resolve_text(stored, self._instance.OUROBOROS_EFFORT_TASK)

    def OUROBOROS_LOCALE(self, scope: OwnerScope) -> str:
        stored = self._override(scope, "OUROBOROS_LOCALE")
        return self._resolve_text(stored, self._instance.OUROBOROS_LOCALE)

    def OUROBOROS_MEMORY_DISTILL(self, scope: OwnerScope) -> bool:
        stored = self._override(scope, "OUROBOROS_MEMORY_DISTILL")
        return self._resolve_flag(stored, self._instance.OUROBOROS_MEMORY_DISTILL)

    def OUROBOROS_MODEL(self, scope: OwnerScope) -> str:
        stored = self._override(scope, "OUROBOROS_MODEL")
        return self._resolve_text(stored, self._instance.OUROBOROS_MODEL)

    def OUROBOROS_MODEL_FALLBACK(self, scope: OwnerScope) -> str:
        stored = self._override(scope, "OUROBOROS_MODEL_FALLBACK")
        return self._resolve_text(stored, self._instance.OUROBOROS_MODEL_FALLBACK)

    def OUROBOROS_MODEL_LIGHT(self, scope: OwnerScope) -> str:
        stored = self._override(scope, "OUROBOROS_MODEL_LIGHT")
        return self._resolve_text(stored, self._instance.OUROBOROS_MODEL_LIGHT)

    def OUROBOROS_REASONING_PROGRESS_ENABLED(self, scope: OwnerScope) -> bool:
        stored = self._override(scope, REASONING_PROGRESS_KEY)
        return self._resolve_flag(stored, self._instance.OUROBOROS_REASONING_PROGRESS_ENABLED)

    def OUROBOROS_TIMEZONE(self, scope: OwnerScope) -> str:
        stored = self._override(scope, "OUROBOROS_TIMEZONE")
        return self._resolve_text(stored, self._instance.OUROBOROS_TIMEZONE)

    def OUROBOROS_TOOL_CONFIRMATION_ENABLED(self, scope: OwnerScope) -> bool:
        stored = self._override(scope, "OUROBOROS_TOOL_CONFIRMATION_ENABLED")
        return self._resolve_flag(stored, self._instance.OUROBOROS_TOOL_CONFIRMATION_ENABLED)

    def OUROBOROS_USER_ENABLED_SKILL_IDS(self, scope: OwnerScope) -> list[str]:
        stored = self._override(scope, USER_ENABLED_SKILL_IDS_KEY)
        return self._resolve_canonical_skill_ids(stored, self._instance.OUROBOROS_USER_ENABLED_SKILL_IDS)

    async def async_OUROBOROS_USER_ENABLED_MCP_IDS(self, scope: OwnerScope) -> list[str]:
        stored = await self._async_override(scope, "OUROBOROS_USER_ENABLED_MCP_IDS")
        return self._resolve_mcp_ids(stored, self._instance.OUROBOROS_USER_ENABLED_MCP_IDS)

    async def async_OUROBOROS_EFFORT_TASK(self, scope: OwnerScope) -> str:
        stored = await self._async_override(scope, "OUROBOROS_EFFORT_TASK")
        return self._resolve_text(stored, self._instance.OUROBOROS_EFFORT_TASK)

    async def async_OUROBOROS_LOCALE(self, scope: OwnerScope) -> str:
        stored = await self._async_override(scope, "OUROBOROS_LOCALE")
        return self._resolve_text(stored, self._instance.OUROBOROS_LOCALE)

    async def async_OUROBOROS_MEMORY_DISTILL(self, scope: OwnerScope) -> bool:
        stored = await self._async_override(scope, "OUROBOROS_MEMORY_DISTILL")
        return self._resolve_flag(stored, self._instance.OUROBOROS_MEMORY_DISTILL)

    async def async_OUROBOROS_MODEL(self, scope: OwnerScope) -> str:
        stored = await self._async_override(scope, "OUROBOROS_MODEL")
        return self._resolve_text(stored, self._instance.OUROBOROS_MODEL)

    async def async_OUROBOROS_MODEL_FALLBACK(self, scope: OwnerScope) -> str:
        stored = await self._async_override(scope, "OUROBOROS_MODEL_FALLBACK")
        return self._resolve_text(stored, self._instance.OUROBOROS_MODEL_FALLBACK)

    async def async_OUROBOROS_MODEL_LIGHT(self, scope: OwnerScope) -> str:
        stored = await self._async_override(scope, "OUROBOROS_MODEL_LIGHT")
        return self._resolve_text(stored, self._instance.OUROBOROS_MODEL_LIGHT)

    async def async_OUROBOROS_REASONING_PROGRESS_ENABLED(self, scope: OwnerScope) -> bool:
        stored = await self._async_override(scope, REASONING_PROGRESS_KEY)
        return self._resolve_flag(stored, self._instance.OUROBOROS_REASONING_PROGRESS_ENABLED)

    async def async_OUROBOROS_TIMEZONE(self, scope: OwnerScope) -> str:
        stored = await self._async_override(scope, "OUROBOROS_TIMEZONE")
        return self._resolve_text(stored, self._instance.OUROBOROS_TIMEZONE)

    async def async_OUROBOROS_TOOL_CONFIRMATION_ENABLED(self, scope: OwnerScope) -> bool:
        stored = await self._async_override(scope, "OUROBOROS_TOOL_CONFIRMATION_ENABLED")
        return self._resolve_flag(stored, self._instance.OUROBOROS_TOOL_CONFIRMATION_ENABLED)

    async def async_OUROBOROS_USER_ENABLED_SKILL_IDS(self, scope: OwnerScope) -> list[str]:
        stored = await self._async_override(scope, USER_ENABLED_SKILL_IDS_KEY)
        return self._resolve_canonical_skill_ids(stored, self._instance.OUROBOROS_USER_ENABLED_SKILL_IDS)

    def set_override(self, scope: OwnerScope, key: str, value: str) -> None:
        self._writer.set_override(scope.owner_id, scope.agent_id, key, value)
        self._cache.delete_value(settings_cache_key(scope.owner_id, scope.agent_id, key))

    def _override(self, scope: OwnerScope, key: str) -> str | None:
        entry_key = settings_cache_key(scope.owner_id, scope.agent_id, key)
        cached = self._cache.get_value(entry_key)
        if isinstance(cached, dict) and "value" in cached:
            return None if cached["value"] is None else str(cached["value"])
        stored = self._store.get_setting_value(scope.owner_id, scope.agent_id, key)
        self._cache.set_value(entry_key, {"value": stored})
        return stored

    async def _async_override(self, scope: OwnerScope, key: str) -> str | None:
        entry_key = settings_cache_key(scope.owner_id, scope.agent_id, key)
        cached = await self._async_cache.get_value(entry_key)
        if isinstance(cached, dict) and "value" in cached:
            return None if cached["value"] is None else str(cached["value"])
        stored = await self._async_store.get_setting_value(scope.owner_id, scope.agent_id, key)
        await self._async_cache.set_value(entry_key, {"value": stored})
        return stored


class UserSettingsFactory:
    def __init__(self) -> None:
        self._settings: UserSettings | None = None

    def settings(self) -> UserSettings:
        if self._settings is None:
            self._settings = UserSettings(
                load_settings(),
                SqlUserSettingsStore(),
                SqlAsyncUserSettingsStore(),
                SqlSyncUserSettingsRepo(),
                cache_factory.cache(),
                cache_factory.async_cache(),
            )
        return self._settings

    def reset_for_tests(self, settings: UserSettings | None) -> None:
        self._settings = settings


user_settings_factory = UserSettingsFactory()


__all__ = [
    "REASONING_PROGRESS_KEY",
    "USER_ENABLED_SKILL_IDS_KEY",
    "ResolvedUserSettings",
    "UserSettings",
    "UserSettingsFactory",
    "user_settings_factory",
]
