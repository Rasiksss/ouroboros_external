"""The package's own asynchronous repositories over the runtime database."""

from __future__ import annotations

import asyncio
import base64
import threading

import pytest
from sqlalchemy import text

from background_memory.infra.repositories import (
    SqlAuditRepo,
    SqlChatHistoryRepo,
    SqlConsolidationLeaseRepo,
    SqlContextSummaryRepo,
    SqlMemoryHeaderRepo,
    SqlTaskRepo,
)
from infra.async_engine import create_async_runtime_engine
from new_core.identity import OwnerScope
from ouroboros.storage.core_memory.memory import get_memory
from ouroboros.storage.message_field_crypto import (
    FieldKeyUnavailable,
    encrypt_field,
    reset_encryption_cache_for_tests,
)
from ouroboros.storage.runtime_models import AuditEvent, ContextSummary, ConversationMessage, TaskRequest
from ouroboros.utils import utc_now_iso
from tests.helpers.background_memory_db import memory_database
from tests.helpers.conversations import sql_conversation_repository
from tests.helpers.shared import TEST_SCOPE

KEY = base64.b64encode(b"k" * 32).decode("ascii")


@pytest.fixture
def db():
    yield from memory_database()


def _append(store, index: int, *, content: str = "hello", actor_type: str = "owner") -> str:
    message_id = f"message-{index:04d}"
    sql_conversation_repository(store.engine).append_message(
        ConversationMessage(
            message_id=message_id,
            dedup_key=message_id,
            owner_id=TEST_SCOPE.owner_id,
            agent_id=TEST_SCOPE.agent_id,
            chat_id=TEST_SCOPE.chat_id,
            session_id="session",
            source="dialogs",
            actor_type=actor_type,
            channel="chat",
            content=content,
            created_at=f"2026-09-19T00:{index:02d}:00+00:00",
        ),
        scope=TEST_SCOPE,
    )
    return message_id


def _store_ciphertext(store, message_id: str, plaintext: str) -> None:
    """Put a sealed value in the row, with no payload to fall back on."""
    with store.engine.connect() as conn:
        conn.execute(
            text("UPDATE messages SET content=:content, payload_json='' WHERE message_id=:message_id"),
            {"content": encrypt_field(plaintext), "message_id": message_id},
        )
        conn.commit()


@pytest.fixture
def encryption(monkeypatch):
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_KEY", KEY)
    reset_encryption_cache_for_tests()
    yield monkeypatch
    reset_encryption_cache_for_tests()


# --- encryption ---------------------------------------------------------------


def test_a_stored_message_decrypts_on_read(db, encryption):
    _append(db.store, 1, content="placeholder")
    _store_ciphertext(db.store, "message-0001", "private text")

    backlog = db.run(SqlChatHistoryRepo(db.engine).backlog(TEST_SCOPE, limit=10))

    assert [message.content for message in backlog.messages] == ["private text"]


def test_a_message_whose_key_is_unavailable_refuses_the_whole_read(db, encryption):
    """The declared path: the read raises, so no run summarizes around a hole and commits past it."""
    _append(db.store, 1, content="readable")
    _append(db.store, 2, content="placeholder")
    _store_ciphertext(db.store, "message-0002", "unreadable")
    encryption.delenv("OUROBOROS_CHAT_ENCRYPTION_KEY", raising=False)
    reset_encryption_cache_for_tests()

    with pytest.raises(FieldKeyUnavailable):
        db.run(SqlChatHistoryRepo(db.engine).backlog(TEST_SCOPE, limit=10))


# --- prompt headers -----------------------------------------------------------


def test_the_prompt_header_listing_omits_the_excluded_statuses(db):
    """The status filter lives on the domain service; a lifted adapter query loses it."""
    memory = get_memory()
    memory.save(TEST_SCOPE, "live", "project", "kept", "body")
    memory.save(TEST_SCOPE, "gone", "project", "archived", "body")
    with db.store.engine.connect() as conn:
        conn.execute(text("UPDATE memory SET status='archived' WHERE name='gone'"))
        conn.commit()

    names = [header.name for header in db.run(SqlMemoryHeaderRepo(db.engine).list_prompt_headers(TEST_SCOPE))]

    assert names == [header.name for header in memory.list_prompt_headers(TEST_SCOPE)]
    assert ("live" in names, "gone" in names) == (True, False)


# --- the lease ----------------------------------------------------------------


def test_racing_holders_leave_one_consolidation_lease(db):
    """Real threads, separate engines, one ownership pair: the statement decides.

    Moved here with the statements from the runtime store's suite. Every thread's exception
    is carried back out and asserted on: a read-then-write acquire passes the "one winner"
    half by accident — the losers read the winner's committed row — and fails only here,
    where the conflict its racing inserts raise would otherwise die inside a thread nobody
    joins for a result. Several rounds, because the interleaving that makes two threads read
    before either writes is likely rather than certain.
    """
    url = db.store.engine.url.render_as_string(hide_password=False)
    failures: list[BaseException] = []
    lock = threading.Lock()

    def _race(scope) -> list[str]:
        start = threading.Barrier(4)
        won: list[str] = []

        def _acquire(holder: str) -> None:
            loop = asyncio.new_event_loop()
            engine = create_async_runtime_engine(url)
            repo = SqlConsolidationLeaseRepo(engine)
            try:
                start.wait(timeout=30)
                taken = loop.run_until_complete(
                    repo.acquire(
                        scope, holder_id=holder, now_iso=utc_now_iso(), expires_at_iso="2999-01-01T00:00:00+00:00"
                    )
                )
                if taken:
                    with lock:
                        won.append(holder)
            except BaseException as exc:  # a lost thread exception is the bug this test looks for
                with lock:
                    failures.append(exc)
            finally:
                loop.run_until_complete(engine.dispose())
                loop.close()

        threads = [threading.Thread(target=_acquire, args=(f"holder-{index}",)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        return won

    for round_index in range(5):
        # A pair of its own per round: the race is only a race on a row that does not exist
        # yet, and one round would leave the next four deciding against a stored winner.
        scope = OwnerScope(
            owner_id=f"racer-{round_index}",
            agent_id=TEST_SCOPE.agent_id,
            surface_id=TEST_SCOPE.surface_id,
            chat_id=TEST_SCOPE.chat_id,
        )

        won = _race(scope)

        assert failures == [], f"an acquire raised instead of deciding: {failures}"
        assert won == [db.run(db.leases.read(scope)).holder_id]


def test_a_renewal_and_a_release_by_a_holder_the_row_does_not_name_apply_to_nothing(db):
    repo = SqlConsolidationLeaseRepo(db.engine)
    now, expiry = utc_now_iso(), "2999-01-01T00:00:00+00:00"
    db.run(repo.acquire(TEST_SCOPE, holder_id="holder-a", now_iso=now, expires_at_iso=expiry))

    assert not db.run(repo.renew(TEST_SCOPE, holder_id="holder-nobody", now_iso=now, expires_at_iso=expiry))
    assert not db.run(repo.release(TEST_SCOPE, holder_id="holder-nobody", outcome="failed", now_iso=now))
    assert db.run(repo.read(TEST_SCOPE)).consecutive_failures == 0


def test_a_commit_that_outlived_its_lease_still_resets_the_pairs_throttle(db):
    """A displaced run that still won the cursor swap has proven consolidation works.

    Its release frees nothing — the lease is the peer's to free — but the throttle it was
    fighting is reset all the same, or a pair would stay declined after a success.
    """
    repo = SqlConsolidationLeaseRepo(db.engine)
    now, expiry = utc_now_iso(), "2999-01-01T00:00:00+00:00"
    db.run(repo.acquire(TEST_SCOPE, holder_id="holder-a", now_iso=now, expires_at_iso=expiry))
    db.run(repo.release(TEST_SCOPE, holder_id="holder-a", outcome="failed", now_iso=now))
    db.run(repo.acquire(TEST_SCOPE, holder_id="holder-b", now_iso=now, expires_at_iso=expiry))
    assert db.run(repo.read(TEST_SCOPE)).consecutive_failures == 1

    assert db.run(repo.release(TEST_SCOPE, holder_id="holder-a", outcome="completed", now_iso=now))

    lease = db.run(repo.read(TEST_SCOPE))
    assert (lease.holder_id, lease.consecutive_failures, lease.last_failure_at) == ("holder-b", 0, "")


def test_a_renewal_by_another_holder_matches_no_row(db):
    repo = SqlConsolidationLeaseRepo(db.engine)
    now, expiry = utc_now_iso(), "2099-01-01T00:00:00+00:00"
    assert db.run(repo.acquire(TEST_SCOPE, holder_id="mine", now_iso=now, expires_at_iso=expiry))

    assert not db.run(repo.renew(TEST_SCOPE, holder_id="theirs", now_iso=now, expires_at_iso=expiry))
    assert db.run(repo.renew(TEST_SCOPE, holder_id="mine", now_iso=now, expires_at_iso=expiry))


def test_a_failed_release_frees_the_pair_and_advances_its_throttle(db):
    repo = SqlConsolidationLeaseRepo(db.engine)
    now = utc_now_iso()
    db.run(repo.acquire(TEST_SCOPE, holder_id="mine", now_iso=now, expires_at_iso="2099-01-01T00:00:00+00:00"))

    assert db.run(repo.release(TEST_SCOPE, holder_id="mine", outcome="failed", now_iso=now))

    lease = db.run(repo.read(TEST_SCOPE))
    assert (lease.holder_id, lease.consecutive_failures) == ("", 1)


def test_a_completed_release_deletes_the_row(db):
    repo = SqlConsolidationLeaseRepo(db.engine)
    now = utc_now_iso()
    db.run(repo.acquire(TEST_SCOPE, holder_id="mine", now_iso=now, expires_at_iso="2099-01-01T00:00:00+00:00"))

    assert db.run(repo.release(TEST_SCOPE, holder_id="mine", outcome="completed", now_iso=now))

    assert db.run(repo.read(TEST_SCOPE)) is None


# --- the committed summary ----------------------------------------------------


def _summary(*, cursor: str, body: str) -> ContextSummary:
    return ContextSummary(
        owner_id=TEST_SCOPE.owner_id,
        agent_id=TEST_SCOPE.agent_id,
        chat_id=TEST_SCOPE.chat_id,
        source_offsets_ref=cursor,
        summary_ref=body,
    )


def test_the_compare_and_swap_refuses_a_stale_cursor(db):
    repo = SqlContextSummaryRepo(db.engine)
    assert db.run(repo.commit(_summary(cursor="message-0001", body="first"), expected_cursor=""))

    assert not db.run(repo.commit(_summary(cursor="message-0002", body="second"), expected_cursor="stale"))

    assert db.run(repo.read(TEST_SCOPE)).summary_ref == "first"


def test_the_swap_lands_on_the_cursor_the_run_started_from(db):
    repo = SqlContextSummaryRepo(db.engine)
    db.run(repo.commit(_summary(cursor="message-0001", body="first"), expected_cursor=""))

    assert db.run(repo.commit(_summary(cursor="message-0002", body="second"), expected_cursor="message-0001"))

    assert db.run(repo.read(TEST_SCOPE)).summary_ref == "second"


def test_a_committed_summary_is_sealed_at_rest(db, encryption):
    repo = SqlContextSummaryRepo(db.engine)

    assert db.run(repo.commit(_summary(cursor="message-0001", body="private summary"), expected_cursor=""))

    with db.store.engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT summary_ref, metadata_json, payload_json FROM context_summaries "
                "WHERE owner_id=:owner_id AND agent_id=:agent_id AND chat_id=:chat_id"
            ),
            {
                "owner_id": TEST_SCOPE.owner_id,
                "agent_id": TEST_SCOPE.agent_id,
                "chat_id": TEST_SCOPE.chat_id,
            },
        ).fetchone()

    assert row is not None and all(str(value).startswith("enc:v1:") for value in row)
    assert db.run(repo.read(TEST_SCOPE)).summary_ref == "private summary"


# --- chat history -------------------------------------------------------------


def test_a_missing_anchor_recovers_by_listing_from_the_beginning(db):
    _append(db.store, 1)
    _append(db.store, 2)

    backlog = db.run(SqlChatHistoryRepo(db.engine).backlog(TEST_SCOPE, limit=10, after_message_id="message-9999"))

    assert (backlog.anchor_missing, [m.message_id for m in backlog.messages]) == (
        True,
        ["message-0001", "message-0002"],
    )


def test_a_present_anchor_lists_what_follows_it(db):
    _append(db.store, 1)
    _append(db.store, 2)

    backlog = db.run(SqlChatHistoryRepo(db.engine).backlog(TEST_SCOPE, limit=10, after_message_id="message-0001"))

    assert (backlog.anchor_missing, [m.message_id for m in backlog.messages]) == (False, ["message-0002"])


def test_the_tail_watermark_is_the_newest_visible_message(db):
    _append(db.store, 1)
    _append(db.store, 2)
    repo = SqlChatHistoryRepo(db.engine)

    assert (db.run(repo.tail_message_id(TEST_SCOPE)), db.run(repo.message_exists("message-0002"))) == (
        "message-0002",
        True,
    )
    assert not db.run(repo.message_exists("message-9999"))


# --- the audit claim ----------------------------------------------------------


def _claim(job_id: str) -> AuditEvent:
    return AuditEvent(
        event_id=f"memory-distillation:{TEST_SCOPE.owner_id}:{job_id}",
        owner_id=TEST_SCOPE.owner_id,
        agent_id=TEST_SCOPE.agent_id,
        event_type="memory_distillation_claim",
        log_stream="memory",
        resource_type="task",
        resource_id=job_id,
    )


def test_only_the_first_claim_of_a_job_is_taken(db):
    repo = SqlAuditRepo(db.engine)

    assert (db.run(repo.claim(_claim("job-1"))), db.run(repo.claim(_claim("job-1")))) == (True, False)


# --- the task reads -----------------------------------------------------------


def test_foreground_pressure_reads_every_owners_open_tasks(db):
    db.store.tasks_repo.create_task(
        TaskRequest(id="task-1", type="task", text="work", chat_id=TEST_SCOPE.chat_id, scope=TEST_SCOPE.to_dict())
    )
    repo = SqlTaskRepo(db.engine)

    assert db.run(repo.list_tasks(statuses=["queued", "running"], limit=1)) == ["task-1"]
    assert db.run(repo.list_tasks(statuses=["completed"], limit=1)) == []


def test_an_absent_task_row_reports_neither_detached_nor_attached(db):
    assert db.run(SqlTaskRepo(db.engine).is_detached("nobody")) is None
