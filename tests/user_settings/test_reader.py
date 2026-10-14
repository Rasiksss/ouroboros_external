"""Setting overrides: what the settings object resolves, per pair and per key."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from config_registry.user_settings.infra.repositories.repo import SqlUserSettingsStore
from config_registry.user_settings.reader import USER_ENABLED_SKILL_IDS_KEY, UserSettings
from infra.cache.inmemory import AsyncInMemoryCache, InMemoryCache
from infra.cache.null import AsyncNullCache, NullCache
from new_core.identity import OwnerScope, SurfaceId
from ouroboros.storage.runtime_engine import resolve_runtime_engine
from ouroboros.storage.settings.schema import AppSettings
from ouroboros.storage.settings.store import load_settings
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.user_settings import NoUserSettingsWrites, StubAsyncUserSettingsStore, StubUserSettingsStore

OWNER = "alice"
OTHER_OWNER = "bob"


def _scope(owner_id: str = OWNER, agent_id: str = "main") -> OwnerScope:
    return OwnerScope(owner_id=owner_id, agent_id=agent_id, surface_id=SurfaceId.BROWSER, chat_id="c1")


def _run(coro):
    return asyncio.run(coro)


def _user_settings(store: StubUserSettingsStore) -> UserSettings:
    return UserSettings(
        load_settings(),
        store,
        StubAsyncUserSettingsStore(mirror=store),
        NoUserSettingsWrites(),
        NullCache(),
        AsyncNullCache(),
    )


def _insert_row(owner_id: str, agent_id: str, key: str, value: str) -> None:
    with resolve_runtime_engine("user settings").begin() as conn:
        conn.execute(
            text(
                "INSERT INTO user_settings (owner_id, agent_id, key, value) VALUES (:owner_id, :agent_id, :key, :value)"
            ),
            {"owner_id": owner_id, "agent_id": agent_id, "key": key, "value": value},
        )


def test_user_settings_has_no_nominal_base_class():
    assert UserSettings.__bases__ == (object,)


def test_without_overrides_the_instance_values_apply():
    instance = load_settings()
    settings = _user_settings(StubUserSettingsStore())
    assert settings.OUROBOROS_TIMEZONE(_scope()) == instance.OUROBOROS_TIMEZONE
    assert settings.OUROBOROS_LOCALE(_scope()) == instance.OUROBOROS_LOCALE
    assert settings.OUROBOROS_EFFORT_TASK(_scope()) == instance.OUROBOROS_EFFORT_TASK


def test_an_override_replaces_only_its_own_key():
    settings = _user_settings(StubUserSettingsStore({"OUROBOROS_TIMEZONE": "Asia/Vladivostok"}))
    assert settings.OUROBOROS_TIMEZONE(_scope()) == "Asia/Vladivostok"
    assert settings.OUROBOROS_LOCALE(_scope()) == load_settings().OUROBOROS_LOCALE


def test_each_accessor_makes_sense_of_its_own_key_and_costs_its_own_read():
    store = StubUserSettingsStore({"OUROBOROS_TIMEZONE": "Asia/Vladivostok"})
    settings = _user_settings(store)
    assert settings.OUROBOROS_TIMEZONE(_scope()) == "Asia/Vladivostok"
    assert settings.OUROBOROS_LOCALE(_scope()) == load_settings().OUROBOROS_LOCALE
    assert store.reads == 2


def test_each_read_asks_the_store_because_nothing_caches():
    """A writer runs in another process; a value read twice must not be a value read once."""
    store = StubUserSettingsStore({"OUROBOROS_TIMEZONE": "Asia/Vladivostok"})
    settings = _user_settings(store)
    settings.OUROBOROS_TIMEZONE(_scope())
    settings.OUROBOROS_TIMEZONE(_scope())
    assert store.reads == 2


def test_two_agents_of_one_owner_resolve_independent_values():
    settings = _user_settings(
        StubUserSettingsStore(
            by_pair={
                (OWNER, "main"): {
                    "OUROBOROS_MODEL": "gigachat-b2bank::GigaChat-Max",
                    "OUROBOROS_TIMEZONE": "Asia/Tokyo",
                },
                (OWNER, "sub-1"): {"OUROBOROS_MODEL": "gigachat-b2bank::GigaChat-Lite"},
            }
        )
    )
    assert settings.OUROBOROS_MODEL(_scope(agent_id="main")) == "gigachat-b2bank::GigaChat-Max"
    assert settings.OUROBOROS_MODEL(_scope(agent_id="sub-1")) == "gigachat-b2bank::GigaChat-Lite"
    assert settings.OUROBOROS_MODEL(_scope(agent_id="sub-2")) == load_settings().OUROBOROS_MODEL
    assert settings.OUROBOROS_TIMEZONE(_scope(agent_id="main")) == "Asia/Tokyo"
    assert settings.OUROBOROS_TIMEZONE(_scope(agent_id="sub-1")) == load_settings().OUROBOROS_TIMEZONE


def test_two_owners_under_the_same_agent_id_do_not_see_each_other():
    settings = _user_settings(
        StubUserSettingsStore(
            by_pair={
                (OWNER, "main"): {"OUROBOROS_MODEL": "mine"},
                (OTHER_OWNER, "main"): {"OUROBOROS_MODEL": "theirs"},
            }
        )
    )
    assert settings.OUROBOROS_MODEL(_scope(OWNER)) == "mine"
    assert settings.OUROBOROS_MODEL(_scope(OTHER_OWNER)) == "theirs"


def test_a_bool_override_replaces_the_instance_value():
    settings = _user_settings(StubUserSettingsStore({"OUROBOROS_MEMORY_DISTILL": "false"}))
    assert settings.OUROBOROS_MEMORY_DISTILL(_scope()) is False


def test_the_confirmation_override_turns_on_what_the_instance_keeps_off():
    settings = _user_settings(StubUserSettingsStore({"OUROBOROS_TOOL_CONFIRMATION_ENABLED": "true"}))
    assert load_settings().OUROBOROS_TOOL_CONFIRMATION_ENABLED is False
    assert settings.OUROBOROS_TOOL_CONFIRMATION_ENABLED(_scope()) is True
    assert _run(settings.async_OUROBOROS_TOOL_CONFIRMATION_ENABLED(_scope())) is True
    assert _run(settings.user_settings(OWNER, "main")).OUROBOROS_TOOL_CONFIRMATION_ENABLED is True


def test_a_model_override_replaces_the_instance_value():
    settings = _user_settings(
        StubUserSettingsStore(
            {
                "OUROBOROS_MODEL": "gigachat-b2bank::GigaChat-Max",
                "OUROBOROS_MODEL_LIGHT": "gigachat-b2bank::GigaChat-Lite",
            }
        )
    )
    assert settings.OUROBOROS_MODEL(_scope()) == "gigachat-b2bank::GigaChat-Max"
    assert settings.OUROBOROS_MODEL_LIGHT(_scope()) == "gigachat-b2bank::GigaChat-Lite"
    assert settings.OUROBOROS_MODEL_FALLBACK(_scope()) == load_settings().OUROBOROS_MODEL_FALLBACK


def test_mcp_server_ids_are_empty_without_an_owner_setting():
    settings = _user_settings(StubUserSettingsStore())

    assert settings.OUROBOROS_USER_ENABLED_MCP_IDS(_scope()) == []
    assert _run(settings.user_settings(OWNER, "main")).OUROBOROS_USER_ENABLED_MCP_IDS == []


def test_mcp_server_ids_fall_back_to_the_env_value_when_no_override(monkeypatch):
    monkeypatch.setenv("OUROBOROS_USER_ENABLED_MCP_IDS", '["search", "calendar"]')
    from ouroboros.storage.settings.bootstrap import bootstrap_config

    bootstrap_config(force=True)
    settings = _user_settings(StubUserSettingsStore())

    assert settings.OUROBOROS_USER_ENABLED_MCP_IDS(_scope()) == ["search", "calendar"]
    assert _run(settings.user_settings(OWNER, "main")).OUROBOROS_USER_ENABLED_MCP_IDS == ["search", "calendar"]


def test_blank_mcp_server_ids_override_denies_a_nonempty_instance_default(monkeypatch):
    monkeypatch.setenv("OUROBOROS_USER_ENABLED_MCP_IDS", '["search"]')
    from ouroboros.storage.settings.bootstrap import bootstrap_config

    bootstrap_config(force=True)
    settings = _user_settings(StubUserSettingsStore({"OUROBOROS_USER_ENABLED_MCP_IDS": ""}))

    assert settings.OUROBOROS_USER_ENABLED_MCP_IDS(_scope()) == []
    assert _run(settings.user_settings(OWNER, "main")).OUROBOROS_USER_ENABLED_MCP_IDS == []


@parametrize_from_dict(
    {
        "json array": {"stored": '["search", " calendar "]', "expected": ["search", "calendar"]},
        "comma separated": {"stored": "search, calendar", "expected": ["search", "calendar"]},
        "comma separated id with brackets": {"stored": "crm[prod]", "expected": ["crm[prod]"]},
        "empty array": {"stored": "[]", "expected": []},
        "empty text": {"stored": "", "expected": []},
        "json object": {"stored": '{"server": "search"}', "expected": []},
        "non-string item": {"stored": '["search", 1]', "expected": []},
    }
)
def test_mcp_server_ids_parse_as_a_deny_by_default_list(stored: str, expected: list[str]):
    settings = _user_settings(StubUserSettingsStore({"OUROBOROS_USER_ENABLED_MCP_IDS": stored}))

    assert settings.OUROBOROS_USER_ENABLED_MCP_IDS(_scope()) == expected
    assert expected == _run(settings.user_settings(OWNER, "main")).OUROBOROS_USER_ENABLED_MCP_IDS


def test_two_agents_of_one_owner_resolve_independent_mcp_server_ids():
    settings = _user_settings(
        StubUserSettingsStore(
            by_pair={
                (OWNER, "main"): {"OUROBOROS_USER_ENABLED_MCP_IDS": '["search"]'},
                (OWNER, "sub-1"): {"OUROBOROS_USER_ENABLED_MCP_IDS": '["calendar"]'},
            }
        )
    )

    assert settings.OUROBOROS_USER_ENABLED_MCP_IDS(_scope(agent_id="main")) == ["search"]
    assert settings.OUROBOROS_USER_ENABLED_MCP_IDS(_scope(agent_id="sub-1")) == ["calendar"]
    assert settings.OUROBOROS_USER_ENABLED_MCP_IDS(_scope(agent_id="sub-2")) == []


def test_user_enabled_skill_ids_are_empty_without_an_owner_setting():
    settings = _user_settings(StubUserSettingsStore())

    assert settings.OUROBOROS_USER_ENABLED_SKILL_IDS(_scope()) == []
    assert _run(settings.user_settings(OWNER, "main")).OUROBOROS_USER_ENABLED_SKILL_IDS == []


def test_user_enabled_skill_ids_fall_back_to_the_instance_value():
    instance = AppSettings.model_validate(
        {"OUROBOROS_USER_ENABLED_SKILL_IDS": '["distribution:alpha", "distribution:beta"]'}
    )
    settings = UserSettings(
        instance,
        StubUserSettingsStore(),
        StubAsyncUserSettingsStore(),
        NoUserSettingsWrites(),
        NullCache(),
        AsyncNullCache(),
    )

    assert settings.OUROBOROS_USER_ENABLED_SKILL_IDS(_scope()) == ["distribution:alpha", "distribution:beta"]
    assert _run(settings.user_settings(OWNER, "main")).OUROBOROS_USER_ENABLED_SKILL_IDS == [
        "distribution:alpha",
        "distribution:beta",
    ]


def test_user_enabled_skill_ids_override_replaces_the_instance_value():
    instance = AppSettings.model_validate({"OUROBOROS_USER_ENABLED_SKILL_IDS": '["distribution:alpha"]'})
    store = StubUserSettingsStore({USER_ENABLED_SKILL_IDS_KEY: '["distribution:beta"]'})
    settings = UserSettings(
        instance,
        store,
        StubAsyncUserSettingsStore(mirror=store),
        NoUserSettingsWrites(),
        NullCache(),
        AsyncNullCache(),
    )

    assert settings.OUROBOROS_USER_ENABLED_SKILL_IDS(_scope()) == ["distribution:beta"]


@parametrize_from_dict(
    {
        "empty array": {"stored": "[]"},
        "empty text": {"stored": ""},
    }
)
def test_user_enabled_skill_ids_explicit_empty_enables_nothing(stored: str):
    instance = AppSettings.model_validate({"OUROBOROS_USER_ENABLED_SKILL_IDS": '["distribution:alpha"]'})
    store = StubUserSettingsStore({USER_ENABLED_SKILL_IDS_KEY: stored})
    settings = UserSettings(
        instance,
        store,
        StubAsyncUserSettingsStore(mirror=store),
        NoUserSettingsWrites(),
        NullCache(),
        AsyncNullCache(),
    )

    assert settings.OUROBOROS_USER_ENABLED_SKILL_IDS(_scope()) == []
    assert _run(settings.user_settings(OWNER, "main")).OUROBOROS_USER_ENABLED_SKILL_IDS == []


@parametrize_from_dict(
    {
        "json array": {
            "stored": '["distribution:alpha", " distribution:beta "]',
            "expected": ["distribution:alpha", "distribution:beta"],
        },
        "comma separated and duplicate": {
            "stored": "distribution:alpha, distribution:beta, distribution:alpha",
            "expected": ["distribution:alpha", "distribution:beta"],
        },
        "empty array": {"stored": "[]", "expected": []},
        "empty text": {"stored": "", "expected": []},
        "unknown canonical id": {"stored": "future:unknown", "expected": ["future:unknown"]},
        "canonical id with json punctuation": {
            "stored": 'future:crm[prod], future:team{blue}, future:quoted"name',
            "expected": ["future:crm[prod]", "future:team{blue}", 'future:quoted"name'],
        },
    }
)
def test_user_enabled_skill_ids_parse_as_canonical_ids(stored: str, expected: list[str]):
    settings = _user_settings(StubUserSettingsStore({USER_ENABLED_SKILL_IDS_KEY: stored}))

    assert settings.OUROBOROS_USER_ENABLED_SKILL_IDS(_scope()) == expected
    assert expected == _run(settings.user_settings(OWNER, "main")).OUROBOROS_USER_ENABLED_SKILL_IDS


@parametrize_from_dict(
    {
        "malformed array": {"stored": '["distribution:alpha"'},
        "unclosed json object": {"stored": '{"skill":"distribution:alpha"'},
        "json object": {"stored": '{"skill": "distribution:alpha"}'},
        "non-string item": {"stored": '["distribution:alpha", 1]'},
        "empty item": {"stored": '["distribution:alpha", ""]'},
        "legacy alias": {"stored": "alpha"},
        "empty source": {"stored": ":alpha"},
    }
)
def test_invalid_user_enabled_skill_ids_are_refused(stored: str):
    settings = _user_settings(StubUserSettingsStore({USER_ENABLED_SKILL_IDS_KEY: stored}))

    with pytest.raises(ValueError, match="canonical skill id"):
        settings.OUROBOROS_USER_ENABLED_SKILL_IDS(_scope())
    with pytest.raises(ValueError, match="canonical skill id"):
        _run(settings.user_settings(OWNER, "main"))


def test_owner_and_agent_skill_allowlists_are_independent():
    settings = _user_settings(
        StubUserSettingsStore(
            by_pair={
                (OWNER, "main"): {USER_ENABLED_SKILL_IDS_KEY: '["distribution:alpha"]'},
                (OWNER, "sub-1"): {USER_ENABLED_SKILL_IDS_KEY: '["distribution:beta"]'},
                (OTHER_OWNER, "main"): {USER_ENABLED_SKILL_IDS_KEY: '["distribution:gamma"]'},
            }
        )
    )

    assert settings.OUROBOROS_USER_ENABLED_SKILL_IDS(_scope(OWNER, "main")) == ["distribution:alpha"]
    assert settings.OUROBOROS_USER_ENABLED_SKILL_IDS(_scope(OWNER, "sub-1")) == ["distribution:beta"]
    assert settings.OUROBOROS_USER_ENABLED_SKILL_IDS(_scope(OTHER_OWNER, "main")) == ["distribution:gamma"]
    assert settings.OUROBOROS_USER_ENABLED_SKILL_IDS(_scope(OWNER, "sub-2")) == []


def test_the_aggregate_resolves_all_owner_keys_in_one_typed_snapshot():
    instance = load_settings()
    settings = _user_settings(
        StubUserSettingsStore(
            {
                "OUROBOROS_EFFORT_TASK": "high",
                "OUROBOROS_MODEL": "gigachat-b2bank::GigaChat-Max",
                "OUROBOROS_MEMORY_DISTILL": "false",
            }
        )
    )
    resolved = _run(settings.user_settings(OWNER, "main"))
    assert resolved.OUROBOROS_EFFORT_TASK == "high"
    assert resolved.OUROBOROS_MODEL == "gigachat-b2bank::GigaChat-Max"
    assert resolved.OUROBOROS_MEMORY_DISTILL is False
    assert resolved.OUROBOROS_LOCALE == instance.OUROBOROS_LOCALE
    assert resolved.OUROBOROS_MODEL_FALLBACK == instance.OUROBOROS_MODEL_FALLBACK
    assert resolved.OUROBOROS_MODEL_LIGHT == instance.OUROBOROS_MODEL_LIGHT
    assert resolved.OUROBOROS_TIMEZONE == instance.OUROBOROS_TIMEZONE


def test_the_aggregate_without_overrides_carries_the_instance_values_typed():
    instance = load_settings()
    resolved = _run(_user_settings(StubUserSettingsStore()).user_settings(OWNER, "main"))
    assert resolved.OUROBOROS_EFFORT_TASK == instance.OUROBOROS_EFFORT_TASK
    assert isinstance(resolved.OUROBOROS_EFFORT_TASK, str)
    assert resolved.OUROBOROS_MEMORY_DISTILL is instance.OUROBOROS_MEMORY_DISTILL
    assert isinstance(resolved.OUROBOROS_MEMORY_DISTILL, bool)
    for key in ("LOCALE", "MODEL", "MODEL_FALLBACK", "MODEL_LIGHT", "TIMEZONE"):
        assert getattr(resolved, f"OUROBOROS_{key}") == getattr(instance, f"OUROBOROS_{key}")
    assert list(instance.OUROBOROS_USER_ENABLED_SKILL_IDS) == resolved.OUROBOROS_USER_ENABLED_SKILL_IDS


def test_the_aggregate_reads_the_pair_in_one_statement():
    async_store = StubAsyncUserSettingsStore()
    settings = UserSettings(
        load_settings(), StubUserSettingsStore(), async_store, NoUserSettingsWrites(), NullCache(), AsyncNullCache()
    )
    _run(settings.user_settings(OWNER, "main"))
    assert async_store.reads == 1


def test_the_aggregate_resolves_the_same_values_the_accessors_resolve():
    instance = load_settings()
    store = StubUserSettingsStore(
        {
            "OUROBOROS_EFFORT_TASK": "high",
            "OUROBOROS_MODEL": "gigachat-b2bank::GigaChat-Max",
            "OUROBOROS_MEMORY_DISTILL": "false",
            "OUROBOROS_TIMEZONE": "",
        }
    )
    settings = _user_settings(store)
    resolved = _run(settings.user_settings(OWNER, "main"))
    effort = settings.OUROBOROS_EFFORT_TASK(_scope())
    model = settings.OUROBOROS_MODEL(_scope())
    distill = settings.OUROBOROS_MEMORY_DISTILL(_scope())
    assert effort == resolved.OUROBOROS_EFFORT_TASK
    assert model == resolved.OUROBOROS_MODEL
    assert resolved.OUROBOROS_MEMORY_DISTILL is distill
    assert resolved.OUROBOROS_TIMEZONE == instance.OUROBOROS_TIMEZONE


def test_an_effort_override_the_environment_would_refuse_resolves_to_the_instance_value():
    instance = load_settings()
    resolved = _run(
        _user_settings(StubUserSettingsStore({"OUROBOROS_EFFORT_TASK": "banana"})).user_settings(OWNER, "main")
    )
    assert resolved.OUROBOROS_EFFORT_TASK == instance.OUROBOROS_EFFORT_TASK


def test_the_aggregate_ignores_rows_outside_the_declaration():
    instance = load_settings()
    resolved = _run(
        _user_settings(StubUserSettingsStore({"OUROBOROS_RETIRED_KEY": "whatever"})).user_settings(OWNER, "main")
    )
    assert resolved.OUROBOROS_MODEL == instance.OUROBOROS_MODEL
    assert resolved.OUROBOROS_TIMEZONE == instance.OUROBOROS_TIMEZONE


def test_the_aggregate_is_a_frozen_snapshot():
    resolved = _run(_user_settings(StubUserSettingsStore()).user_settings(OWNER, "main"))
    with pytest.raises(ValidationError):
        resolved.OUROBOROS_TIMEZONE = "Asia/Tokyo"


def test_the_aggregate_reads_one_pair_and_never_another():
    settings = _user_settings(
        StubUserSettingsStore(
            by_pair={
                (OWNER, "main"): {"OUROBOROS_MODEL": "mine", "OUROBOROS_TIMEZONE": "Asia/Tokyo"},
                (OWNER, "sub-1"): {"OUROBOROS_MODEL": "other-agent"},
                (OTHER_OWNER, "main"): {"OUROBOROS_MODEL": "theirs"},
            }
        )
    )
    main = _run(settings.user_settings(OWNER, "main"))
    sub = _run(settings.user_settings(OWNER, "sub-1"))
    theirs = _run(settings.user_settings(OTHER_OWNER, "main"))
    unknown = _run(settings.user_settings(OWNER, "sub-2"))
    instance = load_settings()
    assert main.OUROBOROS_MODEL == "mine"
    assert main.OUROBOROS_TIMEZONE == "Asia/Tokyo"
    assert sub.OUROBOROS_MODEL == "other-agent"
    assert sub.OUROBOROS_TIMEZONE == instance.OUROBOROS_TIMEZONE
    assert theirs.OUROBOROS_MODEL == "theirs"
    assert unknown.OUROBOROS_MODEL == instance.OUROBOROS_MODEL


def test_an_empty_override_leaves_the_instance_value_in_force():
    settings = _user_settings(StubUserSettingsStore({"OUROBOROS_TIMEZONE": "", "OUROBOROS_MEMORY_DISTILL": ""}))
    assert settings.OUROBOROS_TIMEZONE(_scope()) == load_settings().OUROBOROS_TIMEZONE
    assert settings.OUROBOROS_MEMORY_DISTILL(_scope()) == load_settings().OUROBOROS_MEMORY_DISTILL


@pytest.mark.parametrize(("stored", "expected"), [("true", True), ("false", False), ("1", True), ("0", False)])
def test_a_bool_override_is_one_of_the_four_texts_the_reader_names(stored: str, expected: bool):
    settings = _user_settings(StubUserSettingsStore({"OUROBOROS_MEMORY_DISTILL": stored}))
    assert settings.OUROBOROS_MEMORY_DISTILL(_scope()) is expected


@pytest.mark.parametrize("stored", ["maybe", "yes", "on", "no", "off"])
def test_a_bool_override_the_reader_does_not_recognise_is_refused(stored: str):
    settings = _user_settings(StubUserSettingsStore({"OUROBOROS_MEMORY_DISTILL": stored}))
    with pytest.raises(ValueError, match="boolean setting value"):
        settings.OUROBOROS_MEMORY_DISTILL(_scope())


def test_the_aggregate_refuses_a_bool_override_it_does_not_recognise():
    settings = _user_settings(StubUserSettingsStore({"OUROBOROS_MEMORY_DISTILL": "maybe"}))
    with pytest.raises(ValueError, match="boolean setting value"):
        _run(settings.user_settings(OWNER, "main"))


def test_the_sql_store_reads_one_key_of_one_pair():
    _insert_row(OWNER, "main", "OUROBOROS_MODEL", "mine")
    _insert_row(OWNER, "main", "OUROBOROS_RETIRED_KEY", "whatever")
    _insert_row(OWNER, "sub-1", "OUROBOROS_MODEL", "other-agent")
    _insert_row(OTHER_OWNER, "main", "OUROBOROS_MODEL", "theirs")
    store = SqlUserSettingsStore()

    assert store.get_setting_value(OWNER, "main", "OUROBOROS_MODEL") == "mine"
    assert store.get_setting_value(OWNER, "sub-1", "OUROBOROS_MODEL") == "other-agent"
    assert store.get_setting_value(OTHER_OWNER, "main", "OUROBOROS_MODEL") == "theirs"
    assert store.get_setting_value(OWNER, "main", "OUROBOROS_TIMEZONE") is None
    assert store.get_setting_value(OWNER, "sub-2", "OUROBOROS_MODEL") is None


# --- the async twins --------------------------------------------------------


def _async_cache() -> AsyncInMemoryCache:
    return AsyncInMemoryCache(InMemoryCache(ttl_sec=300))


def _async_user_settings(store: StubUserSettingsStore, cache: AsyncInMemoryCache) -> UserSettings:
    """The reader as a caller on an event loop composes it: the async store and an async cache."""
    return UserSettings(
        load_settings(),
        store,
        StubAsyncUserSettingsStore(mirror=store),
        NoUserSettingsWrites(),
        NullCache(),
        cache,
    )


def test_an_async_twin_resolves_what_its_synchronous_accessor_does():
    store = StubUserSettingsStore({"OUROBOROS_MODEL": "chosen", "OUROBOROS_MEMORY_DISTILL": "false"})
    settings = _async_user_settings(store, _async_cache())

    assert _run(settings.async_OUROBOROS_MODEL(_scope())) == settings.OUROBOROS_MODEL(_scope())
    assert _run(settings.async_OUROBOROS_MEMORY_DISTILL(_scope())) is False
    assert _run(settings.async_OUROBOROS_TIMEZONE(_scope())) == load_settings().OUROBOROS_TIMEZONE


def test_an_async_twin_reads_its_key_once_and_serves_the_entry_after():
    store = StubUserSettingsStore({"OUROBOROS_MODEL": "chosen"})
    async_store = StubAsyncUserSettingsStore(mirror=store)
    settings = UserSettings(load_settings(), store, async_store, NoUserSettingsWrites(), NullCache(), _async_cache())

    for _ in range(4):
        assert _run(settings.async_OUROBOROS_MODEL(_scope())) == "chosen"

    assert async_store.reads == 1, "an entry the surface drops, not a read per call"


def test_an_async_twin_refuses_a_bool_override_it_does_not_recognise():
    settings = _async_user_settings(StubUserSettingsStore({"OUROBOROS_MEMORY_DISTILL": "maybe"}), _async_cache())

    with pytest.raises(ValueError, match="boolean setting value"):
        _run(settings.async_OUROBOROS_MEMORY_DISTILL(_scope()))


def test_an_async_twin_serves_no_other_pair_from_one_entry():
    store = StubUserSettingsStore(
        by_pair={(OWNER, "main"): {"OUROBOROS_MODEL": "mine"}, (OTHER_OWNER, "main"): {"OUROBOROS_MODEL": "theirs"}}
    )
    settings = _async_user_settings(store, _async_cache())

    assert _run(settings.async_OUROBOROS_MODEL(_scope(OWNER))) == "mine"
    assert _run(settings.async_OUROBOROS_MODEL(_scope(OTHER_OWNER))) == "theirs"
