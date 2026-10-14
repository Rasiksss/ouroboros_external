from __future__ import annotations

from new_core.identity import OwnerScope
from ouroboros.storage.runtime_models import TaskRecord, TaskRequest, ToolConfirmation
from tests.helpers.shared import TEST_SCOPE
from tests.helpers.tool_confirmations import sql_tool_confirmation_reader


def claim_task(store, task_id: str = "task1", *, scope: OwnerScope = TEST_SCOPE) -> TaskRecord:
    store.tasks_repo.create_task(
        TaskRequest(id=task_id, type="task", text="work", chat_id=scope.chat_id, scope=scope.to_dict())
    )
    claimed = store.tasks_repo.claim_next_task(worker_id=0, runtime_id="runtime-a")
    assert claimed is not None and claimed.task_id == task_id
    return claimed


def make_tool_confirmation(
    *,
    scope: OwnerScope = TEST_SCOPE,
    task_id: str = "task1",
    tool: str = "data_write",
    args_hash: str = "hash-of-call-0",
    round_idx: int = 1,
    confirmation_id: str = "",
) -> ToolConfirmation:
    return ToolConfirmation(
        owner_id=scope.owner_id,
        agent_id=scope.agent_id,
        surface_id=str(scope.surface_id),
        chat_id=scope.chat_id,
        confirmation_id=confirmation_id,
        task_id=task_id,
        tool_name=tool,
        round_idx=round_idx,
        args_hash=args_hash,
    )


def park_run(store, *, task_id: str = "task1", snapshot: dict | None = None, **confirmation):
    return sql_tool_confirmation_reader(store.engine).park_and_open(
        task_id=task_id,
        claim_token="",
        confirmation=make_tool_confirmation(task_id=task_id, **confirmation),
        snapshot=snapshot if snapshot is not None else {"round_idx": 1, "messages": []},
        workspace="",
        arguments={},
    )
