"""Policy engine tests."""

from __future__ import annotations

import pytest

from app.services.policy_engine import PolicyEngine
from app.utils.exceptions.common import PolicyDeniedException


def test_validate_exec_normalizes_cwd_and_preserves_allowlisted_env() -> None:
    decision = PolicyEngine().validate_exec(
        command=["python", "-V"],
        cwd="workspace/../workspace",
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
    )

    assert decision.cwd == "/workspace"
    assert decision.env == {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


def test_validate_exec_rejects_pythonpath_to_block_module_hijacking() -> None:
    with pytest.raises(PolicyDeniedException) as exc_info:
        PolicyEngine().validate_exec(
            command=["python", "-V"],
            cwd="/",
            env={"PYTHONPATH": "/workspace"},
        )

    assert exc_info.value.detail["details"]["forbiddenEnv"] == ["PYTHONPATH"]


def test_validate_exec_rejects_denied_command_token() -> None:
    with pytest.raises(PolicyDeniedException) as exc_info:
        PolicyEngine().validate_exec(
            command=["/usr/bin/sudo", "whoami"],
            cwd="/",
            env={},
        )

    assert exc_info.value.code == "policy-deny"
    assert exc_info.value.detail["details"]["token"] == "sudo"


def test_validate_exec_rejects_denied_token_inside_shell_payload() -> None:
    with pytest.raises(PolicyDeniedException) as exc_info:
        PolicyEngine().validate_exec(
            command=["/bin/sh", "-lc", "echo ok && sudo whoami"],
            cwd="/",
            env={},
        )

    assert exc_info.value.code == "policy-deny"
    assert exc_info.value.detail["details"]["token"] == "sudo"


def test_validate_exec_rejects_non_allowlisted_env() -> None:
    with pytest.raises(PolicyDeniedException) as exc_info:
        PolicyEngine().validate_exec(
            command=["python", "-V"],
            cwd="/",
            env={"SECRET_TOKEN": "hidden"},
        )

    assert exc_info.value.detail["details"]["forbiddenEnv"] == ["SECRET_TOKEN"]


def test_validate_file_path_rejects_traversal() -> None:
    with pytest.raises(PolicyDeniedException):
        PolicyEngine().validate_file_path("../../etc/passwd")
