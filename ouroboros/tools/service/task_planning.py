"""Root orchestration over delegated plan proposals and result reviews."""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import partial

from pydantic import ValidationError

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.contracts.task_plan import AcceptPlan, FinishPlan, PlanProposal, PlanState, StepReview, StepUpdate
from ouroboros.storage.runtime_models import TaskRecord
from ouroboros.storage.runtime_persistence import runtime_persistence


def _same_scope(ctx: ToolContext, task: TaskRecord) -> bool:
    return (task.owner_id, task.agent_id, task.surface_id, str(task.chat_id or "")) == (
        ctx.scope.owner_id,
        ctx.scope.agent_id,
        str(ctx.scope.surface_id),
        ctx.scope.chat_id,
    )


def _accept(state: PlanState, command: AcceptPlan, proposal: PlanProposal) -> None:
    state.accept(proposal, command)


def task_plan(ctx: ToolContext, action: str, data: Mapping[str, object] | None = None) -> str:
    try:
        return _operate(ctx, action, data or {})
    except (ValueError, ValidationError) as exc:
        return f"ERROR: {exc}"


def _operate(ctx: ToolContext, action: str, data: Mapping[str, object]) -> str:
    command: AcceptPlan | StepUpdate | StepReview | FinishPlan
    if action == "schema":
        return json.dumps(
            {
                "proposal": PlanProposal.model_json_schema(),
                "commands": {
                    "accept": AcceptPlan.model_json_schema(),
                    "step": StepUpdate.model_json_schema(),
                    "review": StepReview.model_json_schema(),
                    "finish": FinishPlan.model_json_schema(),
                },
            },
            ensure_ascii=False,
        )
    root_id = str(ctx.root_task_id or ctx.task_id or "")
    tasks = runtime_persistence.current.tasks_repo
    root = tasks.get_task(root_id)
    caller = tasks.get_task(str(ctx.task_id or ""))
    if root is None or caller is None or not _same_scope(ctx, root) or not _same_scope(ctx, caller):
        raise ValueError("task not found in the current scope")
    if root.parent_task_id or (
        caller.task_id != root_id and (caller.input.get("root_task_id") or caller.parent_task_id) != root_id
    ):
        raise ValueError("caller does not belong to this root")
    plans = runtime_persistence.current.plans_repo
    if action == "read":
        return plans.load(ctx.scope, root_id).model_dump_json()
    if caller.task_id != root_id or ctx.task_depth != 0:
        raise ValueError("only the root orchestrator can write the plan")
    if action == "accept":
        command = AcceptPlan.model_validate(data)
        child = tasks.get_task(command.proposal_task_id)
        if (
            child is None
            or not _same_scope(ctx, child)
            or child.parent_task_id != root_id
            or child.status != "completed"
        ):
            raise ValueError("proposal must be the full result of a completed direct child in this scope")
        proposal = PlanProposal.model_validate_json(child.result)
        mutation = partial(_accept, command=command, proposal=proposal)
    elif action == "step":
        command = StepUpdate.model_validate(data)
        mutation = partial(PlanState.update_step, command=command)
    elif action == "review":
        command = StepReview.model_validate(data)
        mutation = partial(PlanState.review, command=command)
    elif action == "finish":
        command = FinishPlan.model_validate(data)
        mutation = partial(PlanState.finish, command=command)
    else:
        raise ValueError("unknown action")
    return plans.change(
        ctx.scope, root_id, command.expected_version, mutation, claim_token=ctx.execution_claim
    ).model_dump_json()


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="task_plan",
            schema=ToolSchema(
                name="task_plan",
                description=(
                    "Root-owned task plan. schema returns proposal and command schemas; read returns the current "
                    "version. accept reads a completed planning child's full JSON result. step records an attempt; "
                    "review records the root's observations and acceptance; finish records complete or partial. "
                    "Only the root writes. Supply versioned result/observation references, not promises."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["schema", "read", "accept", "step", "review", "finish"]},
                        "data": {
                            "type": "object",
                            "description": "Command fields from task_plan(action='schema'); every write requires expected_version.",
                        },
                    },
                    "required": ["action"],
                },
            ),
            handler=task_plan,
        )
    ]
