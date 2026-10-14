"""The runtime state row, spend and audit belong to an owner, not to the deployment.

One row keyed by a tenant constant made one person's session, spend and history everybody's
the moment a second owner existed — with no parallelism at all, two owners taking turns was
enough. These pin the four halves of the fix: the row is per owner, the outbound door takes
its identity from the message, spend and audit say whose they are, and no lock is left
beside the writes.
"""

from __future__ import annotations

import pathlib

import pytest

from new_core.identity import OwnerScope, SurfaceId
from ouroboros.agent_events.interfaces import LlmUsageReported
from ouroboros.storage.runtime_models import LlmUsageRecord, OwnerRuntimeState
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.infra.repositories.owner_state.repo import SqlOwnerStateRepository
from supervisor.infra.repositories.usage.repo import SqlUsageRepository
from supervisor.state import RuntimeStateRepository, configured_owner_state_scope
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.conversations import make_sql_runtime_persistence
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.supervisor_owner_state import sql_owner_state_repository
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader
from tests.helpers.usage import sql_usage_repository


def _other_owner(chat_id: str = "9") -> OwnerScope:
    return OwnerScope(
        owner_id="another-owner",
        agent_id="another-agent",
        surface_id=SurfaceId("browser"),
        chat_id=chat_id,
    )


def _repo(
    tmp_path: pathlib.Path, owners: SqlOwnerStateRepository, scope: OwnerScope, usage: SqlUsageRepository
) -> RuntimeStateRepository:
    return RuntimeStateRepository(
        tmp_path,
        owner_state_repository=owners,
        scope=scope,
        usage_repository=usage,
        audit_event_repository=sql_audit_event_repository(),
    )


# --- the row is one owner's --------------------------------------------------


def test_two_owners_hold_separate_runtime_state(tmp_path: pathlib.Path) -> None:
    owners = sql_owner_state_repository()
    usage = sql_usage_repository()
    mine = _repo(tmp_path, owners, TEST_SCOPE, usage)
    theirs = _repo(tmp_path, owners, _other_owner(), usage)

    mine.save(OwnerRuntimeState.model_validate({"session_id": "mine", "owner_id": 1}))
    theirs.save(OwnerRuntimeState.model_validate({"session_id": "theirs", "owner_id": 9}))

    assert mine.load().session_id == "mine"
    assert theirs.load().session_id == "theirs"
    assert (mine.load().owner_id, theirs.load().owner_id) == (1, 9)


def test_a_state_read_names_the_owner_it_wants(tmp_path: pathlib.Path) -> None:
    """No default: a reader with no owner in hand has to say which owner it means."""
    owners = sql_owner_state_repository()
    usage = sql_usage_repository()

    with pytest.raises(TypeError):
        owners.load()  # type: ignore[call-arg]

    with pytest.raises(TypeError):
        RuntimeStateRepository(
            tmp_path,
            owner_state_repository=owners,
            usage_repository=usage,
            audit_event_repository=sql_audit_event_repository(),
        )  # type: ignore[call-arg]

    # And the widening an author-less caller uses is spelled out rather than defaulted.
    assert owners.load(configured_owner_state_scope()) is None


# --- spend accumulates, per owner --------------------------------------------


def _usage(scope: OwnerScope, tokens: int, *, usage_id: str, session_id: str = "") -> LlmUsageRecord:
    return LlmUsageRecord(
        usage_id=usage_id,
        owner_id=scope.owner_id,
        agent_id=scope.agent_id,
        prompt_tokens=tokens,
        completion_tokens=tokens,
        created_at="2026-01-01T00:00:00+00:00",
        metadata={"session_id": session_id} if session_id else {},
    )


def test_spend_recorded_by_two_handles_for_one_owner_sums(tmp_path: pathlib.Path) -> None:
    """There is nothing to read, add and write back: the counter is the table's own sum."""
    from infra.runtime_database import create_runtime_engine

    store = runtime_persistence.initialize()
    elsewhere = make_sql_runtime_persistence(create_runtime_engine(store.db_urls[0]), db_url=store.db_urls[0])

    sql_usage_repository(store.engine).record_usage(_usage(TEST_SCOPE, 10, usage_id="a"))
    sql_usage_repository(elsewhere.engine).record_usage(_usage(TEST_SCOPE, 5, usage_id="b"))

    totals = sql_usage_repository(store.engine).sum_usage_stats(scope=TEST_SCOPE)
    assert totals["spent_calls"] == 2
    assert totals["spent_tokens_prompt"] == 15


def test_the_state_repository_counters_are_that_owners_own(tmp_path: pathlib.Path) -> None:
    """The row reports the spend of its own owner's session, not of everybody's."""
    owners = sql_owner_state_repository()
    usage = sql_usage_repository()
    mine = _repo(tmp_path, owners, TEST_SCOPE, usage)
    theirs = _repo(tmp_path, owners, _other_owner(), usage)
    usage.record_usage(_usage(TEST_SCOPE, 10, usage_id="mine", session_id=mine.session_id()))
    usage.record_usage(_usage(_other_owner(), 99, usage_id="theirs", session_id=theirs.session_id()))

    assert mine.load().spent_tokens_prompt == 10
    assert theirs.load().spent_tokens_prompt == 99


# --- attribution -------------------------------------------------------------


def test_a_model_call_records_the_owner_it_ran_under(tmp_path: pathlib.Path) -> None:
    from supervisor.events import _handle_llm_usage

    ctx = _UsageCtx(tmp_path)

    _handle_llm_usage(
        LlmUsageReported(
            scope=_other_owner(),
            task_id="t1",
            model="m",
            provider="p",
            prompt_tokens=7,
            completion_tokens=1,
            usage={"prompt_tokens": 7, "completion_tokens": 1},
        ),
        ctx,
    )

    rows = ctx.usage_repository.sum_usage_stats(scope=_other_owner())
    assert rows["spent_calls"] == 1
    assert ctx.usage_repository.sum_usage_stats(scope=TEST_SCOPE)["spent_calls"] == 0


def test_a_model_call_with_no_owner_is_the_instances_own(tmp_path: pathlib.Path) -> None:
    """The instance's own diagnostic call is nobody's spend, and the producer says so.

    The scope is required on the message, so the fallback is minted where the call was
    made rather than guessed by a consumer that did not make it.
    """
    from new_core.identity import ServiceJob
    from ouroboros.contracts.owner_scope import service_identity
    from ouroboros.observability.pricing import emit_llm_usage_event
    from supervisor.events import _handle_llm_usage
    from tests.helpers.agent_events import RecordingAgentEvents

    reported = RecordingAgentEvents()
    emit_llm_usage_event(reported, "", "m", {"prompt_tokens": 3}, scope=None)
    ctx = _UsageCtx(tmp_path)
    _handle_llm_usage(reported.published[0], ctx)

    instance = service_identity(job=ServiceJob.RUNTIME_LOG).as_scope()
    assert ctx.usage_repository.sum_usage_stats(scope=instance)["spent_calls"] == 1


def test_an_audit_event_records_the_actor(tmp_path: pathlib.Path) -> None:
    from ouroboros.observability.event_record import record_event

    store = runtime_persistence.initialize()

    record_event(
        "events",
        "thing_happened",
        {"task_id": "t1"},
        scope=_other_owner(),
        audit_event_repository=sql_audit_event_repository(),
    )

    theirs = sql_audit_event_repository(store.engine).list_audit_events(
        scope=_other_owner(), event_type="thing_happened"
    )
    assert [event.event_type for event in theirs] == ["thing_happened"]
    assert (
        sql_audit_event_repository(store.engine).list_audit_events(scope=TEST_SCOPE, event_type="thing_happened") == []
    )


class _UsageCtx:
    """Just enough supervisor context for the usage handler."""

    def __init__(self, drive_root: pathlib.Path) -> None:
        self.DRIVE_ROOT = drive_root
        self.usage_repository = sql_usage_repository()

    def update_usage_from_llm(self, _usage: dict, **_kwargs) -> None:
        return None

    def load_state(self) -> OwnerRuntimeState:
        return OwnerRuntimeState()


# --- a status report answers about the asker ---------------------------------


def test_status_reports_the_askers_row_not_the_configured_owners(tmp_path: pathlib.Path, monkeypatch) -> None:
    """``/status`` is an ordinary slash command any owner can send."""
    from supervisor import state as state_module

    owners = sql_owner_state_repository()
    usage = sql_usage_repository()
    alice = OwnerScope(owner_id="alice", agent_id="main", surface_id=SurfaceId("browser"), chat_id="1")
    bob = OwnerScope(owner_id="bob", agent_id="main", surface_id=SurfaceId("browser"), chat_id="2")
    instance_repo = _repo(tmp_path, owners, configured_owner_state_scope(), usage)
    monkeypatch.setattr(state_module, "get_state_repository", lambda drive_root=None: instance_repo)
    for scope, session in ((alice, "session-alice"), (bob, "session-bob")):
        repo = instance_repo.for_scope(scope)
        row = repo.load()
        row.session_id = session
        # The row's own ``owner_id`` is the transport id of whoever claimed the instance,
        # not the scope axis; what matters here is that the row read is the asker's.
        row.owner_id = f"{scope.owner_id}-transport-id"
        repo.save(row)
    usage.record_usage(
        LlmUsageRecord(
            owner_id=bob.owner_id,
            agent_id=bob.agent_id,
            session_id="session-bob",
            model="provider/model-a",
            prompt_tokens=1000,
            completion_tokens=200,
        )
    )

    report = state_module.status_text(
        600,
        1800,
        scope=alice,
        tool_confirmation_reader=sql_tool_confirmation_reader(),
    )

    assert "owner_id: alice-transport-id" in report
    assert "session_id: session-alice" in report
    assert "session-bob" not in report
    assert "bob-transport-id" not in report
    # bob's spend is bob's. The model breakdown stays deployment-wide until the audit
    # rows carry their owner (``attribute-runtime-events-to-owner``), so it is not
    # asserted here.
    assert "spent_calls: 0" in report


def test_a_usage_row_carries_the_producing_owners_session(tmp_path: pathlib.Path, monkeypatch) -> None:
    from supervisor import events as events_module, state as state_module

    owners = sql_owner_state_repository()
    usage = sql_usage_repository()
    alice = OwnerScope(owner_id="alice", agent_id="main", surface_id=SurfaceId("browser"), chat_id="1")
    instance_repo = _repo(tmp_path, owners, configured_owner_state_scope(), usage)
    monkeypatch.setattr(state_module, "get_state_repository", lambda drive_root=None: instance_repo)
    instance_state = instance_repo.load()
    instance_state.session_id = "session-of-the-instance"
    instance_repo.save(instance_state)
    alice_repo = instance_repo.for_scope(alice)
    alice_state = alice_repo.load()
    alice_state.session_id = "session-alice"
    alice_repo.save(alice_state)

    class _Ctx:
        DRIVE_ROOT = tmp_path
        state_repo = instance_repo
        usage_repository = usage

        def load_state(self) -> OwnerRuntimeState:
            return instance_repo.load()

        def update_usage_from_llm(self, _usage: dict, **_kwargs) -> None:
            return None

    events_module._handle_llm_usage(
        LlmUsageReported(
            scope=alice,
            model="provider/model-a",
            prompt_tokens=10,
            completion_tokens=2,
            cached_tokens=0,
            cache_write_tokens=0,
            usage={"prompt_tokens": 10, "completion_tokens": 2},
        ),
        _Ctx(),
    )

    stats = usage.sum_usage_stats(session_id="session-alice", scope=alice)
    assert int(stats.get("spent_calls") or 0) == 1
    assert int(usage.sum_usage_stats(session_id="session-of-the-instance", scope=alice).get("spent_calls") or 0) == 0
