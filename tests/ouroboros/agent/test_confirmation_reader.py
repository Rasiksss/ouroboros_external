from ouroboros.agent.loop_adapters import ToolConfirmationBroker, build_tool_call_executor
from ouroboros.agent.loop_tool_execution import GatedCall
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.shared import collecting_event_sink, make_static_tool_env, make_tool_entry
from tests.helpers.tool_confirmation_conformance import confirmation
from tests.helpers.tool_confirmations import seed_tool_confirmations


def test_broker_uses_injected_decisions_and_matches_the_complete_call_key(tmp_path):
    reader = seed_tool_confirmations(
        [
            (
                confirmation(status="approved", call_ordinal=1, tool_name="data_write", args_hash="hash", round_idx=2),
                "",
            ),
            (
                confirmation(
                    confirmation_id="other",
                    status="denied",
                    call_ordinal=2,
                    tool_name="other",
                    args_hash="hash",
                    round_idx=2,
                ),
                "",
            ),
        ]
    )
    catalog, _ = make_static_tool_env(tmp_path, make_tool_entry("data_write", lambda ctx, **kw: "sent"))
    executor = build_tool_call_executor(
        catalog=catalog,
        events=collecting_event_sink(),
        confirmation_gate=True,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    broker = ToolConfirmationBroker(executor=executor, claim_token="", tool_confirmation_reader=reader)
    calls = [GatedCall(1, "data_write", {}, "hash"), GatedCall(1, "data_write", {}, "other-hash")]
    assert broker._decided(calls, task_id="task", round_idx=2) == {(1, "data_write", "hash"): "approved"}
    assert broker._decided(calls, task_id="other", round_idx=2) == {}
    assert broker._decided(calls, task_id="task", round_idx=3) == {}
