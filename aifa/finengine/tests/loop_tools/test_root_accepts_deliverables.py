"""ADR-0061: the root, not the service, accepts user deliverables."""

from loop_support import open_cycle
import pytest


@pytest.mark.asyncio
async def test_agent_cycle_does_not_accept_or_reject_user_file_requirements(base):
    cycle = await open_cycle(base, entry_stage="agent")
    response = await cycle.call("aifa_move", {
        "objective": "execution only", "reason": "root owns the plan",
        "plan": ["make workbook"], "files": [{"kind": "xlsx", "parts": 2}],
        "steps": [{"atom": "not_an_atom"}],
    })
    assert "ОТКАЗ" in response or "отклонены" in response
    assert cycle.session.expected_files == ()
    assert cycle.session.plan == ()
    state = await cycle.call("aifa_state")
    assert "task_plan" in state
    assert "пришли `plan` и `files`" not in state
    response = await cycle.call("aifa_finish", {
        "action": "finalize", "deliverables": [], "evidence_ids": [],
    })
    assert response.startswith("ПРИНЯТО")
    await cycle.close()
