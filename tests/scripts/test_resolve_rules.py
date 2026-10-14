import subprocess
import sys
from pathlib import Path

import pytest

from scripts.resolve_rules import matching_rules
from tests.helpers.parametrize import parametrize_from_dict

REPO_ROOT = Path(__file__).resolve().parents[2]


@parametrize_from_dict(
    {
        "shared_bootstrap": {
            "path": "multiuser_config/bootstrap.py",
            "expected": {"supervisor-process", "agent-runtime-process", "transport-process", "background-memory"},
        },
        "adapter": {"path": "ouroboros/agent/loop_adapters.py", "expected": {"agent-loop", "new-core"}},
        "skill_execution": {"path": "ouroboros/tools/skill_exec.py", "expected": {"tools-and-policy", "skills"}},
        "events": {"path": "supervisor/events.py", "expected": {"supervisor", "observability"}},
        "episode_store": {"path": "ouroboros/storage/task_episodes.py", "expected": {"agent-loop"}},
        "memory_tests": {
            "path": "tests/background_memory/test_memory_runtime.py",
            "expected": {"testing", "background-memory"},
        },
        "nested_transport": {"path": "multiuser_config/transport/browser/app.py", "expected": {"transport-process"}},
    }
)
def test_resolves_all_owners_even_when_the_edit_map_is_shorter(path: str, expected: set[str]) -> None:
    assert {rule.stem for rule in matching_rules([path], REPO_ROOT / ".rules")} == expected


def test_new_files_and_multiple_paths_resolve_without_reading_source(tmp_path: Path) -> None:
    for name, glob in (("package", "pkg/**"), ("python", "pkg/**/*.py"), ("root_only", "pkg/*.py")):
        (tmp_path / f"{name}.md").write_text(f'---\npaths:\n  - "{glob}"\n---\n')
    rules = matching_rules(["pkg/sub/new.py", "pkg/sub/other.py"], tmp_path)
    assert [rule.stem for rule in rules] == ["package", "python"]
    assert {rule.stem for rule in matching_rules(["pkg/new.py"], tmp_path)} == {"package", "python", "root_only"}


def test_malformed_frontmatter_is_refused_instead_of_silently_skipped(tmp_path: Path) -> None:
    (tmp_path / "broken.md").write_text('---\npath:\n  - "pkg/**"\n---\n')
    with pytest.raises(ValueError, match="broken.md"):
        matching_rules(["pkg/new.py"], tmp_path)


def test_cli_resolves_from_the_repository_even_when_called_elsewhere(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/resolve_rules.py"), "ouroboros/storage/task_episodes.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines() == [".rules/agent-loop.md"]
