from __future__ import annotations


def settings_cache_key(owner_id: str, agent_id: str, key: str) -> str:
    return f"user-settings:{owner_id}:{agent_id}:{key}"


__all__ = ["settings_cache_key"]
