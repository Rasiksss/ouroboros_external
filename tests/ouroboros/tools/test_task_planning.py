"""Root-owned plan acceptance through the real tool and runtime database."""

import json

import pytest

from ouroboros.storage.runtime_models import TaskRequest
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.tools.service.task_planning import task_plan
from tests.helpers.shared import TEST_SCOPE, make_tool_ctx, scope_with
from tests.helpers.task_plans import make_plan_proposal, make_plan_tool_ctx


@pytest.fixture(autouse=True)
def plan_encryption_key(monkeypatch):
    monkeypatch.setenv(
        "OUROBOROS_CREDENTIALS_ENCRYPTION_KEY",
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==",
    )


def test_one_sheet_result_is_not_accepted_and_survives_reconstruction(tmp_path):
    ctx = make_plan_tool_ctx(tmp_path)
    task_plan(ctx, "step", {"expected_version": 1, "step_id": "S1", "status": "running", "attempt_id": "try1"})
    task_plan(
        ctx,
        "step",
        {
            "expected_version": 2,
            "step_id": "S1",
            "status": "awaiting_review",
            "attempt_id": "try1",
            "result_refs": ["file:xlsx@hash1"],
        },
    )
    reviewed = json.loads(
        task_plan(
            ctx,
            "review",
            {
                "expected_version": 3,
                "step_id": "S1",
                "attempt_id": "try1",
                "result_refs": ["file:xlsx@hash1"],
                "checks": [
                    {
                        "criterion_id": "C1",
                        "verdict": "fail",
                        "observation_refs": ["inspection:hash1"],
                        "note": "Only one sheet",
                    }
                ],
                "decision": "revise",
                "explanation": "Needs two sheets",
            },
        )
    )
    assert reviewed["steps"]["S1"]["status"] == "failed"
    assert json.loads(task_plan(ctx, "read"))["reviews"] == reviewed["reviews"]
    assert task_plan(ctx, "finish", {"expected_version": 4, "status": "complete", "reason": "Done"}).startswith(
        "ERROR:"
    )
    assert (
        json.loads(
            task_plan(
                ctx, "finish", {"expected_version": 4, "status": "partial", "reason": "Workbook needs correction"}
            )
        )["status"]
        == "partial"
    )


def test_only_root_can_write_and_scope_cannot_be_changed(tmp_path):
    ctx = make_plan_tool_ctx(tmp_path)
    child = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path, task_id="planner", root_task_id="root", task_depth=1)
    assert task_plan(child, "finish", {"expected_version": 1, "status": "partial", "reason": "stop"}).startswith(
        "ERROR:"
    )
    stranger = make_tool_ctx(
        repo_dir=tmp_path,
        drive_root=tmp_path,
        scope=scope_with(TEST_SCOPE, chat_id="other"),
        task_id="root",
        root_task_id="root",
    )
    assert task_plan(stranger, "read").startswith("ERROR:")
    assert task_plan(ctx, "finish", {"expected_version": 0, "status": "partial", "reason": "stale"}).startswith(
        "ERROR:"
    )


def test_completed_child_result_is_the_full_proposal_not_a_short_report(tmp_path):
    ctx = make_plan_tool_ctx(tmp_path)
    store = runtime_persistence.current.tasks_repo
    store.create_task(TaskRequest(id="other", scope=TEST_SCOPE.to_dict(), parent_task_id="root"), status="running")
    assert task_plan(ctx, "accept", {"proposal_task_id": "other", "expected_version": 1, "reason": "new"}).startswith(
        "ERROR:"
    )
    store.complete_task("other", result=json.dumps(make_plan_proposal()))
    assert task_plan(
        ctx, "accept", {"proposal_task_id": "other", "expected_version": 1, "reason": "stale base"}
    ).startswith("ERROR:")


def test_all_criteria_need_observations_before_acceptance(tmp_path):
    ctx = make_plan_tool_ctx(tmp_path)
    task_plan(ctx, "step", {"expected_version": 1, "step_id": "S1", "status": "running", "attempt_id": "a"})
    task_plan(
        ctx,
        "step",
        {
            "expected_version": 2,
            "step_id": "S1",
            "status": "awaiting_review",
            "attempt_id": "a",
            "result_refs": ["file:v1"],
        },
    )
    assert task_plan(
        ctx,
        "review",
        {
            "expected_version": 3,
            "step_id": "S1",
            "attempt_id": "a",
            "result_refs": ["file:v1"],
            "checks": [],
            "decision": "accept",
            "explanation": "Looks fine",
        },
    ).startswith("ERROR:")


def test_invalid_proposal_does_not_start_execution(tmp_path):
    from pydantic import ValidationError

    from ouroboros.contracts.task_plan import PlanProposal

    data = make_plan_proposal()
    data["steps"][0]["depends_on"] = ["S1"]
    with pytest.raises(ValidationError):
        PlanProposal.model_validate(data)


def test_replanning_cannot_erase_a_running_attempt(tmp_path):
    ctx = make_plan_tool_ctx(tmp_path)
    task_plan(ctx, "step", {"expected_version": 1, "step_id": "S1", "status": "running", "attempt_id": "active-child"})
    store = runtime_persistence.current.tasks_repo
    store.create_task(TaskRequest(id="revision", scope=TEST_SCOPE.to_dict(), parent_task_id="root"), status="running")
    store.complete_task("revision", result=json.dumps(make_plan_proposal(1)))
    assert task_plan(
        ctx, "accept", {"proposal_task_id": "revision", "expected_version": 2, "reason": "replan"}
    ).startswith("ERROR:")
    current = json.loads(task_plan(ctx, "read"))
    assert current["steps"]["S1"]["attempt_id"] == "active-child"
    assert task_plan(ctx, "finish", {"expected_version": 2, "status": "partial", "reason": "stop"}).startswith("ERROR:")


def test_plan_storage_is_encrypted_even_when_chat_encryption_is_disabled(tmp_path, monkeypatch):
    from sqlalchemy import text

    monkeypatch.setenv("OUROBOROS_CHAT_ENCRYPTION_ENABLED", "false")
    ctx = make_plan_tool_ctx(tmp_path)
    runtime = runtime_persistence.current
    with runtime.engine.connect() as conn:
        stored = conn.execute(text("SELECT state_encrypted FROM task_plans WHERE root_task_id='root'")).scalar_one()
    assert "Two sheets" not in stored and "requirements" not in stored
    assert json.loads(task_plan(ctx, "read"))["revision"] == 1


def accepted_workbook(ctx):
    task_plan(ctx, "step", {"expected_version": 1, "step_id": "S1", "status": "running", "attempt_id": "a"})
    task_plan(
        ctx,
        "step",
        {
            "expected_version": 2,
            "step_id": "S1",
            "status": "awaiting_review",
            "attempt_id": "a",
            "result_refs": ["file:workbook@sha256:two-sheets"],
        },
    )
    return json.loads(
        task_plan(
            ctx,
            "review",
            {
                "expected_version": 3,
                "step_id": "S1",
                "attempt_id": "a",
                "result_refs": ["file:workbook@sha256:two-sheets"],
                "checks": [
                    {
                        "criterion_id": "C1",
                        "verdict": "pass",
                        "observation_refs": ["inspection:two-sheets@sha256:report"],
                        "note": "Workbook opens and contains two sheets",
                    }
                ],
                "decision": "accept",
                "explanation": "Checked against user requirement",
            },
        )
    )


def test_root_acceptance_allows_complete_and_plan_survives_new_repository_instance(tmp_path):
    from functools import partial

    from pydantic import SecretStr

    from infra.credentials_key import CredentialsKey
    from ouroboros.storage.task_plans import SqlTaskPlanRepository

    ctx = make_plan_tool_ctx(tmp_path)
    assert accepted_workbook(ctx)["steps"]["S1"]["status"] == "accepted"
    complete = json.loads(
        task_plan(ctx, "finish", {"expected_version": 4, "status": "complete", "reason": "All results checked"})
    )
    runtime = runtime_persistence.current
    key = SecretStr("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==")
    restored = SqlTaskPlanRepository(runtime.transaction_manager, partial(CredentialsKey, key)).load(TEST_SCOPE, "root")
    assert restored.model_dump(mode="json") == complete
    assert restored.reviews[0].review.checks[0].observation_refs == ["inspection:two-sheets@sha256:report"]


def test_unchanged_accepted_step_is_preserved_in_a_revised_plan(tmp_path):
    ctx = make_plan_tool_ctx(tmp_path)
    accepted_workbook(ctx)
    store = runtime_persistence.current.tasks_repo
    store.create_task(TaskRequest(id="revision", scope=TEST_SCOPE.to_dict(), parent_task_id="root"), status="running")
    store.complete_task("revision", result=json.dumps(make_plan_proposal(1)))
    revised = json.loads(
        task_plan(
            ctx, "accept", {"proposal_task_id": "revision", "expected_version": 4, "reason": "Reviewed unchanged plan"}
        )
    )
    assert revised["revision"] == 2 and len(revised["revisions"]) == 2
    assert revised["steps"]["S1"]["status"] == "accepted"
    assert len(revised["reviews"]) == 1


def test_review_of_another_file_version_is_refused_without_mutation(tmp_path):
    ctx = make_plan_tool_ctx(tmp_path)
    task_plan(ctx, "step", {"expected_version": 1, "step_id": "S1", "status": "running", "attempt_id": "a"})
    task_plan(
        ctx,
        "step",
        {
            "expected_version": 2,
            "step_id": "S1",
            "status": "awaiting_review",
            "attempt_id": "a",
            "result_refs": ["file:original-version"],
        },
    )
    state = json.loads(task_plan(ctx, "read"))
    review = {
        "expected_version": 3,
        "step_id": "S1",
        "attempt_id": "a",
        "result_refs": ["file:other-version"],
        "checks": [
            {
                "criterion_id": "C1",
                "verdict": "pass",
                "observation_refs": ["inspection:other-version"],
                "note": "checked another file",
            }
        ],
        "decision": "accept",
        "explanation": "Wrong result",
    }
    assert "exact returned result versions" in task_plan(ctx, "review", review)
    assert json.loads(task_plan(ctx, "read")) == state


def test_two_concurrent_writers_cannot_overwrite_each_other(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from functools import partial

    ctx = make_plan_tool_ctx(tmp_path)
    commands = [
        {"expected_version": 1, "step_id": "S1", "status": "running", "attempt_id": attempt} for attempt in ("a", "b")
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(partial(task_plan, ctx, "step"), commands))
    assert sum(reply.startswith("ERROR:") for reply in replies) == 1
    assert json.loads(task_plan(ctx, "read"))["version"] == 2


def test_an_unrelated_completed_task_cannot_supply_a_make_plan_proposal(tmp_path):
    ctx = make_plan_tool_ctx(tmp_path)
    store = runtime_persistence.current.tasks_repo
    store.create_task(
        TaskRequest(id="outsider", scope=TEST_SCOPE.to_dict(), parent_task_id="another-root"), status="running"
    )
    store.complete_task("outsider", result=json.dumps(make_plan_proposal(1)))
    assert task_plan(
        ctx, "accept", {"proposal_task_id": "outsider", "expected_version": 1, "reason": "wrong lineage"}
    ).startswith("ERROR:")


def test_missing_encryption_key_fails_closed_without_writing_plaintext(tmp_path, monkeypatch):
    from sqlalchemy import text

    ctx = make_plan_tool_ctx(tmp_path)
    monkeypatch.setenv("OUROBOROS_CREDENTIALS_ENCRYPTION_KEY", "")
    assert task_plan(ctx, "read").startswith("ERROR:")
    with runtime_persistence.current.engine.connect() as conn:
        stored = conn.execute(text("SELECT state_encrypted FROM task_plans WHERE root_task_id='root'")).scalar_one()
    assert "requirements" not in stored
