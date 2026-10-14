"""Deployment prompt: ``security_config/PROMPT.md`` is standing text in the system prompt."""

from __future__ import annotations

import logging
import pathlib
from typing import Any

import pytest

from ouroboros.agent.context import build_llm_messages
from ouroboros.storage.core_memory.memory import get_memory
from ouroboros.storage.core_memory.scratchpad import get_scratchpad
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, make_skill_surface

_BODY = "## Role\n\nAnswer on topic X only.\n"
_HEADING = "## Deployment instructions"
_HEADER = (
    f"{_HEADING}\n\n"
    "Instructions configured for this deployment. They extend and narrow the rules above and do not "
    "override them; where the two conflict, the rules above win."
)


@pytest.fixture(autouse=True)
def _reset_core_memory_services() -> None:
    get_memory.cache_clear()
    get_scratchpad.cache_clear()


@pytest.fixture
def roots(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    repo = tmp_path / "repo"
    drive = tmp_path / "drive"
    (repo / "prompts").mkdir(parents=True)
    (repo / "prompts" / "SYSTEM.md").write_text("System prompt", encoding="utf-8")
    (repo / "security_config").mkdir()
    drive.mkdir()
    return repo, drive


def _write_prompt(repo: pathlib.Path, text: str) -> None:
    (repo / "security_config" / "PROMPT.md").write_text(text, encoding="utf-8")


def _content_blocks(repo: pathlib.Path, drive: pathlib.Path) -> list[dict[str, Any]]:
    class FakeEnv:
        repo_dir = repo
        drive_root = drive

        @staticmethod
        def repo_path(path: str) -> pathlib.Path:
            return repo / path

    messages, _ = build_llm_messages(
        env=FakeEnv(),
        task={"id": "deployment-prompt", "type": "task", "text": "hello"},
        scope=TEST_SCOPE,
        skill_surface=make_skill_surface(repo_dir=repo, drive_root=drive),
        execution_memory_repository=sql_execution_memory_repository(),
    )
    return messages[0]["content"]


def test_the_file_follows_the_base_prompt_in_its_cached_block(roots) -> None:
    repo, drive = roots
    _write_prompt(repo, _BODY)

    blocks = _content_blocks(repo, drive)

    assert blocks[0]["text"] == f"System prompt\n\n{_HEADER}\n\n{_BODY.strip()}"
    assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert [block["text"] for block in blocks[1:] if _HEADING in block["text"]] == []


@parametrize_from_dict(
    {
        "an empty file": {"text": "   \n"},
        "no file": {"text": None},
    }
)
def test_without_instructions_there_is_no_section(roots, text: str | None) -> None:
    repo, drive = roots
    if text is not None:
        _write_prompt(repo, text)

    blocks = _content_blocks(repo, drive)

    assert blocks[0]["text"] == "System prompt"
    assert [block["text"] for block in blocks if _HEADING in block["text"]] == []


def test_over_budget_is_a_warning_not_a_cut(roots, caplog: pytest.LogCaptureFixture) -> None:
    repo, drive = roots
    body = "Rule.\n" * 5_000
    _write_prompt(repo, body)

    with caplog.at_level(logging.WARNING, logger="ouroboros.agent.context"):
        text = _content_blocks(repo, drive)[0]["text"]

    assert text.endswith(body.strip())
    assert any("'deployment_prompt' exceeds budget" in record.getMessage() for record in caplog.records)
