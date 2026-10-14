from __future__ import annotations

import pytest

from ouroboros.egress.workstation.paths import (
    INBOUND_SUBDIR,
    WorkstationPathError,
    normalize_workspace_path,
    to_command_relative,
)

ROOT = "/workspace"


def test_already_canonical_path_is_used_as_is() -> None:
    assert normalize_workspace_path("/workspace/foo.txt", workspace_root=ROOT) == "/workspace/foo.txt"
    assert normalize_workspace_path("/workspace", workspace_root=ROOT) == "/workspace"


def test_relative_path_resolves_under_the_root() -> None:
    assert normalize_workspace_path("foo/bar.txt", workspace_root=ROOT) == "/workspace/foo/bar.txt"


def test_relative_path_matching_root_name_is_not_mistaken_for_the_root() -> None:
    # A legitimate subdirectory named "workspace" must not collapse into the root itself.
    assert normalize_workspace_path("workspace/inner.txt", workspace_root=ROOT) == "/workspace/workspace/inner.txt"


def test_absolute_host_looking_path_is_rewritten_by_last_root_segment() -> None:
    assert normalize_workspace_path("/home/user/workspace/foo.txt", workspace_root=ROOT) == "/workspace/foo.txt"


def test_absolute_path_uses_last_occurrence_of_root_name() -> None:
    # Two segments literally named "workspace" — the last one is the real root.
    assert normalize_workspace_path("/mnt/workspace/workspace/data.csv", workspace_root=ROOT) == "/workspace/data.csv"


def test_absolute_path_without_root_segment_is_rejected() -> None:
    with pytest.raises(WorkstationPathError):
        normalize_workspace_path("/etc/passwd", workspace_root=ROOT)


@pytest.mark.parametrize(
    "raw",
    [
        "../../etc/passwd",
        "/workspace/../etc/passwd",
        "/home/user/workspace/../../etc/passwd",
    ],
)
def test_dot_dot_escape_is_always_rejected(raw: str) -> None:
    with pytest.raises(WorkstationPathError):
        normalize_workspace_path(raw, workspace_root=ROOT)


def test_empty_path_is_rejected() -> None:
    with pytest.raises(WorkstationPathError):
        normalize_workspace_path("", workspace_root=ROOT)
    with pytest.raises(WorkstationPathError):
        normalize_workspace_path("   ", workspace_root=ROOT)


def test_to_command_relative_strips_the_leading_root() -> None:
    assert to_command_relative("/workspace/foo/bar.txt", workspace_root=ROOT) == "foo/bar.txt"
    assert to_command_relative("foo/bar.txt", workspace_root=ROOT) == "foo/bar.txt"


def test_to_command_relative_root_itself_is_dot() -> None:
    assert to_command_relative("/workspace", workspace_root=ROOT) == "."


def test_to_command_relative_works_for_non_default_root() -> None:
    assert to_command_relative("/sandbox/a.py", workspace_root="/sandbox") == "a.py"


def test_inbound_subdir_constant_is_stable() -> None:
    assert INBOUND_SUBDIR == "media/inbound"
