from __future__ import annotations

import logging
import types

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from new_core.loop.interfaces import ConfirmationRound
from ouroboros.agent.task_ledger import safe_args_hash
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.utils import utc_now_iso
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.runtime_store import claim_task
from tests.helpers.shared import (
    TEST_SCOPE,
    collecting_event_sink,
    decision_for,
    execute_tool_round,
    make_static_tool_env,
    make_tool_call,
    make_tool_ctx,
    make_tool_entry,
    scope_with,
)
from tests.helpers.sql_faults import fail_sql
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader


def _broker(tmp_path, catalog, *, repeat_guard=None, task_ledger=None, reader=None, confirmation_gate=True):
    from ouroboros.agent.loop_adapters import ToolConfirmationBroker, build_tool_call_executor

    executor = build_tool_call_executor(
        catalog=catalog,
        events=collecting_event_sink(),
        task_ledger=task_ledger,
        repeat_guard=repeat_guard,
        confirmation_gate=confirmation_gate,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    return ToolConfirmationBroker(
        executor=executor,
        claim_token="",
        tool_confirmation_reader=reader if reader is not None else sql_tool_confirmation_reader(),
    )


def _claimed_task(tmp_path, scope=TEST_SCOPE):
    store = runtime_persistence.initialize()
    claim_task(store, scope=scope)
    return store


def _ask(broker, ctx, calls, *, round_idx=1, snapshot=None):
    return broker.evaluate(
        calls,
        snapshot=snapshot if snapshot is not None else {"round_idx": round_idx},
        tool_ctx=ctx,
        task_id="task1",
        round_idx=round_idx,
        active_tool_names=None,
    )


def _answer(store, request, *, decision="approved"):
    from ouroboros.utils import utc_now_iso

    sql_tool_confirmation_reader(store.engine).resolve_tool_confirmation(
        request.confirmation_id, decision=decision, now_iso=utc_now_iso()
    )
    sql_tool_confirmation_reader(store.engine).requeue_parked_task(request.task_id)
    store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a")


def test_the_question_describes_the_call_in_text_and_fields_without_its_secrets(tmp_path):
    """The owner is shown the same redaction the logs get, never the raw arguments."""
    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "sent"))
    _claimed_task(tmp_path)
    call = make_tool_call("data_write", {"file_path": "report.pdf", "token": "s3cret"})

    request = _ask(_broker(tmp_path, catalog), ctx, [call]).request

    assert "report.pdf" in request.question and "report.pdf" in request.arguments_preview
    assert "s3cret" not in request.question + request.arguments_preview
    assert (request.tool, request.arguments_truncated, request.task_id) == ("data_write", False, "task1")
    assert request.confirmation_id.startswith(request.question.split("[")[1][:8]), "the hint names this request"


@pytest.mark.parametrize(("surface", "told_what_to_type"), [("dialogs", True), ("browser", False)])
def test_only_a_chat_answered_by_typing_is_told_what_to_type(tmp_path, surface, told_what_to_type):
    catalog, _ = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "sent"))
    scope = scope_with(TEST_SCOPE, surface_id=surface)
    ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path, scope=scope)
    _claimed_task(tmp_path, scope=scope)

    request = _ask(_broker(tmp_path, catalog), ctx, [make_tool_call("data_write")]).request

    assert ("Ответьте" in request.question) is told_what_to_type


def test_a_long_call_is_previewed_rather_than_pasted_whole(tmp_path):
    """Below the log sanitizer's own per-value threshold, so this is the confirmation's cut."""
    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "sent"))
    _claimed_task(tmp_path)

    request = _ask(_broker(tmp_path, catalog), ctx, [make_tool_call("data_write", {"file_path": "x" * 2000})]).request

    assert request.arguments_truncated is True
    assert len(request.arguments_preview) < 1200, "the owner is shown a preview, not the whole call"


def test_parking_hands_the_worker_slot_back(tmp_path, caplog):
    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "sent"))
    store = _claimed_task(tmp_path)

    with caplog.at_level(logging.INFO, logger="ouroboros.agent.loop_adapters"):
        request = _ask(
            _broker(tmp_path, catalog), ctx, [make_tool_call("data_write")], snapshot={"round_idx": 7}
        ).request

    assert store.tasks_repo.get_task("task1").status == "waiting_confirmation"
    assert (
        sql_tool_confirmation_reader(store.engine).read_open_tool_confirmation(TEST_SCOPE).confirmation_id
        == request.confirmation_id
    )
    assert sql_tool_confirmation_reader(store.engine).take_parked_snapshot("task1")["round_idx"] == 7
    assert (
        f"event=tool_confirmation_requested task_id=task1 tool=data_write confirmation_id={request.confirmation_id}"
        in caplog.messages
    )


def test_a_surface_without_a_person_is_refused_instead_of_parked(tmp_path):
    """An a2a peer answers no question, so parking would wait on a reply nobody will send."""
    catalog, _ = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "sent"))
    scope = scope_with(TEST_SCOPE, surface_id="a2a")
    ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path, scope=scope)
    store = _claimed_task(tmp_path, scope=scope)

    asked = _ask(_broker(tmp_path, catalog), ctx, [make_tool_call("data_write")])

    assert (asked.request, asked.decisions) == (None, {})
    assert store.tasks_repo.get_task("task1").status == "running", "the run goes on and the call is refused"
    assert sql_tool_confirmation_reader(store.engine).read_open_tool_confirmation(scope) is None


def test_a_stored_decision_reaches_the_round_by_the_call_it_was_asked_for(tmp_path):
    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "sent"))
    store = _claimed_task(tmp_path)
    broker = _broker(tmp_path, catalog)
    calls = [make_tool_call("data_write", {"file_path": "report.pdf"})]
    _answer(store, _ask(broker, ctx, calls, round_idx=4).request)

    answered = _ask(broker, ctx, calls, round_idx=4)

    assert (answered.decisions, answered.request) == (decision_for("data_write", {"file_path": "report.pdf"}), None)
    assert store.tasks_repo.get_task("task1").status == "running", "an answered round runs instead of parking again"
    assert _ask(broker, ctx, calls, round_idx=5).decisions == {}, "the answer belongs to the round it was asked in"


def test_an_answer_is_spent_by_the_round_that_acts_on_it(tmp_path):
    """A reclaim starts the task over and reaches the same round: the call is put to the owner again."""
    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "sent"))
    store = _claimed_task(tmp_path)
    broker = _broker(tmp_path, catalog)
    calls = [make_tool_call("data_write", {"file_path": "report.pdf"})]
    _answer(store, _ask(broker, ctx, calls, round_idx=4).request)

    assert _ask(broker, ctx, calls, round_idx=4).request is None, "the answered round runs"

    assert _ask(broker, ctx, calls, round_idx=4).decisions == {}
    assert store.tasks_repo.get_task("task1").status == "waiting_confirmation", "the repeated round parks again"


@parametrize_from_dict(
    {
        "approved": {"decision": "approved", "runs": True},
        "denied": {"decision": "denied", "runs": False},
    }
)
def test_a_resumed_decision_still_applies_when_the_gate_was_turned_off(tmp_path, decision, runs):
    invoked = []
    catalog, ctx = make_static_tool_env(
        tmp_path, make_tool_entry("data_write", lambda ctx, **kw: invoked.append(1) or "written")
    )
    store = _claimed_task(tmp_path)
    calls = [make_tool_call("data_write")]
    _answer(store, _ask(_broker(tmp_path, catalog), ctx, calls, round_idx=4).request, decision=decision)

    resumed = _broker(tmp_path, catalog, confirmation_gate=False)
    assert resumed.restore(task_id="task1", tool_ctx=ctx) is not None
    answered = _ask(resumed, ctx, calls, round_idx=4)
    results = execute_tool_round(
        catalog, ctx, tmp_path, calls, collecting_event_sink(), decisions=answered.decisions, confirmation_gate=False
    )

    assert answered.request is None
    assert bool(invoked) is runs
    assert results[0]["result"].startswith("written" if runs else "⚠️ CONFIRMATION_DENIED")
    assert sql_tool_confirmation_reader(store.engine).read_tool_confirmation_decisions("task1", round_idx=4) == {}
    assert _ask(_broker(tmp_path, catalog), ctx, calls, round_idx=4).request is not None


def test_gate_off_resume_stops_if_its_decision_cannot_be_spent(tmp_path, monkeypatch):
    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "written"))
    store = _claimed_task(tmp_path)
    calls = [make_tool_call("data_write")]
    _answer(store, _ask(_broker(tmp_path, catalog), ctx, calls, round_idx=4).request)
    reader = sql_tool_confirmation_reader()
    resumed = _broker(tmp_path, catalog, confirmation_gate=False, reader=reader)
    assert resumed.restore(task_id="task1", tool_ctx=ctx) is not None
    reader.spend_round_decisions("task1", round_idx=4, now_iso=utc_now_iso())

    with pytest.raises(RuntimeError, match="Cannot apply the parked round"):
        _ask(resumed, ctx, calls, round_idx=4)

    assert sql_tool_confirmation_reader(store.engine).read_tool_confirmation_decisions("task1", round_idx=4) == {}


@parametrize_from_dict({"store failure": {"fails": True}, "spent by another attempt": {"fails": False}})
def test_an_answer_this_attempt_did_not_spend_is_not_acted_on(tmp_path, fails):
    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "sent"))
    reader = sql_tool_confirmation_reader()
    store = _claimed_task(tmp_path)
    broker = _broker(tmp_path, catalog, reader=reader)
    calls = [make_tool_call("data_write", {"file_path": "report.pdf"})]
    request = _ask(broker, ctx, calls, round_idx=4).request
    reader.resolve_tool_confirmation(request.confirmation_id, decision="approved", now_iso=utc_now_iso())
    if fails:
        with fail_sql(
            "UPDATE tool_confirmations SET applied_at",
            OperationalError("tool_confirmations", {}, Exception("connection lost")),
        ):
            assert _ask(broker, ctx, calls, round_idx=4) == ConfirmationRound(decisions={}, request=None)
    else:
        spent = False

        def competing_spend(conn, cursor, statement, parameters, context, executemany):
            nonlocal spent
            if not spent and "UPDATE tool_confirmations SET applied_at" in statement:
                spent = True
                sql_tool_confirmation_reader().spend_round_decisions("task1", round_idx=4, now_iso=utc_now_iso())

        event.listen(store.engine, "before_cursor_execute", competing_spend)
        try:
            assert _ask(broker, ctx, calls, round_idx=4) == ConfirmationRound(decisions={}, request=None)
        finally:
            if event.contains(store.engine, "before_cursor_execute", competing_spend):
                event.remove(store.engine, "before_cursor_execute", competing_spend)


def test_an_answer_this_attempt_could_not_read_is_not_asked_about_again(tmp_path, caplog):
    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "sent"))
    reader = sql_tool_confirmation_reader()
    broker = _broker(tmp_path, catalog, reader=reader)
    calls = [make_tool_call("data_write", {"file_path": "report.pdf"})]

    with fail_sql("tool_confirmations", OperationalError("tool_confirmations", {}, Exception("connection lost"))):
        assert _ask(broker, ctx, calls, round_idx=4) == ConfirmationRound(decisions={}, request=None)
    assert "Failed to read the owner's confirmations" in caplog.text


def test_an_answer_to_a_different_call_is_not_this_calls(tmp_path):
    """Same tool-call id, other arguments: the decision was given for a call the model changed."""
    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "sent"))
    store = _claimed_task(tmp_path)
    broker = _broker(tmp_path, catalog)
    _answer(store, _ask(broker, ctx, [make_tool_call("data_write", {"file_path": "report.pdf"})]).request)

    assert _ask(broker, ctx, [make_tool_call("data_write", {"file_path": "payroll.pdf"})]).decisions == {}


def test_a_twin_of_the_answered_call_is_put_to_the_owner_too(tmp_path):
    """Two identical calls with an empty id: one answer allows one send, the other is asked about."""
    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "sent"))
    store = _claimed_task(tmp_path)
    broker = _broker(tmp_path, catalog)
    calls = [
        make_tool_call("data_write", {"file_path": "report.pdf"}),
        make_tool_call("data_write", {"file_path": "report.pdf"}),
    ]
    calls[0].id = calls[1].id = ""
    _answer(store, _ask(broker, ctx, calls).request)

    asked = _ask(broker, ctx, calls)

    assert asked.request is not None, "the second send was never put to the owner"
    assert len(asked.decisions) == 1


def test_an_undelivered_question_is_withdrawn_and_the_run_goes_on(tmp_path):
    """A request recorded as opened with no recorded outcome is a stuck decision nobody can see."""
    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "sent"))
    store = _claimed_task(tmp_path)
    broker = _broker(tmp_path, catalog)
    request = _ask(broker, ctx, [make_tool_call("data_write")]).request

    assert broker.abandon(request) is True
    assert store.tasks_repo.get_task("task1").status == "running"
    assert sql_tool_confirmation_reader(store.engine).read_open_tool_confirmation(TEST_SCOPE) is None
    assert (
        len(
            sql_audit_event_repository(store.engine).list_audit_events(
                event_type="policy.tool_confirmation_requested", limit=50
            )
        )
        == 1
    )
    assert (
        len(
            sql_audit_event_repository(store.engine).list_audit_events(
                event_type="policy.tool_confirmation_expired", limit=50
            )
        )
        == 1
    )


@parametrize_from_dict({"disabled": {"enabled": False}, "enabled": {"enabled": True}})
def test_withdrawal_cannot_resume_above_the_owner_quota_and_requeues_for_recovery(tmp_path, monkeypatch, enabled):
    from new_core.coordination.inprocess import NullWorkSignal
    from ouroboros.storage.runtime_models import TaskRequest
    from supervisor.queue import TaskQueue
    from tests.helpers.shared import scope_with

    monkeypatch.setenv("OUROBOROS_DURABLE_JOBS_ENABLED", str(enabled).lower())
    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "sent"))
    store = _claimed_task(tmp_path)
    broker = _broker(tmp_path, catalog)
    request = _ask(broker, ctx, [make_tool_call("data_write")]).request
    for i in range(3):
        scope = scope_with(TEST_SCOPE, chat_id=f"neighbor-{i}")
        store.tasks_repo.create_task(TaskRequest(id=f"other-{i}", scope=scope.to_dict()))
        assert store.tasks_repo.claim_next_task(runtime_id="runtime")
    assert broker.abandon(request) is False
    assert len(store.tasks_repo.list_tasks(statuses=["running"])) == 3
    queue = TaskQueue(
        store.tasks_repo, tmp_path, work_signal=NullWorkSignal(), audit_event_repository=sql_audit_event_repository()
    )
    assert queue.reconcile_parked_tasks(sql_tool_confirmation_reader()) == ["task1"]
    assert store.tasks_repo.claim_next_task(runtime_id="runtime") is None
    store.tasks_repo.update_task_status("other-0", "completed")
    assert store.tasks_repo.claim_next_task(runtime_id="runtime").task_id == "task1"
    assert sql_tool_confirmation_reader(store.engine).take_parked_snapshot("task1") is not None


def test_an_answer_that_landed_first_survives_the_withdrawal(tmp_path):
    from ouroboros.utils import utc_now_iso

    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "sent"))
    store = _claimed_task(tmp_path)
    broker = _broker(tmp_path, catalog)
    request = _ask(broker, ctx, [make_tool_call("data_write")]).request
    sql_tool_confirmation_reader(store.engine).resolve_tool_confirmation(
        request.confirmation_id, decision="approved", now_iso=utc_now_iso()
    )

    assert broker.abandon(request) is False
    assert store.tasks_repo.get_task("task1").status == "waiting_confirmation"


def test_a_question_that_could_not_be_withdrawn_keeps_the_round_waiting(tmp_path):
    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "sent"))
    reader = sql_tool_confirmation_reader()
    store = _claimed_task(tmp_path)
    broker = _broker(tmp_path, catalog, reader=reader)
    request = _ask(broker, ctx, [make_tool_call("data_write")]).request
    with fail_sql("tool_confirmations", OperationalError("tool_confirmations", {}, Exception("connection lost"))):
        assert broker.abandon(request) is False
    assert store.tasks_repo.get_task("task1").status == "waiting_confirmation"
    assert reader.read_open_tool_confirmation(TEST_SCOPE).confirmation_id == request.confirmation_id


def test_what_earlier_calls_came_to_comes_back_with_the_parked_run(tmp_path):
    from ouroboros.agent.task_ledger import RepeatToolGuard, TaskLedger

    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "sent"))
    store = _claimed_task(tmp_path)
    failing = {"file_path": "a"}
    guard, ledger = RepeatToolGuard(), TaskLedger()
    for _ in range(2):
        guard.record_failure("data_write", failing, "SEND_FAILED")
        ledger.record_tool_result(
            tool_name="data_write", args_hash=safe_args_hash(failing), status="error", failure_class="SEND_FAILED"
        )
    calls = [make_tool_call("data_write", failing), make_tool_call("data_write", {"file_path": "b"}, idx=1)]
    parked = _broker(tmp_path, catalog, repeat_guard=guard, task_ledger=ledger)
    _answer(store, _ask(parked, ctx, calls, round_idx=4).request)

    resumed_ledger = TaskLedger()
    resumed = _broker(tmp_path, catalog, repeat_guard=RepeatToolGuard(), task_ledger=resumed_ledger)
    resumed.restore(task_id="task1", tool_ctx=ctx)

    assert _ask(resumed, ctx, calls, round_idx=4).request is None, "the round is preflighted as it was when it parked"
    assert resumed_ledger.render() == ledger.render()


def test_attachments_kept_for_terminal_delivery_come_back_with_the_parked_run(tmp_path):
    """A detached run stores its files for the final delivery; a park in between must not drop them."""
    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "sent"))
    _claimed_task(tmp_path)
    kept = {"id": "a1", "kind": "file", "filename": "a.txt", "mime": "text/plain", "size": 1, "caption": ""}
    ctx.terminal_attachments.append(kept)
    _ask(_broker(tmp_path, catalog), ctx, [make_tool_call("data_write")])

    resumed_ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path)
    _broker(tmp_path, catalog).restore(task_id="task1", tool_ctx=resumed_ctx)

    assert resumed_ctx.terminal_attachments == [kept]


def test_the_skill_instructions_in_force_come_back_with_the_parked_run(tmp_path):
    """The language guard and compaction read these, and the next skill_read rebuilds the section from them."""
    from ouroboros.agent.context import active_skill_instructions
    from ouroboros.tools.core import ReadPage
    from ouroboros.tools.skills import SkillReadResult

    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "sent"))
    _claimed_task(tmp_path)
    page = SkillReadResult(ReadPage(header="h", body="do this", footer="f", start=0, total=7), "skill:a", "v1", "a")
    ctx.active_skill_pages["skill:a"] = [page]
    ctx.skill_recency.append("skill:a")
    ctx.projected_skills.add("skill:a")
    ctx.pending_skill_read = "skill:a"
    _ask(_broker(tmp_path, catalog), ctx, [make_tool_call("data_write")])

    resumed_ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path)
    _broker(tmp_path, catalog).restore(task_id="task1", tool_ctx=resumed_ctx)

    assert resumed_ctx.active_skill_pages == {"skill:a": [page]}
    assert (resumed_ctx.skill_recency, resumed_ctx.projected_skills) == (["skill:a"], {"skill:a"})
    assert resumed_ctx.pending_skill_read == "skill:a"
    assert "do this" in active_skill_instructions(resumed_ctx)


@pytest.mark.parametrize(
    ("reattached", "kept"), [("s-1", True), ("s-2", False)], ids=["the same session", "another instance's session"]
)
def test_only_the_session_the_run_held_is_handed_to_the_approved_call(tmp_path, monkeypatch, caplog, reattached, kept):
    """An ephemeral session is one process's: another instance gets a fresh sandbox without the files the call is about."""
    from ouroboros.tools.workstation import connect_tool

    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "sent"))
    _claimed_task(tmp_path)
    ctx.workstation = types.SimpleNamespace(record=types.SimpleNamespace(session_id="s-1"))
    _ask(_broker(tmp_path, catalog), ctx, [make_tool_call("data_write")])
    handle = types.SimpleNamespace(record=types.SimpleNamespace(session_id=reattached))

    def attach(tool_ctx):
        tool_ctx.workstation = handle
        return handle

    monkeypatch.setattr(connect_tool, "attach_session", attach)
    resumed_ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path)
    with caplog.at_level(logging.WARNING):
        _broker(tmp_path, catalog).restore(task_id="task1", tool_ctx=resumed_ctx)

    assert (resumed_ctx.workstation is handle) is kept
    assert ("another session" in caplog.text) is not kept


def test_a_round_with_two_gated_calls_parks_twice_and_then_runs(tmp_path):
    """The round reads the first answer both times it parks; the second question is the other call's."""
    from ouroboros.agent.loop_adapters import ToolConfirmationBroker, build_tool_call_executor

    catalog, ctx = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "written"))
    store = _claimed_task(tmp_path)
    executor = build_tool_call_executor(
        confirmation_gate=True,
        catalog=catalog,
        events=collecting_event_sink(),
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    broker = ToolConfirmationBroker(
        executor=executor,
        claim_token="",
        tool_confirmation_reader=sql_tool_confirmation_reader(),
    )
    calls = [make_tool_call("data_write", {"file_path": "a"}), make_tool_call("data_write", {"file_path": "b"}, idx=1)]

    _answer(store, _ask(broker, ctx, calls).request)
    _answer(store, _ask(broker, ctx, calls).request)
    answered = _ask(broker, ctx, calls)

    assert answered.request is None, "both calls are answered, so the round runs"
    results = executor.execute(calls, ctx=ctx, task_id="task1", decisions=answered.decisions)
    assert [r["result"] for r in results] == ["written", "written"]
