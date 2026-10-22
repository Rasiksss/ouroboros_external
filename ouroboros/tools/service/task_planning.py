"""Root orchestration over delegated plan proposals and result reviews."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from functools import partial

from pydantic import ValidationError

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.interfaces import ToolDescriptor
from new_core.tools.schema import ToolSchema
from ouroboros.contracts.task_plan import (
    AcceptPlan,
    ExecutionBinding,
    FinishPlan,
    PlanProposal,
    PlanState,
    StepReview,
    StepUpdate,
)
from ouroboros.storage.runtime_models import TaskRecord
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.storage.task_results import TERMINAL_STATUSES

_FENCED_BLOCK = re.compile(r"```[A-Za-z]*[ \t]*\n(.*?)```", re.DOTALL)


def _proposal_json(message: str) -> str:
    """The planner's final message with a Markdown fence around the object, if it has one.

    Models fence JSON no matter what the skill asks. The last fenced block holding an object is the
    proposal; a message without one stays as it is, so the validation error still names the real text.
    """
    for block in reversed(_FENCED_BLOCK.findall(message)):
        if block.strip().startswith("{"):
            return block.strip()
    return message


def _same_scope(ctx: ToolContext, task: TaskRecord) -> bool:
    return (task.owner_id, task.agent_id, task.surface_id, str(task.chat_id or "")) == (
        ctx.scope.owner_id,
        ctx.scope.agent_id,
        str(ctx.scope.surface_id),
        ctx.scope.chat_id,
    )


def _accept(state: PlanState, command: AcceptPlan, proposal: PlanProposal) -> None:
    state.accept(proposal, command)


def _children_idle(ctx: ToolContext, state: PlanState, step_id: str = "") -> None:
    for call in state.calls:
        if not call.child_task_id or (step_id and call.step_id != step_id):
            continue
        child = runtime_persistence.current.tasks_repo.get_task(call.child_task_id)
        if child is None or not _same_scope(ctx, child) or child.status not in TERMINAL_STATUSES:
            raise ValueError(
                f"executor child {call.child_task_id} is still active or unavailable; wait or cancel it first"
            )
        digest = hashlib.sha256(child.result.encode()).hexdigest()
        call.child_result_ref = f"task:{child.task_id}:{child.status}:sha256:{digest}"


def _idle_mutation(ctx: ToolContext, mutation: Callable[[PlanState], None], step_id: str, state: PlanState) -> None:
    _children_idle(ctx, state, step_id)
    mutation(state)


def _execution_change(ctx: ToolContext, mutation: Callable[[PlanState], None]) -> PlanState:
    plans = runtime_persistence.current.plans_repo
    root_id = str(ctx.root_task_id or ctx.task_id or "")
    return plans.change(ctx.scope, root_id, None, mutation, claim_token=ctx.execution_claim)


def _begin(ctx: ToolContext, binding: ExecutionBinding, call_id: str, name: str, state: PlanState) -> None:
    _children_idle(ctx, state, binding.step_id)
    state.begin_execution(binding, call_id, name)


def _begin_close(ctx: ToolContext, call_id: str, state: PlanState) -> None:
    state.ensure_idle()
    _children_idle(ctx, state)
    state.closing_call_id = call_id


def prepare_plan_execution(ctx: ToolContext, descriptor: ToolDescriptor, binding_data: object) -> ToolContext:
    if ctx.task_depth or not ctx.task_id or not (descriptor.schema.plan_executor or descriptor.schema.plan_closer):
        return ctx
    root_id = str(ctx.root_task_id or ctx.task_id)
    if root_id != ctx.task_id:
        raise ValueError("root execution context is inconsistent")
    state = runtime_persistence.current.plans_repo.load(ctx.scope, root_id)
    if not state.revisions:
        if binding_data is not None:
            raise ValueError("accept a plan before binding executor calls")
        return ctx
    if descriptor.schema.plan_closer:
        call_id = str(ctx.tool_call_id or uuid.uuid4().hex)
        _execution_change(ctx, partial(_begin_close, ctx, call_id))
        return replace(ctx, plan_call_id=call_id)
    if binding_data is None:
        raise ValueError("PLAN_BINDING_REQUIRED: supply _plan with revision, step_id and attempt_id")
    binding = ExecutionBinding.model_validate(binding_data)
    call_id = str(ctx.tool_call_id or uuid.uuid4().hex)
    _execution_change(ctx, partial(_begin, ctx, binding, call_id, descriptor.name))
    return replace(ctx, plan_call_id=call_id)


def _attach_child(ctx: ToolContext, child_task_id: str, state: PlanState) -> None:
    child = runtime_persistence.current.tasks_repo.get_task(child_task_id)
    if child is None or not _same_scope(ctx, child) or child.parent_task_id != ctx.task_id:
        raise ValueError("executor child is not a direct child in this scope")
    state.execution(ctx.plan_call_id).child_task_id = child_task_id


def attach_plan_child(ctx: ToolContext, child_task_id: str) -> None:
    _execution_change(ctx, partial(_attach_child, ctx, child_task_id))


def _returned(call_id: str, text: str, failed: bool, state: PlanState) -> None:
    if state.closing_call_id == call_id:
        state.closing_call_id = ""
        return
    call = state.execution(call_id)
    call.status = "failed" if failed else "returned"
    call.result_ref = "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def finish_plan_execution(ctx: ToolContext, text: str, *, failed: bool = False) -> None:
    if ctx.plan_call_id:
        _execution_change(ctx, partial(_returned, ctx.plan_call_id, text, failed))


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
                "execution_binding": ExecutionBinding.model_json_schema(),
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
        proposal = PlanProposal.model_validate_json(_proposal_json(child.result))
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
    step_id = command.step_id if isinstance(command, StepUpdate | StepReview) else ""
    return plans.change(
        ctx.scope,
        root_id,
        command.expected_version,
        partial(_idle_mutation, ctx, mutation, step_id),
        claim_token=ctx.execution_claim,
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
