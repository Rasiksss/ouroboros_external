from __future__ import annotations

import asyncio

from sqlalchemy import create_engine, text

from ouroboros.contracts.owner_scope import SINGLE_AGENT_ID


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _rows(runtime_db_url: str, sql: str, **params):
    engine = create_engine(runtime_db_url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params).mappings().all()
    finally:
        engine.dispose()


def test_a_previously_unseen_owner_receives_a_default_agent(registry):
    agent = _run(registry.get_or_create_default_agent("owner-a"))
    assert agent.owner_id == "owner-a"
    assert agent.agent_id == SINGLE_AGENT_ID
    assert agent.is_active


def test_a_second_request_returns_the_existing_default_agent(registry, runtime_db_url):
    first = _run(registry.get_or_create_default_agent("owner-a"))
    second = _run(registry.get_or_create_default_agent("owner-a"))
    assert first == second
    assert len(_rows(runtime_db_url, "SELECT agent_id FROM agents WHERE owner_id = :o", o="owner-a")) == 1


def test_concurrent_first_requests_produce_one_default_agent(registry, runtime_db_url):

    async def _race():
        return await asyncio.gather(*(registry.get_or_create_default_agent("owner-a") for _ in range(8)))

    agents = _run(_race())
    assert {agent.agent_id for agent in agents} == {SINGLE_AGENT_ID}
    assert len(set(agents)) == 1, "every caller received the same agent"
    assert len(_rows(runtime_db_url, "SELECT agent_id FROM agents WHERE owner_id = :o", o="owner-a")) == 1


def test_a_created_sub_agent_appears_in_its_owners_listing(registry):
    _run(registry.get_or_create_default_agent("owner-a"))
    created = _run(registry.create_agent("owner-a", "researcher", "reads long documents"))
    assert created.is_active
    assert created.agent_id != SINGLE_AGENT_ID
    assert created.name == "researcher"
    listed = _run(registry.list_agents("owner-a"))
    assert [agent.agent_id for agent in listed] == [SINGLE_AGENT_ID, created.agent_id]
    assert created in listed


def test_two_created_sub_agents_do_not_share_an_id(registry):
    first = _run(registry.create_agent("owner-a", "one", ""))
    second = _run(registry.create_agent("owner-a", "two", ""))
    assert first.agent_id != second.agent_id


def test_a_listing_holds_only_its_own_owners_agents(registry):
    _run(registry.get_or_create_default_agent("owner-a"))
    _run(registry.get_or_create_default_agent("owner-b"))
    theirs = _run(registry.create_agent("owner-b", "theirs", ""))
    listed = _run(registry.list_agents("owner-a"))
    assert [agent.owner_id for agent in listed] == ["owner-a"]
    assert theirs.agent_id not in {agent.agent_id for agent in listed}


def test_an_unknown_agent_reads_as_absence(registry):
    _run(registry.get_or_create_default_agent("owner-a"))
    assert _run(registry.get_agent("owner-a", "no-such-agent")) is None


def test_another_owners_agent_reads_as_absence(registry):
    theirs = _run(registry.create_agent("owner-b", "theirs", ""))
    assert _run(registry.get_agent("owner-a", theirs.agent_id)) is None
    assert _run(registry.get_agent("owner-b", theirs.agent_id)) == theirs


def test_deactivation_preserves_the_row(registry, runtime_db_url):
    created = _run(registry.create_agent("owner-a", "researcher", ""))
    assert _run(registry.deactivate_agent("owner-a", created.agent_id)) is True
    rows = _rows(
        runtime_db_url,
        "SELECT is_active FROM agents WHERE owner_id = :o AND agent_id = :a",
        o="owner-a",
        a=created.agent_id,
    )
    assert len(rows) == 1
    assert not rows[0]["is_active"]
    assert not _run(registry.get_agent("owner-a", created.agent_id)).is_active


def test_deactivating_an_unknown_agent_states_that_nothing_was_updated(registry, runtime_db_url):
    assert _run(registry.deactivate_agent("owner-a", "no-such-agent")) is False
    assert _rows(runtime_db_url, "SELECT agent_id FROM agents") == []


def test_deactivating_another_owners_agent_leaves_it_active(registry):
    theirs = _run(registry.create_agent("owner-b", "theirs", ""))
    assert _run(registry.deactivate_agent("owner-a", theirs.agent_id)) is False
    assert _run(registry.get_agent("owner-b", theirs.agent_id)).is_active


def test_the_default_agent_is_deactivated_like_any_other(registry, runtime_db_url):
    """The reserved id names the default; it does not rank the row behind it."""
    default = _run(registry.get_or_create_default_agent("owner-a"))
    assert _run(registry.deactivate_agent("owner-a", default.agent_id)) is True
    rows = _rows(
        runtime_db_url,
        "SELECT is_active FROM agents WHERE owner_id = :o AND agent_id = :a",
        o="owner-a",
        a=SINGLE_AGENT_ID,
    )
    assert len(rows) == 1
    assert not rows[0]["is_active"]
    assert not _run(registry.get_agent("owner-a", default.agent_id)).is_active


def test_the_reserved_id_gets_no_check_of_its_own(registry, runtime_db_url):
    """No id is inspected before the row is touched, so an absent one writes nothing."""
    assert _run(registry.deactivate_agent("owner-a", SINGLE_AGENT_ID)) is False
    assert _rows(runtime_db_url, "SELECT agent_id FROM agents") == []


def test_resolution_does_not_revive_a_deactivated_default_agent(registry, runtime_db_url):
    """An owner who retired that agent retired it; a silent revival is the flip failing."""
    _run(registry.deactivate_agent("owner-a", _run(registry.get_or_create_default_agent("owner-a")).agent_id))
    assert not _run(registry.get_or_create_default_agent("owner-a")).is_active
    assert len(_rows(runtime_db_url, "SELECT agent_id FROM agents WHERE owner_id = :o", o="owner-a")) == 1
