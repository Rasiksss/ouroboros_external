"""Factories for root-owned planning integration tests."""

import json

from ouroboros.storage.runtime_models import TaskRequest
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.tools.service.task_planning import task_plan
from tests.helpers.shared import TEST_SCOPE, make_tool_ctx


def make_plan_proposal(base_revision=0, *, kind="subtask"):
    return {
        "schema_version": "1",
        "base_revision": base_revision,
        "requirements": [{"id": "R1", "description": "Two sheets", "source": "Excel на двух листах"}],
        "assumptions": [],
        "questions": [],
        "steps": [
            {
                "id": "S1",
                "objective": "Build workbook",
                "requirement_ids": ["R1"],
                "depends_on": [],
                "inputs": [],
                "execution": {"kind": kind, "instruction": "Create an atom"},
                "expected_outputs": ["XLSX with two sheets"],
                "acceptance_criteria": [
                    {"id": "C1", "description": "Two sheets", "verification_method": "Inspect workbook"}
                ],
            }
        ],
    }


def make_plan_tool_ctx(tmp_path, *, kind="subtask", steps=1):
    store = runtime_persistence.current.tasks_repo
    store.create_task(TaskRequest(id="root", scope=TEST_SCOPE.to_dict(), text="Excel на двух листах"), status="running")
    store.create_task(
        TaskRequest(id="planner", scope=TEST_SCOPE.to_dict(), parent_task_id="root", root_task_id="root"),
        status="running",
    )
    payload = make_plan_proposal(kind=kind)
    payload["steps"] = [{**payload["steps"][0], "id": f"S{index}"} for index in range(1, steps + 1)]
    store.complete_task("planner", result=json.dumps(payload))
    ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path, task_id="root", root_task_id="root")
    accepted = json.loads(
        task_plan(ctx, "accept", {"proposal_task_id": "planner", "expected_version": 0, "reason": "Covers the request"})
    )
    assert accepted["revision"] == 1
    return ctx
