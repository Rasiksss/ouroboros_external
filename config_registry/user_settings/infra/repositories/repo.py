from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from config_registry.user_settings.interfaces import (
    IAsyncUserSettingsStore,
    ISyncUserSettingsRepo,
    IUserSettingsRepo,
    UserSettingsStore,
)
from infra.async_engine import async_engine_factory
from ouroboros.storage.runtime_engine import resolve_runtime_engine


class SqlUserSettingsStore(UserSettingsStore):
    def get_setting_value(self, owner_id: str, agent_id: str, key: str) -> str | None:
        with resolve_runtime_engine("user settings").connect() as conn:
            value = conn.execute(
                text(
                    "SELECT value FROM user_settings WHERE owner_id = :owner_id AND agent_id = :agent_id AND key = :key"
                ),
                {"owner_id": owner_id, "agent_id": agent_id, "key": key},
            ).scalar_one_or_none()
        return None if value is None else str(value)


class SqlSyncUserSettingsRepo(ISyncUserSettingsRepo):
    def set_override(self, owner_id: str, agent_id: str, key: str, value: str) -> None:
        with resolve_runtime_engine("user settings").begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO user_settings (owner_id, agent_id, key, value) "
                    "VALUES (:owner_id, :agent_id, :key, :value) "
                    "ON CONFLICT(owner_id, agent_id, key) DO UPDATE SET value = excluded.value"
                ),
                {"owner_id": owner_id, "agent_id": agent_id, "key": key, "value": value},
            )


class SqlAsyncUserSettingsStore(IAsyncUserSettingsStore):
    async def get_setting_values(self, owner_id: str, agent_id: str) -> dict[str, str]:
        async with async_engine_factory.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT key, value FROM user_settings WHERE owner_id = :owner_id AND agent_id = :agent_id"
                        ),
                        {"owner_id": owner_id, "agent_id": agent_id},
                    )
                )
                .mappings()
                .all()
            )
        return {str(row["key"]): str(row["value"]) for row in rows}

    async def get_setting_value(self, owner_id: str, agent_id: str, key: str) -> str | None:
        async with async_engine_factory.engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT value FROM user_settings WHERE owner_id = :owner_id AND agent_id = :agent_id AND key = :key"
                ),
                {"owner_id": owner_id, "agent_id": agent_id, "key": key},
            )
            value = result.scalar_one_or_none()
        return None if value is None else str(value)


class SqlUserSettingsRepo(IUserSettingsRepo):
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def set_override(self, owner_id: str, agent_id: str, key: str, value: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO user_settings (owner_id, agent_id, key, value) "
                    "VALUES (:owner_id, :agent_id, :key, :value) "
                    "ON CONFLICT(owner_id, agent_id, key) DO UPDATE SET value = excluded.value"
                ),
                {"owner_id": owner_id, "agent_id": agent_id, "key": key, "value": value},
            )

    async def clear_override(self, owner_id: str, agent_id: str, key: str) -> bool:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text("DELETE FROM user_settings WHERE owner_id = :owner_id AND agent_id = :agent_id AND key = :key"),
                {"owner_id": owner_id, "agent_id": agent_id, "key": key},
            )
        return bool(result.rowcount)


__all__ = [
    "SqlAsyncUserSettingsStore",
    "SqlSyncUserSettingsRepo",
    "SqlUserSettingsRepo",
    "SqlUserSettingsStore",
]
