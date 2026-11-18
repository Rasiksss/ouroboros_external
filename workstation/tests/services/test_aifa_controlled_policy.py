"""AIFA control-plane policy tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.aifa_controlled_policy import AifaControlledPolicy
from app.utils.exceptions.common import PolicyDeniedException

PIPELINE = "skills/aifa/scripts/aifa_pipeline.py"
ROUTER = "skills/task-router/scripts/route_task.py"


def _write_json(rootfs: Path, virtual_path: str, payload: dict[str, object]) -> None:
    target = rootfs / virtual_path.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")


def test_general_workspace_is_not_restricted(tmp_path: Path) -> None:
    _write_json(tmp_path, "/workspace/agent_mode_state.json", {"decision": "general"})
    AifaControlledPolicy().validate_exec(["python3", "custom.py"], rootfs_path=str(tmp_path))


def test_financial_mode_allows_init_before_run_state(tmp_path: Path) -> None:
    _write_json(tmp_path, "/workspace/agent_mode_state.json", {"decision": "financial"})
    AifaControlledPolicy().validate_exec(
        ["python3", PIPELINE, "--step", "init_run"],
        rootfs_path=str(tmp_path),
    )


def test_financial_mode_blocks_arbitrary_script(tmp_path: Path) -> None:
    _write_json(tmp_path, "/workspace/agent_mode_state.json", {"decision": "financial"})
    with pytest.raises(PolicyDeniedException) as exc_info:
        AifaControlledPolicy().validate_exec(
            ["python3", "custom.py"],
            rootfs_path=str(tmp_path),
        )
    assert exc_info.value.detail["details"]["retryWithArbitraryScript"] is False


def test_current_pipeline_step_is_allowed(tmp_path: Path) -> None:
    _write_json(tmp_path, "/workspace/agent_mode_state.json", {"decision": "financial"})
    _write_json(
        tmp_path,
        "/workspace/aifa_pipeline_state.json",
        {"next_step": "validate_classification", "next_command": "expected command"},
    )
    AifaControlledPolicy().validate_exec(
        ["python3", PIPELINE, "--step", "validate_classification"],
        rootfs_path=str(tmp_path),
    )


def test_wrong_pipeline_step_is_blocked_with_recovery_contract(tmp_path: Path) -> None:
    _write_json(tmp_path, "/workspace/agent_mode_state.json", {"decision": "financial"})
    _write_json(
        tmp_path,
        "/workspace/aifa_pipeline_state.json",
        {
            "next_step": "validate_classification",
            "next_command": "python3 expected.py",
            "allowed_model_artifacts": ["classification_candidate"],
        },
    )
    with pytest.raises(PolicyDeniedException) as exc_info:
        AifaControlledPolicy().validate_exec(
            ["python3", PIPELINE, "--step", "build_excel"],
            rootfs_path=str(tmp_path),
        )
    details = exc_info.value.detail["details"]
    assert details["nextStep"] == "validate_classification"
    assert details["nextCommand"] == "python3 expected.py"
    assert details["preferredSubmissionTool"] == "aifa_submit_artifact"


def test_submit_artifact_pseudo_step_has_specific_error(tmp_path: Path) -> None:
    _write_json(tmp_path, "/workspace/agent_mode_state.json", {"decision": "financial"})
    _write_json(tmp_path, "/workspace/aifa_pipeline_state.json", {"next_step": "validate_classification"})
    with pytest.raises(PolicyDeniedException) as exc_info:
        AifaControlledPolicy().validate_exec(
            ["python3", PIPELINE, "--step", "submit_artifact"],
            rootfs_path=str(tmp_path),
        )
    assert "not an aifa_pipeline.py step" in str(exc_info.value)


def test_canonical_router_command_is_allowed_without_aifa_state(tmp_path: Path) -> None:
    AifaControlledPolicy().validate_exec(
        [
            "python3",
            ROUTER,
            "--input",
            "task_router_input.json",
            "--output",
            "agent_mode_state.json",
        ],
        rootfs_path=str(tmp_path),
    )


def test_noncanonical_router_command_is_blocked(tmp_path: Path) -> None:
    with pytest.raises(PolicyDeniedException) as exc_info:
        AifaControlledPolicy().validate_exec(
            ["python3", ROUTER, "--input", ".task-router/input.json"],
            rootfs_path=str(tmp_path),
        )
    assert exc_info.value.detail["details"]["preferredTool"] == "task_router_submit_input"


def test_workspace_absolute_control_script_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PolicyDeniedException) as exc_info:
        AifaControlledPolicy().validate_exec(
            ["python3", f"/workspace/{PIPELINE}", "--step", "init_run"],
            rootfs_path=str(tmp_path),
        )
    assert exc_info.value.detail["details"]["retryWithRelativePath"] is True


def test_shell_control_operators_do_not_bypass_financial_policy(tmp_path: Path) -> None:
    _write_json(tmp_path, "/workspace/agent_mode_state.json", {"decision": "financial"})
    with pytest.raises(PolicyDeniedException):
        AifaControlledPolicy().validate_exec(
            ["/bin/sh", "-lc", f"python3 {PIPELINE} --step init_run && python3 custom.py"],
            rootfs_path=str(tmp_path),
        )
