"""Production-route and confinement tests for the skill_read tool.

The ``ToolRegistry`` these once drove is retired; the same behaviours are now reached
through the process wiring in ``tests/helpers/shared.py`` — ``make_tool_env`` builds the catalog and
context the server lifespan builds, ``run_tool`` bridges a single dispatch call, and
``run_tool_calls`` drives a whole round the way the ``exec_tools`` node does. The
first-prompt schema set comes from ``bind_tool_surface`` rather than a registry method.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
from unittest.mock import Mock

import pytest

from new_core.llm.types import LLMFailure, LLMMessage
from new_core.loop import nodes
from new_core.skills.models import SkillId
from new_core.skills.surface import SkillSurface
from ouroboros.agent.context import active_skill_instructions, build_llm_messages, retain_skill_page
from ouroboros.agent.task_graph import bind_tool_surface
from ouroboros.agent_runtime.cancellation import EventCancellation
from ouroboros.tools.skill_exec import _handle_skill_exec
from ouroboros.tools.skills import _check_skills, _list_skills, _skill_read
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import (
    TEST_SCOPE,
    make_skill_availability,
    make_skill_catalog,
    make_skill_surface,
    make_tool_ctx,
    make_tool_env,
    run_tool,
    run_tool_calls,
)


def test_missing_surface_fails_closed_for_task_facing_skill_tools(tmp_path: pathlib.Path) -> None:
    ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path, skill_surface=None, scope=TEST_SCOPE)

    assert _list_skills(ctx) == "⚠️ SKILLS_UNAVAILABLE: skill surface is not configured"
    assert _skill_read(ctx, "alpha") == "⚠️ SKILLS_UNAVAILABLE: skill surface is not configured"
    assert _handle_skill_exec(ctx, skill="alpha", script="run.py") == (
        "⚠️ SKILLS_UNAVAILABLE: skill surface is not configured"
    )


@pytest.fixture
def skill_roots(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[pathlib.Path, pathlib.Path]:
    repo = tmp_path / "repo"
    drive = tmp_path / "drive"
    repo.mkdir()
    drive.mkdir()
    monkeypatch.setenv("SELFHOSTED_LLM_BASE_URL", "http://localhost:9999/v1")
    monkeypatch.setattr("ouroboros.agent.loop_llm_call.provider_circuit_is_open", lambda *a, **k: False)
    monkeypatch.setattr(
        "tests.helpers.shared.skill_test_settings",
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
    script_name: str = "scripts/run.py",
    entry_name: str = "",
) -> pathlib.Path:
    skill_dir = repo / "skills" / (directory or name)
    skill_dir.mkdir(parents=True, exist_ok=True)
    extra = ""
    if skill_type == "script":
        extra = f"runtime: python3\nscripts:\n  - name: {script_name}\n"
        script = skill_dir.joinpath(*pathlib.PurePosixPath(script_name).parts)
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("print('script sentinel')\n", encoding="utf-8")
    elif skill_type == "extension":
        extra = f"entry: {entry_name or 'plugin.py'}\n"
        (skill_dir / "plugin.py").write_text(
            "def register(api):\n    return None\n",
            encoding="utf-8",
        )
    elif skill_type == "tool":
        extra = f"entry: {entry_name or 'tool.py'}\n"
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
    descriptor = catalog.find("list_skills", TEST_SCOPE)
    assert descriptor is not None
    schema = descriptor.schema
    properties = schema.parameters["properties"]
    assert "agent_name" not in properties
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
    assert all(
        set(item) == {"skill_id", "name", "description"} for item in first_page["skills"] + second_page["skills"]
    )
    assert {item["skill_id"] for item in first_page["skills"] + second_page["skills"]} == {
        f"distribution:{name}" for name in expected_names
    }


def test_discovery_keeps_a_long_canonical_skill_id_addressable(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    repo, drive = skill_roots
    local_id = "/".join(["x" * 50, "y" * 50, "z" * 50, "w" * 50, "leaf"])
    expected_id = f"distribution:{local_id}"
    _write_skill(repo, "long-skill", directory=local_id)
    catalog, ctx = _env(repo, drive)

    discovery = json.loads(run_tool(catalog, ctx, "list_skills", {"view": "discovery"}))

    assert discovery["skills"][0]["skill_id"] == expected_id
    assert "manifest sentinel" in run_tool(catalog, ctx, "skill_read", {"skill": expected_id})


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


def test_task_selection_hides_a_non_enabled_skill_from_every_task_facing_path(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, drive = skill_roots
    allowed_manifest = _write_skill(repo, "allowed", body="allowed sentinel\n")
    denied_manifest = _write_skill(repo, "denied", body="denied sentinel\n")
    _catalog, ctx = _env(repo, drive)
    task_surface = SkillSurface(
        snapshot=make_skill_catalog(repo_dir=repo, drive_root=drive).snapshot(),
        availability=make_skill_availability(),
        agent_name=TEST_SCOPE.agent_id,
        task_selection=frozenset({SkillId.parse("distribution:allowed")}),
    )
    ctx.skill_surface = task_surface

    messages, _ = build_llm_messages(
        ctx,
        {},
        ctx.scope,
        tool_ctx=ctx,
        skill_surface=task_surface,
        execution_memory_repository=sql_execution_memory_repository(),
    )
    prompt = json.dumps(messages, ensure_ascii=False)
    full = json.loads(_list_skills(ctx))
    discovery = json.loads(_list_skills(ctx, view="discovery"))
    checked = json.loads(_check_skills(ctx))

    original_read_text = pathlib.Path.read_text

    def guarded_read_text(path: pathlib.Path, *args, **kwargs):
        if path == denied_manifest:
            raise AssertionError("a non-enabled skill payload was opened")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", guarded_read_text)
    denied = _skill_read(ctx, skill="distribution:denied")

    assert "distribution:allowed" in prompt
    assert "distribution:denied" not in prompt
    assert [row["skill_id"] for row in full["skills"]] == ["distribution:allowed"]
    assert [row["skill_id"] for row in discovery["skills"]] == ["distribution:allowed"]
    assert [row["skill_id"] for row in checked["skills"]] == ["distribution:allowed"]
    assert "SKILL_READ_NOT_FOUND" in denied
    assert allowed_manifest.read_text(encoding="utf-8")


@pytest.mark.parametrize("skill_type", ["instruction", "script", "extension", "tool"])
def test_agent_route_reads_manifest_for_every_selected_eligible_skill_type(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
    skill_type: str,
) -> None:
    repo, drive = skill_roots
    name = f"{skill_type}-docs"
    manifest = _write_skill(repo, name, skill_type=skill_type)
    catalog, ctx = _env(repo, drive)

    surface = ctx.skill_surface
    if surface is None:
        pytest.fail("_env must compose a skill surface")
    ctx.messages, _ = build_llm_messages(
        ctx,
        {},
        ctx.scope,
        tool_ctx=ctx,
        skill_surface=surface,
        execution_memory_repository=sql_execution_memory_repository(),
    )
    skill_id = f"distribution:{name}"
    result = _agent_tool_call(catalog, ctx, drive, skill=skill_id)
    assert active_skill_instructions(ctx).count(manifest.read_text()) == 1

    assert f"# skill:{name}/SKILL.md — lines 1" in result
    assert "manifest sentinel" not in result
    assert manifest.read_text() in run_tool(catalog, ctx, "skill_read", {"skill": skill_id})
    assert _skill_read(ctx, skill=skill_id).skill_page.page.body == manifest.read_text()
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
        skill="distribution:nested-docs",
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
            "skill": "distribution:paged",
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
            "skill": "distribution:paged",
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
            "skill": "distribution:paged",
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
        skill="distribution:long-line",
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
        skill="distribution:long-line",
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


def test_agent_route_reads_any_document_the_manifest_names(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    repo, drive = skill_roots
    package = repo / "skills" / "pilot-skill"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\n"
        "name: pilot-skill\n"
        "description: Pilot package whose resources sit in author-named directories.\n"
        "---\n\n"
        "# Pilot\n\nRead design_doc.md, then examples/input-output.json.\n",
        encoding="utf-8",
    )
    documents = {
        "design_doc.md": "design sentinel",
        "examples/input-output.json": '{"example": "json sentinel"}',
        "reference-materials/policy.txt": "policy sentinel",
    }
    for rel, text in documents.items():
        target = package.joinpath(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
    (package / "templates").mkdir()
    (package / "templates" / "report.docx").write_bytes(b"PK\x03\x04\xff\xfe")
    catalog, ctx = _env(repo, drive)

    for rel, text in documents.items():
        result = _agent_tool_call(catalog, ctx, drive, skill="distribution:pilot-skill", path=rel)
        assert result.startswith(f"# skill:pilot-skill/{rel} — lines 1")
        assert text in result
        assert str(package) not in result

    binary = _agent_tool_call(
        catalog, ctx, drive, skill="distribution:pilot-skill", path="templates/report.docx", expected_errors=1
    )
    assert "SKILL_READ_NOT_TEXT" in binary


@parametrize_from_dict(
    {
        "parent escape": {"skill_type": "instruction", "blocked_path": "../other/SKILL.md"},
        "parent escape mid-path": {"skill_type": "instruction", "blocked_path": "examples/../../README.md"},
        "traversal back to the manifest": {"skill_type": "instruction", "blocked_path": "examples/../SKILL.md"},
        "scripts directory": {"skill_type": "instruction", "blocked_path": "scripts/run.py"},
        "scripts directory, capitalised": {"skill_type": "instruction", "blocked_path": "Scripts/run.py"},
        "dot path": {"skill_type": "instruction", "blocked_path": "."},
        "dot-slash path": {"skill_type": "instruction", "blocked_path": "./"},
        "backslash separator": {"skill_type": "instruction", "blocked_path": r"examples\secret.md"},
        "extension entry": {"skill_type": "extension", "blocked_path": "plugin.py"},
        "extension entry, capitalised": {"skill_type": "extension", "blocked_path": "Plugin.py"},
        "tool entry": {"skill_type": "tool", "blocked_path": "tool.py"},
        "tool entry, capitalised": {"skill_type": "tool", "blocked_path": "Tool.py"},
    }
)
def test_skill_read_hides_executable_surfaces_and_escapes(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
    skill_type: str,
    blocked_path: str,
) -> None:
    repo, drive = skill_roots
    manifest = _write_skill(repo, "confined", skill_type=skill_type)
    (manifest.parent / "scripts").mkdir()
    (manifest.parent / "scripts" / "run.py").write_text("secret\n", encoding="utf-8")
    catalog, ctx = _env(repo, drive)

    result = run_tool(
        catalog,
        ctx,
        "skill_read",
        {"skill": "distribution:confined", "path": blocked_path},
    )

    assert "SKILL_READ_BLOCKED" in result
    assert "secret" not in result
    assert str(repo) not in result


def test_skill_read_blocks_a_same_package_symlink_that_targets_a_hidden_file(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    repo, drive = skill_roots
    manifest = _write_skill(repo, "symlinked")
    scripts_dir = manifest.parent / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "run.py").write_text("SECRET_TOKEN = 'sk-should-not-be-readable'\n", encoding="utf-8")
    docs_dir = manifest.parent / "docs"
    docs_dir.mkdir()
    (docs_dir / "tutorial.md").symlink_to(pathlib.Path("..") / "scripts" / "run.py")
    catalog, ctx = _env(repo, drive)

    result = run_tool(catalog, ctx, "skill_read", {"skill": "distribution:symlinked", "path": "docs/tutorial.md"})

    assert "SKILL_READ_BLOCKED" in result
    assert "SECRET_TOKEN" not in result


def test_skill_read_pages_a_document_larger_than_the_manifest_ceiling(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    repo, drive = skill_roots
    manifest = _write_skill(repo, "bulky")
    (manifest.parent / "reference.md").write_text("sentinel line\n" * 40_000, encoding="utf-8")
    catalog, ctx = _env(repo, drive)

    result = run_tool(catalog, ctx, "skill_read", {"skill": "distribution:bulky", "path": "reference.md"})

    assert "SKILL_READ_BLOCKED" not in result
    assert "sentinel line" in result
    assert "More content available" in result


def test_skill_read_hides_a_companion_process_command(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    repo, drive = skill_roots
    package = repo / "skills" / "companioned"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\n"
        "name: companioned\n"
        "description: Extension that registers a companion process.\n"
        "type: extension\n"
        "entry: plugin.py\n"
        "companion_processes:\n"
        "  - name: worker\n"
        "    runtime: python3\n"
        "    command: [python3, companion.py]\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )
    (package / "plugin.py").write_text("def register(api):\n    return None\n", encoding="utf-8")
    (package / "companion.py").write_text("SECRET_COMPANION = 'a process that actually runs'\n", encoding="utf-8")
    catalog, ctx = _env(repo, drive)

    result = run_tool(catalog, ctx, "skill_read", {"skill": "distribution:companioned", "path": "companion.py"})

    assert "SKILL_READ_BLOCKED" in result
    assert "SECRET_COMPANION" not in result


def test_skill_read_hides_an_entry_module_declared_with_a_leading_dot(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    repo, drive = skill_roots
    manifest = _write_skill(repo, "dot-entry", skill_type="extension", entry_name="./plugin.py")
    (manifest.parent / "plugin.py").write_text("SECRET_ENTRY = 'loaded in process'\n", encoding="utf-8")
    catalog, ctx = _env(repo, drive)

    result = run_tool(catalog, ctx, "skill_read", {"skill": "distribution:dot-entry", "path": "plugin.py"})

    assert "SKILL_READ_BLOCKED" in result
    assert "SECRET_ENTRY" not in result


def test_skill_read_hides_the_file_a_declared_script_symlink_points_at(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    repo, drive = skill_roots
    manifest = _write_skill(repo, "symlinked-script", skill_type="script")
    lib_dir = manifest.parent / "lib"
    lib_dir.mkdir()
    (lib_dir / "impl.py").write_text("SECRET_EXECUTED = 'the bytes skill_exec runs'\n", encoding="utf-8")
    declared = manifest.parent / "scripts" / "run.py"
    declared.unlink()
    declared.symlink_to(pathlib.Path("..") / "lib" / "impl.py")
    catalog, ctx = _env(repo, drive)

    result = run_tool(catalog, ctx, "skill_read", {"skill": "distribution:symlinked-script", "path": "lib/impl.py"})

    assert "SKILL_READ_BLOCKED" in result
    assert "SECRET_EXECUTED" not in result


def test_skill_read_hides_repository_metadata_the_loader_never_reviews(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    repo, drive = skill_roots
    manifest = _write_skill(repo, "cloned")
    git_dir = manifest.parent / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(
        '[remote "origin"]\n  url = https://oauth2:glpat-SHOULD-NOT-LEAK@example.invalid/x.git\n',
        encoding="utf-8",
    )
    catalog, ctx = _env(repo, drive)

    result = run_tool(catalog, ctx, "skill_read", {"skill": "distribution:cloned", "path": ".git/config"})

    assert "SKILL_READ_BLOCKED" in result
    assert "glpat-SHOULD-NOT-LEAK" not in result


def test_skill_read_hides_a_declared_script_whose_name_is_not_under_scripts(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    repo, drive = skill_roots
    manifest = _write_skill(repo, "nested-script", skill_type="script", script_name="helpers/run.py")
    (manifest.parent / "helpers" / "run.py").write_text("SECRET = 'nested script source'\n", encoding="utf-8")
    catalog, ctx = _env(repo, drive)

    result = run_tool(catalog, ctx, "skill_read", {"skill": "distribution:nested-script", "path": "helpers/run.py"})

    assert "SKILL_READ_BLOCKED" in result
    assert "SECRET" not in result


def test_document_named_like_an_entry_module_is_readable_without_a_declared_entry(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    repo, drive = skill_roots
    manifest = _write_skill(repo, "lookalike")
    (manifest.parent / "plugin.py").write_text("not an entry, just a document\n", encoding="utf-8")
    catalog, ctx = _env(repo, drive)

    result = _agent_tool_call(catalog, ctx, drive, skill="distribution:lookalike", path="plugin.py")

    assert result.startswith("# skill:lookalike/plugin.py — lines 1")
    assert "not an entry, just a document" in result


@pytest.mark.parametrize(
    "blocked_path",
    [
        ".ouroboros_env/state.txt",
        "agents/gigaagent.yaml",
        "AGENTS/GIGAAGENT.YAML",
    ],
)
def test_skill_read_blocks_control_files_anywhere_in_the_package(
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
        {"skill": "distribution:control-files", "path": blocked_path},
    )

    assert "SKILL_READ_BLOCKED" in result
    assert "control secret" not in result
    assert str(repo) not in result


def test_desktop_owner_state_filename_policy_is_removed() -> None:
    from ouroboros.contracts import skill_payload_policy

    assert not hasattr(skill_payload_policy, "SKILL_OWNER_STATE_FILENAMES")


def test_old_review_state_name_is_an_ordinary_skill_document(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    repo, drive = skill_roots
    manifest = _write_skill(repo, "review-guide")
    document = manifest.parent / "references" / "review.json"
    document.parent.mkdir()
    document.write_text('{"guidance": "review this"}\n', encoding="utf-8")
    catalog, ctx = _env(repo, drive)

    result = run_tool(
        catalog,
        ctx,
        "skill_read",
        {"skill": "distribution:review-guide", "path": "references/review.json"},
    )

    assert '"guidance": "review this"' in result


def test_skill_read_blocks_a_sensitive_file_added_after_the_snapshot(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    repo, drive = skill_roots
    manifest = _write_skill(repo, "late-secret")
    catalog, ctx = _env(repo, drive)
    secret = manifest.parent / "references" / "auth_token.json"
    secret.parent.mkdir()
    secret.write_text('{"token": "LEAK-ME"}\n', encoding="utf-8")

    result = run_tool(
        catalog,
        ctx,
        "skill_read",
        {"skill": "distribution:late-secret", "path": "references/auth_token.json"},
    )

    assert "SKILL_READ_BLOCKED" in result
    assert "LEAK-ME" not in result


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
        {"skill": "distribution:absolute", "path": str(secret)},
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
        skill="distribution:malformed-path",
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
        {"skill": "distribution:first", "path": "references/outside.md"},
    )
    neighbor_result = run_tool(
        catalog,
        ctx,
        "skill_read",
        {"skill": "distribution:first", "path": "references/neighbor.md"},
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
        {"skill": "distribution:file-kinds", "path": "references/nested"},
    )
    fifo_result = run_tool(
        catalog,
        ctx,
        "skill_read",
        {"skill": "distribution:file-kinds", "path": "references/pipe"},
    )
    missing_result = run_tool(
        catalog,
        ctx,
        "skill_read",
        {"skill": "distribution:file-kinds", "path": "references/missing.md"},
    )
    invalid_result = run_tool(
        catalog,
        ctx,
        "skill_read",
        {"skill": "distribution:file-kinds", "path": "references/invalid.bin"},
    )

    assert "SKILL_READ_BLOCKED" in directory_result
    assert "SKILL_READ_BLOCKED" in fifo_result
    assert "SKILL_READ_FILE_NOT_FOUND" in missing_result
    assert "SKILL_READ_NOT_TEXT" in invalid_result
    for result in (directory_result, fifo_result, missing_result, invalid_result):
        assert str(manifest.parent) not in result


def test_skill_read_rejects_unknown_ineligible_and_display_name_aliases(
    skill_roots: tuple[pathlib.Path, pathlib.Path],
) -> None:
    repo, drive = skill_roots
    _write_skill(repo, "ineligible", description="")
    _write_skill(repo, "collision", directory="collision-a", body="first alias owner\n")
    _write_skill(repo, "collision", directory="collision-b", body="second alias owner\n")
    catalog, ctx = _env(repo, drive)

    unknown = run_tool(catalog, ctx, "skill_read", {"skill": "unknown"})
    ineligible = run_tool(catalog, ctx, "skill_read", {"skill": "distribution:ineligible"})
    collision = run_tool(catalog, ctx, "skill_read", {"skill": "collision"})
    first = run_tool(catalog, ctx, "skill_read", {"skill": "distribution:collision-a"})
    second = run_tool(catalog, ctx, "skill_read", {"skill": "distribution:collision-b"})

    assert "SKILL_READ_NOT_FOUND" in unknown
    assert "SKILL_READ_NOT_ELIGIBLE" in ineligible
    assert "SKILL_READ_NOT_FOUND" in collision
    assert "first alias owner" in first
    assert "second alias owner" in second
    assert str(repo) not in unknown + ineligible + collision + first + second


@pytest.fixture
def skill_context(skill_roots):
    from new_core.tools.context import ToolContext
    from tests.helpers.shared import TEST_SCOPE

    repo, drive = skill_roots
    manifest = _write_skill(repo, "retained", body="  verbatim\t\n有时候\n## Runtime context\ntrailing  \n")
    skill_surface = make_skill_surface(repo_dir=repo, drive_root=drive)
    ctx = ToolContext(repo, drive, TEST_SCOPE, skill_surface)
    ctx.messages, _ = build_llm_messages(
        ctx,
        {},
        ctx.scope,
        tool_ctx=ctx,
        skill_surface=skill_surface,
        soft_cap_tokens=1,
        execution_memory_repository=sql_execution_memory_repository(),
    )
    return manifest, ctx


@pytest.mark.parametrize("long_line", [False, True])
def test_retained_pages_merge_without_duplicates_or_unread_text(skill_context, long_line):
    manifest, ctx = skill_context
    manifest.write_text(manifest.read_text() + ("Z" * 100_000 if long_line else "line 10\n" * 20))
    original = manifest.read_text()
    first = _skill_read(ctx, skill="distribution:retained", max_lines=2000 if long_line else 7)
    retain_skill_page(ctx, first.skill_page)
    retain_skill_page(ctx, _skill_read(ctx, skill="distribution:retained", start_char=len(original) - 9).skill_page)
    partial = active_skill_instructions(ctx)
    assert "unread characters" in partial and (long_line or "line 10\n" * 20 not in partial)
    retain_skill_page(ctx, first.skill_page)
    assert active_skill_instructions(ctx) == partial
    retain_skill_page(
        ctx, _skill_read(ctx, skill="distribution:retained", start_char=len(first.skill_page.page.body) - 1).skill_page
    )
    complete = active_skill_instructions(ctx)
    assert complete.count(original) == 1
    assert not long_line or complete.count("Z") == 100_000
    manifest.write_text(original + "CHANGED")
    assert "VERSION_CONFLICT" in retain_skill_page(ctx, _skill_read(ctx, skill="distribution:retained").skill_page)
    assert active_skill_instructions(ctx) == complete


def test_retention_preserves_prompt_notes_and_isolates_contexts(skill_context):
    from new_core.tools.context import ToolContext

    manifest, ctx = skill_context
    note = {"type": "text", "text": "LATE_NOTE"}
    ctx.messages[0]["content"].append(note)
    ctx.messages.append({"role": "assistant", "content": "HISTORY"})
    receipt = retain_skill_page(ctx, _skill_read(ctx, skill="distribution:retained").skill_page)
    assert manifest.read_text() in active_skill_instructions(ctx) and "verbatim" not in receipt
    assert "".join(block["text"] for block in ctx.messages[0]["content"]).count(manifest.read_text()) == 1
    assert note in ctx.messages[0]["content"] and ctx.messages[-1]["content"] == "HISTORY"
    other = ToolContext(ctx.repo_dir, ctx.drive_root, ctx.scope, ctx.skill_surface, task_id=ctx.task_id)
    build_llm_messages(
        other,
        {},
        other.scope,
        tool_ctx=other,
        skill_surface=ctx.skill_surface,
        execution_memory_repository=sql_execution_memory_repository(),
    )
    assert active_skill_instructions(other) == "" and not other.active_skill_pages


@pytest.mark.parametrize("source,soft_cap", [("task", 11000), ("note", 11000), ("task", 0)])
def test_skill_retention_budgets_unchanged_context(skill_context, source, soft_cap):
    from new_core.prompt import PromptSection, RenderedSection
    from ouroboros.agent.context import apply_section_soft_cap, estimate_message_tokens

    manifest, ctx = skill_context
    manifest.write_text(manifest.read_text() + "S" * 28000)
    retained = "U" * 32000
    task = {"text": retained if source == "task" else ""}
    ready = RenderedSection(
        PromptSection("ready_skills", "## Ready execution surfaces", "volatile", lambda _: "", drop_priority=0),
        "R" * 4000,
    )
    ctx.messages, _ = apply_section_soft_cap([ready], task, soft_cap, tool_ctx=ctx)
    if source == "note":
        ctx.messages[0]["content"].append({"type": "text", "text": retained})
    tail = list(ctx.messages[1:])

    for _ in range(2):
        retain_skill_page(ctx, _skill_read(ctx, skill="distribution:retained").skill_page)
        prompt = "".join(block["text"] for block in ctx.messages[0]["content"])
        assert (ready.text in prompt) is (soft_cap == 0)
        assert prompt.count(manifest.read_text()) == 1
        assert retained in str(ctx.messages) and ctx.messages[1:] == tail
        assert ctx.prompt_soft_cap == soft_cap
        if soft_cap:
            assert sum(estimate_message_tokens(m) for m in ctx.messages) + 4096 < 20000


def test_reread_after_soft_cap_preserves_trim_notice_and_prompt(skill_context):
    from copy import deepcopy

    from new_core.prompt import PromptSection, RenderedSection
    from ouroboros.agent.context import apply_section_soft_cap

    manifest, ctx = skill_context
    manifest.write_text(manifest.read_text() + "A" * 5000)
    ready = RenderedSection(
        PromptSection(
            "ready_skills",
            "## Ready execution surfaces",
            "volatile",
            lambda _: "",
            drop_priority=0,
        ),
        "R" * 9000,
    )
    ctx.messages, _ = apply_section_soft_cap([ready], {}, 3000, tool_ctx=ctx)
    page = _skill_read(ctx, skill="distribution:retained").skill_page

    retain_skill_page(ctx, page)
    first_prompt = deepcopy(ctx.messages[0])
    assert "CONTEXT TRIMMED" in str(first_prompt)
    assert ready.text not in str(first_prompt)

    retain_skill_page(ctx, page)
    assert ctx.messages[0] == first_prompt


@pytest.mark.parametrize("path,rejected", [("", True), ("../bad", False), ("references/note.md", False)])
def test_unaccepted_reads_do_not_activate(skill_context, path, rejected):
    from tests.helpers.loop import tool_runner

    manifest, ctx = skill_context
    (manifest.parent / "references").mkdir()
    (manifest.parent / "references" / "note.md").write_text("REFERENCE")
    result = _skill_read(ctx, skill="distribution:retained", path=path)
    assert active_skill_instructions(ctx) == ""
    record = {
        "fn_name": "skill_read",
        "result": result,
        "is_error": rejected,
        "tool_call_id": "read",
        "args_for_log": {},
    }
    tool_runner(ctx.drive_root).process_tool_results(
        results=[record],
        messages=ctx.messages,
        trace={"tool_calls": []},
        tool_ctx=ctx,
    )
    assert active_skill_instructions(ctx) == ""


@pytest.mark.parametrize("failure", [None, "context length exceeded"])
def test_compaction_keeps_instructions_and_recent_history(skill_context, monkeypatch, failure):
    from ouroboros.agent.context_compaction import compact_tool_history_llm
    from tests.helpers.messages import make_large_arg_messages

    manifest, ctx = skill_context
    retain_skill_page(ctx, _skill_read(ctx, skill="distribution:retained").skill_page)
    instructions = active_skill_instructions(ctx)
    messages = ctx.messages + make_large_arg_messages("calculator")[1:]

    def summarize(**request):
        if failure:
            raise RuntimeError(failure)
        ids = re.findall(r"\[round:(\d+)\]", request["messages"][-1]["content"])
        return {"content": "\n".join(f"[round:{i}]\nSUMMARY" for i in ids)}, {}

    chat = Mock(side_effect=summarize)
    monkeypatch.setattr("ouroboros.llm.LLMClient.chat", chat)
    compacted = compact_tool_history_llm(
        messages,
        "light-model",
        keep_recent=1,
        protected_instructions=instructions,
        cancellation=EventCancellation(),
        task_id="t",
    ).messages
    assert compacted[0] == messages[0] and len(str(compacted)) < len(str(messages))
    assert "Round 7" in str(compacted) and ("SUMMARY" in str(compacted)) is (failure is None)
    assert chat.call_args.kwargs["model"] == "light-model"
    summary_messages = chat.call_args.kwargs["messages"]
    assert summary_messages[0]["content"] == instructions
    assert manifest.read_text() not in summary_messages[-1]["content"]


@pytest.fixture
def skill_request(skill_context):
    from tests.helpers.loop import llm_provider

    _, ctx = skill_context
    retain_skill_page(ctx, _skill_read(ctx, skill="distribution:retained").skill_page)
    client = Mock(spec=["chat"])
    client.chat.return_value = ({"content": "Ответ漢 содержит сбой."}, {})
    adapter = llm_provider(ctx.drive_root, llm=client, max_retries=1, tool_context=lambda: ctx)
    request = {
        "messages": ctx.messages,
        "model": "test-model",
        "effort": "low",
        "round_idx": 0,
        "use_local": False,
        "accumulated_usage": {},
    }
    return adapter, client, request


def test_skill_request_reaches_client_without_adapter_preflight(skill_request, monkeypatch):
    adapter, client, request = skill_request
    monkeypatch.setenv("OUROBOROS_MODEL_CONTEXT_TOKENS", "1")
    assert isinstance(adapter.call(**request, tools=None), LLMMessage)
    assert client.chat.call_count == 1


@pytest.mark.parametrize("error", [None, "context length exceeded", "temporary network failure"])
def test_skill_call_returns_each_failure_explicitly(skill_request, error):
    adapter, client, request = skill_request
    client.chat.side_effect = RuntimeError("context length exceeded")
    overflow = adapter.call(**request, tools=None)
    assert isinstance(overflow, LLMFailure) and "context length exceeded" in overflow.detail
    assert request["accumulated_usage"] == {}
    client.chat.side_effect = RuntimeError(error) if error else None
    result = adapter.call(**request, tools=None)
    if error:
        assert isinstance(result, LLMFailure) and error in result.detail
    else:
        assert isinstance(result, LLMMessage)
    assert not hasattr(adapter, "last_failure")
    assert all(not key.startswith("_") for key in request["accumulated_usage"])


def test_provider_overflow_skips_ordinary_retries(skill_request):
    adapter, client, request = skill_request
    adapter._max_retries = 3
    client.chat.side_effect = RuntimeError("context length exceeded")
    result = adapter.call(**request, tools=None)
    assert isinstance(result, LLMFailure)
    assert client.chat.call_count == 1


@pytest.mark.parametrize(
    "error",
    [
        "Rate limit reached on tokens per min (TPM): Limit 10000, Used 10000. Please try again in 6s.",
        "HTTP 429: token limit reached",
    ],
)
def test_token_rate_limits_keep_ordinary_retry_and_skill_instructions(skill_request, monkeypatch, error):
    from copy import deepcopy

    adapter, client, request = skill_request
    adapter._max_retries = 3
    monkeypatch.setattr("ouroboros.agent.loop_llm_call.time.sleep", Mock())
    prompt = deepcopy(request["messages"][0])
    client.chat.side_effect = [RuntimeError(error), ({"content": "done"}, {})]
    result = adapter.call(**request, tools=None)
    assert isinstance(result, LLMMessage) and result.content == "done"
    assert client.chat.call_count == 2
    assert client.chat.call_args.kwargs["messages"][0] == prompt


@pytest.fixture
def skill_pressure(skill_context):
    from ouroboros.agent.loop_adapters import TranscriptCompactorAdapter
    from ouroboros.agent.task_ledger import TaskLedger
    from tests.helpers.loop import llm_provider
    from tests.new_core.loop._fakes import make_config, make_ctx, make_state

    _, tool_ctx = skill_context
    bodies = {}
    for name, length in [("a", 1600), ("b", 1600), ("c", 1600)]:
        bodies[name] = _write_skill(tool_ctx.repo_dir, name, body=f"  {name}" * length + "\n\tEND  \n").read_text()
    skill_surface = make_skill_surface(repo_dir=tool_ctx.repo_dir, drive_root=tool_ctx.drive_root)
    tool_ctx.skill_surface = skill_surface
    tool_ctx.messages, _ = build_llm_messages(
        tool_ctx,
        {},
        tool_ctx.scope,
        tool_ctx=tool_ctx,
        skill_surface=skill_surface,
        soft_cap_tokens=0,
        execution_memory_repository=sql_execution_memory_repository(),
    )
    compactor = TranscriptCompactorAdapter(
        cancellation=EventCancellation(),
        drive_root=tool_ctx.drive_root,
        task_id="t",
        task_ledger=TaskLedger(),
        light_model="light-model",
        tool_context=lambda: tool_ctx,
    )
    client = Mock(spec=["chat"])
    client.chat.return_value = ({"content": "done"}, {})
    adapter = llm_provider(tool_ctx.drive_root, llm=client, max_retries=1, tool_context=lambda: tool_ctx)
    ctx = make_ctx(llm=adapter, compactor=compactor, config=make_config(emergency_threshold=3400))
    state = make_state(ctx, messages=tool_ctx.messages)
    return tool_ctx, bodies, client, ctx, state


def test_emergency_selects_fresh_skills_without_changing_render_order(skill_pressure):
    from copy import deepcopy

    tool_ctx, bodies, client, ctx, state = skill_pressure
    for name in bodies:
        retain_skill_page(tool_ctx, _skill_read(tool_ctx, skill=f"distribution:{name}").skill_page)
    original_pages = deepcopy(tool_ctx.active_skill_pages)
    nodes.prepare_transcript(state, ctx)
    nodes.llm_call(state, ctx)
    prompt = "".join(block["text"] for block in client.chat.call_args.kwargs["messages"][0]["content"])
    assert bodies["a"] not in prompt and bodies["b"] in prompt and bodies["c"] in prompt
    assert "Excluded skills: a" in prompt and "skill_read" in prompt
    assert prompt.index("### skill:b/") < prompt.index("### skill:c/")
    assert tool_ctx.active_skill_pages == original_pages
    assert state.last_resp.content == "done" and state.last_failure is None

    retain_skill_page(tool_ctx, _skill_read(tool_ctx, skill="distribution:a").skill_page)
    nodes.prepare_transcript(state, ctx)
    nodes.llm_call(state, ctx)
    prompt = "".join(block["text"] for block in client.chat.call_args.kwargs["messages"][0]["content"])
    assert bodies["a"] in prompt and bodies["b"] not in prompt and bodies["c"] in prompt
    assert prompt.index("### skill:a/") < prompt.index("### skill:c/")
    assert "Excluded skills: b" in prompt
    assert tool_ctx.active_skill_pages == original_pages


def test_reread_below_emergency_keeps_the_system_prompt_identical(skill_pressure):
    from copy import deepcopy
    from dataclasses import replace

    tool_ctx, bodies, client, ctx, state = skill_pressure
    ctx = replace(ctx, config=replace(ctx.config, emergency_threshold=20000))
    for name in bodies:
        retain_skill_page(tool_ctx, _skill_read(tool_ctx, skill=f"distribution:{name}").skill_page)
    original = deepcopy(state.messages[0])
    for _ in range(2):
        retain_skill_page(tool_ctx, _skill_read(tool_ctx, skill="distribution:a").skill_page)
        nodes.prepare_transcript(state, ctx)
        nodes.llm_call(state, ctx)
        assert client.chat.call_args.kwargs["messages"][0] == original
        instructions = active_skill_instructions(tool_ctx)
        assert all(instructions.count(body) == 1 for body in bodies.values())
        assert (
            instructions.index("### skill:a/") < instructions.index("### skill:b/") < instructions.index("### skill:c/")
        )


def test_emergency_skips_oversized_candidate_and_preserves_unseen_latest(skill_pressure):
    tool_ctx, bodies, client, ctx, state = skill_pressure
    bodies["b"] = _write_skill(tool_ctx.repo_dir, "b", body="large middle " * 3000).read_text()
    for name in bodies:
        retain_skill_page(tool_ctx, _skill_read(tool_ctx, skill=f"distribution:{name}").skill_page)
    nodes.prepare_transcript(state, ctx)
    assert bodies["a"] in active_skill_instructions(tool_ctx)
    assert bodies["c"] in active_skill_instructions(tool_ctx)
    assert bodies["b"] not in active_skill_instructions(tool_ctx)

    retain_skill_page(tool_ctx, _skill_read(tool_ctx, skill="distribution:b").skill_page)
    nodes.prepare_transcript(state, ctx)
    nodes.llm_call(state, ctx)
    prompt = "".join(block["text"] for block in client.chat.call_args.kwargs["messages"][0]["content"])
    assert bodies["b"] in prompt
    assert bodies["a"] not in prompt and bodies["c"] not in prompt
    assert state.last_resp.content == "done"


def test_emergency_compacts_history_before_excluding_skills(skill_pressure, monkeypatch):
    from dataclasses import replace

    tool_ctx, bodies, client, ctx, state = skill_pressure
    ctx = replace(ctx, config=replace(ctx.config, emergency_threshold=6000))
    for name in bodies:
        retain_skill_page(tool_ctx, _skill_read(tool_ctx, skill=f"distribution:{name}").skill_page)
    instructions = active_skill_instructions(tool_ctx)
    for index in range(55):
        state.messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "OLD_REASONING " * 10000 if index == 0 else "step",
                    "tool_calls": [{"id": str(index), "function": {"name": "noop", "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": str(index), "content": "OLD_RESULT " * 1000 if index == 0 else "ok"},
            ]
        )

    def summarize(**request):
        assert request["messages"][0]["content"] == instructions
        assert "OLD_RESULT " * 1000 not in str(request["messages"])
        ids = re.findall(r"\[round:(\d+)\]", request["messages"][-1]["content"])
        return {"content": "\n".join(f"[round:{i}]\nSUMMARY" for i in ids)}, {}

    monkeypatch.setattr("ouroboros.llm.LLMClient.chat", Mock(side_effect=summarize))
    nodes.prepare_transcript(state, ctx)
    nodes.llm_call(state, ctx)
    assert active_skill_instructions(tool_ctx) == instructions
    assert "OLD_REASONING" not in str(client.chat.call_args.kwargs["messages"])
    assert "SUMMARY" in str(client.chat.call_args.kwargs["messages"])


@pytest.mark.parametrize("result", ["done", "network failure", "context length exceeded"])
def test_actual_overflow_retries_with_only_the_unseen_skill(skill_pressure, result):
    from copy import deepcopy

    from new_core.loop import build_loop_graph, max_steps_for

    tool_ctx, bodies, client, ctx, state = skill_pressure
    for name in bodies:
        retain_skill_page(tool_ctx, _skill_read(tool_ctx, skill=f"distribution:{name}").skill_page)
    requests = []

    def chat(**request):
        requests.append(deepcopy(request["messages"]))
        if len(requests) == 1 or result == "context length exceeded":
            raise RuntimeError("context length exceeded")
        if result == "network failure":
            raise RuntimeError(result)
        return {"content": result}, {}

    client.chat.side_effect = chat
    build_loop_graph().invoke(state, ctx, max_steps=max_steps_for(ctx.config.max_rounds))
    assert len(requests) == 2
    retry = "".join(block["text"] for block in requests[1][0]["content"])
    assert bodies["c"] in retry and bodies["a"] not in retry and bodies["b"] not in retry
    assert "Excluded skills: a, b" in retry
    assert len(tool_ctx.active_skill_pages) == 3
    if result == "done":
        assert state.final_content == "done" and state.last_failure is None
    else:
        assert result in state.last_failure.detail and state.last_resp is None
        assert ("SKILL_CONTEXT_OVERFLOW" in state.final_content) is (result == "context length exceeded")


@pytest.mark.parametrize("light_token_limit", [None, 3500])
def test_overflow_compacts_short_bulky_history_before_declaring_skill_overflow(
    skill_pressure, monkeypatch, light_token_limit
):
    from copy import deepcopy
    from dataclasses import replace

    from new_core.loop import build_loop_graph, max_steps_for

    tool_ctx, bodies, client, ctx, state = skill_pressure
    ctx = replace(ctx, config=replace(ctx.config, emergency_threshold=150000))
    for name in bodies:
        retain_skill_page(tool_ctx, _skill_read(tool_ctx, skill=f"distribution:{name}").skill_page)
    for index in range(6):
        state.messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "OLD_REASONING " * 15000 if index == 0 else "RECENT_REASONING",
                    "tool_calls": [{"id": str(index), "function": {"name": "noop", "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": str(index), "content": "ok"},
            ]
        )
    requests = []

    def chat(**request):
        requests.append(deepcopy(request["messages"]))
        if ctx.compactor.estimate_tokens(request["messages"]) > 10000:
            raise RuntimeError("context length exceeded")
        return {"content": "done"}, {}

    def summarize(**request):
        if light_token_limit and ctx.compactor.estimate_tokens(request["messages"]) > light_token_limit:
            raise RuntimeError("context length exceeded")
        ids = re.findall(r"\[round:(\d+)\]", request["messages"][-1]["content"])
        return {"content": "\n".join(f"[round:{i}]\nSUMMARY" for i in ids)}, {}

    summary_client = Mock(side_effect=summarize)
    monkeypatch.setattr("ouroboros.llm.LLMClient.chat", summary_client)
    client.chat.side_effect = chat
    build_loop_graph().invoke(state, ctx, max_steps=max_steps_for(ctx.config.max_rounds))
    assert state.final_content == "done" and state.last_failure is None
    assert len(requests) == 2
    assert "OLD_REASONING" not in str(requests[1]) and "RECENT_REASONING" in str(requests[1])
    assert requests[1][-1]["tool_call_id"] == "5"
    prompt = "".join(block["text"] for block in requests[1][0]["content"])
    assert prompt.count(bodies["c"]) == 1 and len(tool_ctx.active_skill_pages) == 3

    assert summary_client.call_count == (2 if light_token_limit else 1)
    if light_token_limit:
        assert summary_client.call_args.kwargs["messages"][0]["content"] == active_skill_instructions(tool_ctx)


@pytest.mark.parametrize(
    "failure, names, expected_requests",
    [
        ("network failure", ("a", "c"), 1),
        ("context length exceeded", ("c",), 1),
        ("context length exceeded", ("a", "c"), 2),
    ],
)
def test_summary_retry_is_bounded_and_requires_overflow_and_changed_projection(
    skill_pressure, monkeypatch, failure, names, expected_requests
):
    tool_ctx, bodies, _, ctx, state = skill_pressure
    for name in names:
        retain_skill_page(tool_ctx, _skill_read(tool_ctx, skill=f"distribution:{name}").skill_page)
    for index in range(2):
        state.messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "RETAINED_REASONING",
                    "tool_calls": [{"id": str(index), "function": {"name": "noop", "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": str(index), "content": "ok"},
            ]
        )
    summary_client = Mock(side_effect=RuntimeError(failure))
    monkeypatch.setattr("ouroboros.llm.LLMClient.chat", summary_client)
    ctx.compactor.compact(state.messages, keep_recent=1, target_tokens=0, target_chars=0)
    assert summary_client.call_count == expected_requests
    assert str(state.messages).count("RETAINED_REASONING") == 2
    assert active_skill_instructions(tool_ctx).count(bodies["c"]) == 1
    assert bodies["a"] not in active_skill_instructions(tool_ctx)
    assert len(tool_ctx.active_skill_pages) == len(names)


@pytest.mark.parametrize("max_rounds", [1, 3])
def test_accepted_manifests_reach_the_next_request_once_under_pressure(skill_pressure, max_rounds):
    from copy import deepcopy
    from dataclasses import replace

    from new_core.loop import build_loop_graph, max_steps_for
    from tests.helpers.loop import tool_runner

    tool_ctx, bodies, client, ctx, state = skill_pressure
    catalog, _ = _env(tool_ctx.repo_dir, tool_ctx.drive_root)
    ctx = replace(
        ctx, tools=tool_runner(tool_ctx.drive_root, catalog=catalog), config=replace(ctx.config, max_rounds=max_rounds)
    )
    state.tool_ctx = tool_ctx
    state.tool_schemas, state.active_tool_names = bind_tool_surface(catalog, tool_ctx, state.messages)
    requests = []

    def chat(**request):
        requests.append(deepcopy(request["messages"]))
        if len(requests) == 1:
            return {
                "tool_calls": [
                    {
                        "id": name,
                        "function": {"name": "skill_read", "arguments": json.dumps({"skill": f"distribution:{name}"})},
                    }
                    for name in bodies
                ]
            }, {}
        return {"content": "done"}, {}

    client.chat.side_effect = chat
    build_loop_graph().invoke(state, ctx, max_steps=max_steps_for(ctx.config.max_rounds))
    assert state.final_content == "done" and len(requests) == 2
    prompt = "".join(block["text"] for block in requests[1][0]["content"])
    assert bodies["a"] not in prompt
    assert prompt.count(bodies["b"]) == prompt.count(bodies["c"]) == 1
    receipts = [msg for msg in requests[1] if msg["role"] == "tool"]
    assert {msg["tool_call_id"] for msg in receipts} == set(bodies)
    assert all("[Tool receipt]" in msg["content"] for msg in receipts)
    assert all(body not in msg["content"] for body in bodies.values() for msg in receipts)


@pytest.mark.parametrize("finish", [nodes.recover, nodes.round_limit_final])
@pytest.mark.parametrize("error", [None, "context length exceeded", "temporary network failure"])
def test_skill_recovery_returns_the_current_result(skill_request, finish, error):
    from tests.new_core.loop._fakes import make_config, make_ctx, make_state

    adapter, client, request = skill_request
    client.chat.side_effect = [
        RuntimeError("context length exceeded"),
        RuntimeError(error) if error else ({"content": "Готово"}, {}),
    ]
    ctx = make_ctx(llm=adapter, config=make_config(fallback_model="test-model"))
    state = make_state(ctx, messages=request["messages"])
    finish(state, ctx)
    result = state.final_content or (state.last_resp.content if state.last_resp else "")
    assert result
    assert (result == "Готово") is (error is None)
    assert ("SKILL_CONTEXT_OVERFLOW" in result) is (error == "context length exceeded")


@pytest.mark.parametrize("model", ["gigachat-b2bank::GigaChat", "test-model"])
@pytest.mark.parametrize("error", [None, "temporary network failure", "context length exceeded"])
def test_language_repair_uses_successful_route_and_current_result(skill_request, monkeypatch, model, error):
    adapter, client, request = skill_request
    fallback = "gigachat-b2bank::GigaChat"
    monkeypatch.setenv("GIGACHAT_B2BANK_MAX_TOKENS", "4096")
    monkeypatch.setenv("OUROBOROS_MODEL_CONTEXT_TOKENS", "65536" if model == fallback else "200000")
    adapter.call(**dict(request, model=fallback), tools=None)
    response = adapter.call(**dict(request, model=model), tools=None)
    client.chat.return_value = ({"content": "Готово"}, {})
    client.chat.side_effect = RuntimeError(error) if error else None
    repaired = adapter.repair_language(response.content, **request)
    expected = response.content if error else "Готово"
    assert expected in repaired
    assert client.chat.call_args.kwargs["model"] == model
    assert "verbatim" in client.chat.call_args.kwargs["messages"][0]["content"]


def test_skill_evidence_does_not_need_repair(skill_request):
    adapter, client, request = skill_request
    assert adapter.repair_language("Ответ有时候", **request) == "Ответ有时候"
    client.chat.assert_not_called()
