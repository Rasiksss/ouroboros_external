"""Production-route and confinement tests for the skill_read tool.

The ``ToolRegistry`` these once drove is retired; the same behaviours are now reached
through the process wiring in ``tests/_shared`` — ``make_tool_env`` builds the catalog and
context the server lifespan builds, ``run_tool`` bridges a single dispatch call, and
``run_tool_calls`` drives a whole round the way the ``exec_tools`` node does. The
first-prompt schema set comes from ``bind_tool_surface`` rather than a registry method.
"""

from __future__ import annotations

import json
import os
import pathlib
import re

import pytest

from ouroboros.agent.task_graph import bind_tool_surface
from tests._shared import make_tool_env, run_tool, run_tool_calls


@pytest.fixture
def skill_roots(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[pathlib.Path, pathlib.Path]:
    repo = tmp_path / "repo"
    drive = tmp_path / "drive"
    repo.mkdir()
    drive.mkdir()
    monkeypatch.setattr(
        "ouroboros.skills.loader.load_settings",
        lambda: {
            "skills.allowBundled": True,
            "skills.load.extraDirs": [],
            "skills.load.watch": False,
            "skills.load.watchDebounceMs": 250,
            "agents.defaults.skills": None,
            "agents.list": [],
        },
    )
    return repo, drive


def _write_skill(
    repo: pathlib.Path,
    name: str,
    *,
    skill_type: str = "instruction",
    description: str = "Test skill documentation",
    directory: str | None = None,
    body: str = "# Test skill\n\nmanifest sentinel\n",
) -> pathlib.Path:
    skill_dir = repo / "skills" / (directory or name)
    skill_dir.mkdir(parents=True, exist_ok=True)
    extra = ""
    if skill_type == "script":
        extra = "runtime: python3\nscripts:\n  - name: scripts/run.py\n"
        script = skill_dir / "scripts" / "run.py"
        script.parent.mkdir()
        script.write_text("print('script sentinel')\n", encoding="utf-8")
    elif skill_type == "extension":
        extra = "entry: plugin.py\n"
        (skill_dir / "plugin.py").write_text(
            "def register(api):\n    return None\n",
            encoding="utf-8",
        )
    elif skill_type == "tool":
        extra = "entry: tool.py\n"
        (skill_dir / "tool.py").write_text(
            "def get_tools():\n    return []\n",
            encoding="utf-8",
        )
    manifest = skill_dir / "SKILL.md"
    manifest.write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: {skill_type}\n{extra}---\n{body}",
        encoding="utf-8",
    )
    return manifest


def _env(repo: pathlib.Path, drive: pathlib.Path):
    """Catalog + context wired like the server lifespan, over the test's roots."""
    return make_tool_env(repo, drive)


def _agent_tool_call(
    catalog,
    ctx,
    drive: pathlib.Path,
    *,
    skill: str,
    path: str = "",
    start_line: int = 1,
    max_lines: int = 2000,
    start_char: int | None = None,
    expected_errors: int = 0,
) -> str:
    from new_core.llm.types import ToolCall, ToolCallFunction

    arguments = {
        "skill": skill,
        "path": path,
        "start_line": start_line,
        "max_lines": max_lines,
    }
    if start_char is not None:
        arguments["start_char"] = start_char
    call = ToolCall(
        id=f"call-{skill}-{path or 'manifest'}",
        function=ToolCallFunction(
            name="skill_read",
            arguments=json.dumps(arguments),
        ),
    )
    messages: list[dict] = []
    trace = {"tool_calls": []}
    _schemas, active_names = bind_tool_surface(catalog, ctx, [])

    errors = run_tool_calls(
        [call],
        catalog,
        drive / "logs",
        "skill-read-task",
        messages,
        trace,
        lambda _text: None,
        ctx=ctx,
        active_tool_names=active_names,
    )

    assert errors == expected_errors
    assert trace["tool_calls"][-1]["tool"] == "skill_read"
    return str(messages[-1]["content"])


def test_skill_read_is_core_with_bounded_schema(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    from ouroboros.tools.catalog import TOOL_SPECS

    repo, drive = skill_roots
    catalog, ctx = _env(repo, drive)
    tool_schemas, _active = bind_tool_surface(catalog, ctx, [])
    schemas = {schema.name: schema for schema in tool_schemas}

    assert "skill_read" in schemas
    assert "continuation" in schemas["skill_read"].description.lower()
    assert schemas["skill_read"].parameters["required"] == ["skill"]
    properties = schemas["skill_read"].parameters["properties"]
    assert properties["path"]["default"] == ""
    assert properties["start_line"]["default"] == 1
    assert properties["start_line"]["minimum"] == 1
    assert properties["max_lines"]["default"] == 2000
    assert properties["max_lines"]["minimum"] == 1
    assert properties["max_lines"]["maximum"] == 2000
    assert properties["start_char"]["minimum"] == 1
    spec = TOOL_SPECS["skill_read"]
    assert spec.category == "execution"
    assert spec.surface == "core"
    assert spec.capability_class == "read_only"
    assert spec.parallel is True
    assert spec.result_limit == 80_000


def test_list_skills_discovery_mode_is_compact_and_cursor_paginated(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    from ouroboros.agent.loop_tool_execution import ToolCallExecutor

    _truncate_tool_result = ToolCallExecutor._truncate_tool_result

    repo, drive = skill_roots
    expected_names = {f"catalog-{index:02d}" for index in range(25)}
    for name in expected_names:
        _write_skill(
            repo,
            name,
            description=f"Discovery description for {name} " + ("D" * 260),
        )
    catalog, ctx = _env(repo, drive)
    descriptor = catalog.find("list_skills")
    assert descriptor is not None
    schema = descriptor.schema
    properties = schema.parameters["properties"]
    assert properties["view"]["enum"] == ["full", "discovery"]
    assert properties["cursor"]["minimum"] == 0
    assert properties["limit"]["maximum"] == 15

    first_result = run_tool(
        catalog,
        ctx,
        "list_skills",
        {"view": "discovery", "cursor": 0, "limit": 15},
    )

    assert first_result.startswith("{"), first_result
    first_model_result = _truncate_tool_result(
        first_result,
        tool_name="list_skills",
        tool_args={"view": "discovery", "cursor": 0, "limit": 15},
    )
    assert "... (truncated from" not in first_model_result
    first_page = json.loads(first_model_result)
    assert first_page["view"] == "discovery"
    assert first_page["cursor"] == 0
    assert first_page["total"] == 25
    assert len(first_page["skills"]) == 15
    assert first_page["next_cursor"] == 15

    second_result = run_tool(
        catalog,
        ctx,
        "list_skills",
        {
            "view": "discovery",
            "cursor": first_page["next_cursor"],
            "limit": 15,
        },
    )
    second_model_result = _truncate_tool_result(
        second_result,
        tool_name="list_skills",
        tool_args={
            "view": "discovery",
            "cursor": first_page["next_cursor"],
            "limit": 15,
        },
    )
    assert "... (truncated from" not in second_model_result
    second_page = json.loads(second_model_result)
    assert len(second_page["skills"]) == 10
    assert second_page["next_cursor"] is None
    observed_names = {item["name"] for item in first_page["skills"] + second_page["skills"]}
    assert observed_names == expected_names
    assert all(set(item) == {"name", "description"} for item in first_page["skills"] + second_page["skills"])


def test_list_skills_default_keeps_full_administrative_status(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    repo, drive = skill_roots
    _write_skill(repo, "full-status")
    catalog, ctx = _env(repo, drive)
    payload = json.loads(run_tool(catalog, ctx, "list_skills", {}))

    row = next(item for item in payload["skills"] if item["name"] == "full-status")
    assert row["selected"] is True
    assert row["eligible"] is True
    assert "location" in row


@pytest.mark.parametrize("skill_type", ["instruction", "script", "extension", "tool"])
def test_agent_route_reads_manifest_for_every_selected_eligible_skill_type(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
    skill_type: str,
) -> None:
    repo, drive = skill_roots
    name = f"{skill_type}-docs"
    manifest = _write_skill(repo, name, skill_type=skill_type)
    catalog, ctx = _env(repo, drive)

    result = _agent_tool_call(catalog, ctx, drive, skill=name)

    assert result.startswith(f"# skill:{name}/SKILL.md — lines 1")
    assert "manifest sentinel" in result
    assert str(manifest.parent) not in result


def test_agent_route_reads_nested_reference(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    repo, drive = skill_roots
    manifest = _write_skill(repo, "nested-docs")
    reference = manifest.parent / "references" / "providers" / "schema.json"
    reference.parent.mkdir(parents=True)
    reference.write_text('{"reference": "nested sentinel"}\n', encoding="utf-8")
    catalog, ctx = _env(repo, drive)

    result = _agent_tool_call(
        catalog,
        ctx,
        drive,
        skill="nested-docs",
        path="references/providers/schema.json",
    )

    assert result.startswith("# skill:nested-docs/references/providers/schema.json — lines 1")
    assert "nested sentinel" in result
    assert str(manifest.parent) not in result


def test_skill_read_paginates_deterministically_and_clamps_handler_arguments(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    repo, drive = skill_roots
    manifest = _write_skill(repo, "paged")
    reference = manifest.parent / "references" / "long.txt"
    reference.parent.mkdir()
    reference.write_text(
        "".join(f"line {line}\n" for line in range(1, 2505)),
        encoding="utf-8",
    )
    catalog, ctx = _env(repo, drive)

    result = run_tool(
        catalog,
        ctx,
        "skill_read",
        {
            "skill": "paged",
            "path": "references/long.txt",
            "start_line": 200,
            "max_lines": 500,
        },
    )
    clamped = run_tool(
        catalog,
        ctx,
        "skill_read",
        {
            "skill": "paged",
            "path": "references/long.txt",
            "start_line": -50,
            "max_lines": 50_000,
        },
    )
    continued = run_tool(
        catalog,
        ctx,
        "skill_read",
        {
            "skill": "paged",
            "path": "references/long.txt",
            "start_line": 2001,
        },
    )

    assert result.startswith("# skill:paged/references/long.txt — lines 200–699 of 2504")
    assert "\nline 200\n" in result
    assert result.endswith("line 699\n\n# More content available; continue with start_line=700, max_lines=500.\n")
    assert clamped.startswith("# skill:paged/references/long.txt — lines 1–2000 of 2504")
    assert clamped.endswith("line 2000\n\n# More content available; continue with start_line=2001, max_lines=2000.\n")
    assert continued.startswith("# skill:paged/references/long.txt — lines 2001–2504 of 2504")
    assert continued.endswith("line 2504\n")
    assert "More content available" not in continued


def test_agent_route_long_line_uses_resumable_character_pages_without_truncation(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    repo, drive = skill_roots
    manifest = _write_skill(repo, "long-line")
    reference = manifest.parent / "references" / "long.txt"
    reference.parent.mkdir()
    content = ("A" * 100_000) + "\nnext line sentinel\n"
    reference.write_text(content, encoding="utf-8")
    catalog, ctx = _env(repo, drive)

    first = _agent_tool_call(
        catalog,
        ctx,
        drive,
        skill="long-line",
        path="references/long.txt",
    )
    assert len(first) <= 80_000
    assert "... (truncated from" not in first
    match = re.search(r"continue with start_char=(\d+)", first)
    assert match is not None

    second = _agent_tool_call(
        catalog,
        ctx,
        drive,
        skill="long-line",
        path="references/long.txt",
        start_char=int(match.group(1)),
    )

    def _page_body(page: str) -> str:
        body = page.split("\n", 1)[1]
        return body.rsplit("\n# More content available;", 1)[0]

    assert len(second) <= 80_000
    assert "... (truncated from" not in second
    assert "next line sentinel" in second
    assert _page_body(first) + _page_body(second) == content


@pytest.mark.parametrize(
    "blocked_path",
    [
        "../other/SKILL.md",
        "references/../../README.md",
        "references/../SKILL.md",
        "scripts/run.py",
        "assets/template.md",
        "plugin.py",
        "tool.py",
        r"references\secret.md",
    ],
)
def test_skill_read_blocks_non_document_paths(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
    blocked_path: str,
) -> None:
    repo, drive = skill_roots
    manifest = _write_skill(repo, "confined")
    (manifest.parent / "scripts").mkdir()
    (manifest.parent / "scripts" / "run.py").write_text("secret\n", encoding="utf-8")
    (manifest.parent / "assets").mkdir()
    (manifest.parent / "assets" / "template.md").write_text("secret\n", encoding="utf-8")
    (manifest.parent / "plugin.py").write_text("secret\n", encoding="utf-8")
    (manifest.parent / "tool.py").write_text("secret\n", encoding="utf-8")
    catalog, ctx = _env(repo, drive)

    result = run_tool(
        catalog,
        ctx,
        "skill_read",
        {"skill": "confined", "path": blocked_path},
    )

    assert "SKILL_READ_BLOCKED" in result
    assert "secret" not in result
    assert str(repo) not in result


@pytest.mark.parametrize(
    "blocked_path",
    [
        "references/enabled.json",
        "references/grants.json",
        "references/deps.json",
        "references/review.json",
        "references/auth_token.json",
        "references/.self_authored.json",
        "references/.ouroboros_env/state.txt",
    ],
)
def test_skill_read_blocks_owner_and_control_files_below_references(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
    blocked_path: str,
) -> None:
    repo, drive = skill_roots
    manifest = _write_skill(repo, "control-files")
    target = manifest.parent.joinpath(*pathlib.PurePosixPath(blocked_path).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("control secret\n", encoding="utf-8")
    catalog, ctx = _env(repo, drive)

    result = run_tool(
        catalog,
        ctx,
        "skill_read",
        {"skill": "control-files", "path": blocked_path},
    )

    assert "SKILL_READ_BLOCKED" in result
    assert "control secret" not in result
    assert str(repo) not in result


def test_owner_state_filename_policy_is_shared_by_both_enforcement_points(
    tmp_path: pathlib.Path,
) -> None:
    from ouroboros.contracts import skill_payload_policy
    from ouroboros.tools import core, skills

    owner_names = skill_payload_policy.SKILL_OWNER_STATE_FILENAMES

    data_root = tmp_path / "data"
    for filename in owner_names:
        owner_target = data_root / "state" / "skills" / "demo" / filename
        assert core._is_skill_owner_state_target(owner_target, data_root)
        assert skills._skill_document_path(f"references/{filename}") is None


def test_skill_read_blocks_absolute_repository_path_without_disclosing_it(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    repo, drive = skill_roots
    _write_skill(repo, "absolute")
    secret = repo / "private.txt"
    secret.write_text("repository secret\n", encoding="utf-8")
    catalog, ctx = _env(repo, drive)

    result = run_tool(
        catalog,
        ctx,
        "skill_read",
        {"skill": "absolute", "path": str(secret)},
    )

    assert "SKILL_READ_BLOCKED" in result
    assert str(secret) not in result
    assert "repository secret" not in result


def test_agent_route_blocks_embedded_nul_path_without_tool_error(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    repo, drive = skill_roots
    manifest = _write_skill(repo, "malformed-path")
    (manifest.parent / "references").mkdir()
    catalog, ctx = _env(repo, drive)

    result = _agent_tool_call(
        catalog,
        ctx,
        drive,
        skill="malformed-path",
        path="references/bad\x00.md",
        expected_errors=1,
    )

    assert "SKILL_READ_BLOCKED" in result
    assert "TOOL_ERROR" not in result
    assert "embedded null byte" not in result
    assert str(manifest.parent) not in result


def test_skill_read_blocks_symlink_escapes_and_cross_skill_links(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    repo, drive = skill_roots
    first = _write_skill(repo, "first")
    second = _write_skill(repo, "second", body="# Second\n\nneighbor secret\n")
    references = first.parent / "references"
    references.mkdir()
    outside = repo / "outside.md"
    outside.write_text("outside secret\n", encoding="utf-8")
    (references / "outside.md").symlink_to(outside)
    (references / "neighbor.md").symlink_to(second)
    catalog, ctx = _env(repo, drive)

    outside_result = run_tool(
        catalog,
        ctx,
        "skill_read",
        {"skill": "first", "path": "references/outside.md"},
    )
    neighbor_result = run_tool(
        catalog,
        ctx,
        "skill_read",
        {"skill": "first", "path": "references/neighbor.md"},
    )

    for result in (outside_result, neighbor_result):
        assert "SKILL_READ_BLOCKED" in result
        assert "secret" not in result
        assert str(repo) not in result


def test_skill_read_rejects_directory_special_file_missing_and_invalid_utf8(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    repo, drive = skill_roots
    manifest = _write_skill(repo, "file-kinds")
    references = manifest.parent / "references"
    nested = references / "nested"
    nested.mkdir(parents=True)
    invalid = references / "invalid.bin"
    invalid.write_bytes(b"\xff\xfe\x00")
    fifo = references / "pipe"
    os.mkfifo(fifo)
    catalog, ctx = _env(repo, drive)

    directory_result = run_tool(
        catalog,
        ctx,
        "skill_read",
        {"skill": "file-kinds", "path": "references/nested"},
    )
    fifo_result = run_tool(
        catalog,
        ctx,
        "skill_read",
        {"skill": "file-kinds", "path": "references/pipe"},
    )
    missing_result = run_tool(
        catalog,
        ctx,
        "skill_read",
        {"skill": "file-kinds", "path": "references/missing.md"},
    )
    invalid_result = run_tool(
        catalog,
        ctx,
        "skill_read",
        {"skill": "file-kinds", "path": "references/invalid.bin"},
    )

    assert "SKILL_READ_BLOCKED" in directory_result
    assert "SKILL_READ_BLOCKED" in fifo_result
    assert "SKILL_READ_FILE_NOT_FOUND" in missing_result
    assert "SKILL_READ_NOT_TEXT" in invalid_result
    for result in (directory_result, fifo_result, missing_result, invalid_result):
        assert str(manifest.parent) not in result


def test_skill_read_rejects_unknown_ineligible_and_colliding_skills(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    repo, drive = skill_roots
    _write_skill(repo, "ineligible", description="")
    _write_skill(repo, "collision", directory="collision-a")
    _write_skill(repo, "collision", directory="collision-b")
    catalog, ctx = _env(repo, drive)

    unknown = run_tool(catalog, ctx, "skill_read", {"skill": "unknown"})
    ineligible = run_tool(catalog, ctx, "skill_read", {"skill": "ineligible"})
    collision = run_tool(catalog, ctx, "skill_read", {"skill": "collision"})

    assert "SKILL_READ_NOT_FOUND" in unknown
    assert "SKILL_READ_NOT_ELIGIBLE" in ineligible
    assert "SKILL_READ_NOT_ELIGIBLE" in collision
    assert str(repo) not in unknown + ineligible + collision


def test_bundled_skill_instructions_use_readable_skill_paths(tmp_path: pathlib.Path) -> None:
    repo = pathlib.Path(__file__).resolve().parents[1]
    system_prompt = (repo / "prompts" / "SYSTEM.md").read_text(encoding="utf-8")
    navigator_manifest = (repo / "skills" / "navigator-bi" / "SKILL.md").read_text(encoding="utf-8")

    assert 'skill_read(skill="calendar-orientation")' in system_prompt
    # Navigator routing lives in the skill manifest and tool description, not in
    # the system prompt (a prompt-level block would override the universal skill).
    assert "navigator" not in system_prompt.lower()
    assert "repo_read" not in navigator_manifest
    # The manifest documents the per-model reference pattern and a concrete call.
    assert 'skill_read(skill="navigator-bi", path="references/<nID>.md")' in navigator_manifest
    assert 'skill_read(skill="navigator-bi", path="references/1000000956.md")' in navigator_manifest

    catalog, ctx = _env(repo, tmp_path / "drive")
    reference_files = sorted((repo / "skills" / "navigator-bi" / "references").glob("*.md"))
    assert reference_files, "navigator-bi ships per-model reference files"
    for reference in reference_files:
        result = run_tool(
            catalog,
            ctx,
            "skill_read",
            {"skill": "navigator-bi", "path": f"references/{reference.name}"},
        )
        assert "SKILL_READ_" not in result
