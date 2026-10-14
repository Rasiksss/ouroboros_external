"""Core memory is owned by an owner *and* an agent, and by nothing else.

Every other test in the suite runs under one identity, so every one of them would still
pass if ``agent_id`` were ignored end to end. These run two identities against the real
PostgreSQL adapters and pin what the pair actually separates — and, just as deliberately,
what it does not: surface and chat are carried but never partition memory.
"""

from __future__ import annotations

import pytest

from infra.migrations.runner import upgrade_head
from new_core.identity import OwnerScope
from ouroboros.storage.core_memory.memory.domain.models import policy_for
from ouroboros.storage.core_memory.memory.infra.psql import PsqlMemory
from ouroboros.storage.core_memory.memory.interfaces.memory import IMemory
from ouroboros.storage.core_memory.scratchpad.domain.models import SCRATCHPAD_MAX_BLOCKS
from ouroboros.storage.core_memory.scratchpad.domain.service import Scratchpad
from ouroboros.storage.core_memory.scratchpad.infra.sql import SqlScratchpad

from ouroboros.storage.core_memory.memory.domain.service import Memory  # isort: skip

from tests.helpers.shared import scope_with

MAIN = OwnerScope(owner_id="global", agent_id="main", surface_id="browser", chat_id="1")
SECOND_AGENT = scope_with(MAIN, agent_id="second")
OTHER_OWNER = scope_with(MAIN, owner_id="other")


@pytest.fixture
def engine(blank_schema):
    engine = blank_schema()
    upgrade_head(engine)
    return engine


@pytest.fixture
def memory(engine) -> Memory:
    return Memory(PsqlMemory(engine))


@pytest.fixture
def pad(engine) -> Scratchpad:
    return Scratchpad(SqlScratchpad(engine))


def _names(memory: Memory, scope: OwnerScope) -> list[str]:
    return [header.name for header in memory.list_headers(scope)]


# ------------------------------------------------------------------ durable memory


def test_two_agents_of_one_owner_hold_the_same_name_separately(memory):
    """Without the agent axis the second save would overwrite the first — one agent
    would silently inherit, and then clobber, another agent's memory of the same name."""
    memory.save(MAIN, "notes", "project", "d", "main body\n")
    memory.save(SECOND_AGENT, "notes", "project", "d", "second body\n")

    assert "main body" in memory.read(MAIN, "notes")[0]
    assert "second body" in memory.read(SECOND_AGENT, "notes")[0]
    assert "second body" not in memory.read(MAIN, "notes")[0]


def test_one_agents_listing_never_shows_anothers_memories(memory):
    """The prompt is built from this listing: a leak here puts one agent's facts in
    another agent's context window."""
    memory.save(MAIN, "main_only", "project", "d", "body\n")
    memory.save(SECOND_AGENT, "second_only", "project", "d", "body\n")

    assert "main_only" in _names(memory, MAIN)
    assert "second_only" not in _names(memory, MAIN)
    assert "second_only" in _names(memory, SECOND_AGENT)
    assert "main_only" not in _names(memory, SECOND_AGENT)


def test_deleting_one_agents_memory_leaves_the_others(memory):
    """DELETE carries the same predicate as SELECT, or one agent erases another's row."""
    memory.save(MAIN, "notes", "project", "d", "main body\n")
    memory.save(SECOND_AGENT, "notes", "project", "d", "second body\n")

    assert memory.delete(MAIN, "notes") is True

    assert memory.fetch(MAIN, "notes") is None
    assert memory.fetch(SECOND_AGENT, "notes") is not None


def test_the_pair_separates_owners_too_not_just_agents(memory):
    """Proves the key is the pair rather than the agent axis doing all the work."""
    memory.save(MAIN, "notes", "project", "d", "global body\n")
    memory.save(OTHER_OWNER, "notes", "project", "d", "other body\n")

    assert "global body" in memory.read(MAIN, "notes")[0]
    assert "other body" in memory.read(OTHER_OWNER, "notes")[0]
    assert "notes" not in _names(memory, scope_with(OTHER_OWNER, agent_id="second"))


def test_surface_and_chat_do_not_partition_memory(memory):
    """Deliberate, and the reason this test exists: a fact learned in one chat has to be
    readable in the next. A refactor that keyed memory by chat would pass every other
    test in the suite and break continuity silently."""
    elsewhere = scope_with(MAIN, surface_id="dialogs", chat_id="999")

    memory.save(MAIN, "learned_here", "project", "d", "body\n")

    assert "learned_here" in _names(memory, elsewhere)
    assert "body" in memory.read(elsewhere, "learned_here")[0]


def test_capacity_pruning_counts_per_scope(memory):
    """The prune lists inside the write transaction. If that listing crossed agents, one
    agent filling a kind would archive another agent's memories to make room."""
    capacity = policy_for("user").capacity

    memory.save(SECOND_AGENT, "second_keeper", "user", "d", "body\n")
    for index in range(capacity + 3):
        memory.save(MAIN, f"main_{index:02d}", "user", "d", "body\n")

    # The prompt listing is what capacity bounds; archived rows stay in the table.
    active = [header.name for header in memory.list_prompt_headers(MAIN) if header.kind == "user"]
    assert len(active) == capacity
    assert "second_keeper" in _names(memory, SECOND_AGENT)


# ---------------------------------------------------------------------- scratchpad


def test_two_agents_keep_separate_scratchpads(pad):
    pad.append_block(MAIN, "main note")
    pad.append_block(SECOND_AGENT, "second note")

    assert [row.content for row in pad.list_blocks(MAIN)] == ["main note"]
    assert [row.content for row in pad.list_blocks(SECOND_AGENT)] == ["second note"]


def test_the_scratchpad_cap_is_enforced_per_scope(pad):
    """The eviction statement carries the ownership predicate twice — an outer UPDATE and
    an inner subselect. Widening only one of them retires another agent's oldest blocks
    every time this agent appends, and no other test would notice."""
    pad.append_block(SECOND_AGENT, "second keeper")

    for index in range(SCRATCHPAD_MAX_BLOCKS + 5):
        pad.append_block(MAIN, f"main {index}")

    assert len(pad.list_blocks(MAIN)) == SCRATCHPAD_MAX_BLOCKS
    assert [row.content for row in pad.list_blocks(SECOND_AGENT)] == ["second keeper"]


# ------------------------------------------------------------------- the port itself


def test_the_ports_take_a_scope_and_never_a_bare_owner():
    """The contract this all rests on: no port method can be called without a scope, so
    no statement can reach the database without both ownership axes bound."""
    import inspect

    for name, method in inspect.getmembers(IMemory, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        parameters = list(inspect.signature(method).parameters)
        assert parameters[1] == "scope", f"IMemory.{name} does not take the scope first: {parameters}"
        assert "owner_id" not in parameters, f"IMemory.{name} still takes a bare owner: {parameters}"
