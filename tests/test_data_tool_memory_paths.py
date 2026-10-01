"""Generic data tools refuse the paths of migrated memory capabilities.

Migration ``0006`` moved every durable memory — long-term facts, knowledge
topics, and identity — plus the scratchpad into the runtime database, and left
the pre-cutover blobs behind as a rollback net. ``data_read``/``data_list``
therefore serve neither source at those paths: they refuse and name the tool
that owns the data.

The refusal is what closes the confinement hole the shims had — a
``skill_repair`` task resolved a payload-confined target and the shim then
discarded it, answering from the process-wide service instead.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

import pytest

from ouroboros.contracts.task_constraint import TaskConstraint
from ouroboros.storage.core_memory.memory import get_memory
from ouroboros.storage.core_memory.scratchpad import get_scratchpad
from ouroboros.tools.core import _data_list, _data_read


@dataclass
class _FakeCtx:
    repo_dir: pathlib.Path
    drive_root: pathlib.Path
    task_constraint: TaskConstraint | None = field(default=None)

    def repo_path(self, rel: str) -> pathlib.Path:
        return self.repo_dir / rel

    def drive_path(self, rel: str) -> pathlib.Path:
        return self.drive_root / rel


def _ctx(tmp_path: pathlib.Path, constraint: TaskConstraint | None = None) -> _FakeCtx:
    drive = tmp_path / "data"
    drive.mkdir(parents=True, exist_ok=True)
    return _FakeCtx(repo_dir=tmp_path / "repo", drive_root=drive, task_constraint=constraint)


def _confined_ctx(tmp_path: pathlib.Path) -> _FakeCtx:
    """A task confined to one external skill's payload."""
    payload_root = "skills/external/demo"
    ctx = _ctx(
        tmp_path,
        TaskConstraint(mode="skill_repair", skill_name="demo", payload_root=payload_root),
    )
    (ctx.drive_root / payload_root).mkdir(parents=True, exist_ok=True)
    return ctx


def test_data_read_preserves_line_slice_format(tmp_path):
    ctx = _ctx(tmp_path)
    target = ctx.drive_root / "notes.txt"
    target.write_text("".join(f"line{index}\n" for index in range(1, 101)), encoding="utf-8")

    result = _data_read(ctx, "notes.txt", max_lines=10)

    expected = "# notes.txt — lines 1–10 of 100\n" + "".join(f"line{index}\n" for index in range(1, 11))
    assert result == expected


def test_data_read_unmigrated_memory_result_is_not_truncated():
    from ouroboros.agent.loop_tool_execution import ToolCallExecutor

    result = "m" * 70_000

    assert ToolCallExecutor._truncate_tool_result(result, "data_read", {"path": "memory/deep_review.md"}) == result


def test_data_read_existing_file_returns_its_content(tmp_path):
    ctx = _ctx(tmp_path)
    target = ctx.drive_root / "memory" / "plan.md"
    target.parent.mkdir(parents=True)
    target.write_text("real plan content\n", encoding="utf-8")

    assert _data_read(ctx, "memory/plan.md") == "real plan content\n"


@pytest.mark.parametrize("error_type", [PermissionError, IsADirectoryError])
def test_data_read_propagates_non_missing_file_errors(tmp_path, monkeypatch, error_type):
    import ouroboros.tools.core as core

    error = error_type("read failed")
    monkeypatch.setattr(core, "read_text", lambda _path: (_ for _ in ()).throw(error))

    with pytest.raises(error_type, match="read failed"):
        _data_read(_ctx(tmp_path), "notes/plan.md")


def test_data_read_handles_file_disappearing_between_resolution_and_read(tmp_path, monkeypatch):
    import ouroboros.tools.core as core

    ctx = _ctx(tmp_path)
    target = ctx.drive_root / "memory" / "racy.md"
    target.parent.mkdir(parents=True)
    target.write_text("about to vanish\n", encoding="utf-8")
    monkeypatch.setattr(core, "read_text", lambda path: (_ for _ in ()).throw(FileNotFoundError(path)))

    result = _data_read(ctx, "memory/racy.md")

    assert "DATA_NOT_YET_CREATED" in result
    assert "memory/racy.md" in result


@pytest.mark.parametrize(
    ("path", "expected_explanation", "unexpected_explanation"),
    [
        ("memory/deep_review.md", "written lazily", "not guaranteed"),
        ("logs/missing.jsonl", "not guaranteed", "written lazily"),
    ],
)
def test_data_read_missing_path_explains_its_creation_contract(
    tmp_path,
    path,
    expected_explanation,
    unexpected_explanation,
):
    result = _data_read(_ctx(tmp_path), path)

    assert "DATA_NOT_YET_CREATED" in result
    assert path in result
    assert expected_explanation in result
    assert unexpected_explanation not in result


def test_data_read_strips_absolute_drive_prefix(tmp_path):
    ctx = _ctx(tmp_path)
    target = ctx.drive_root / "notes" / "plan.md"
    target.parent.mkdir(parents=True)
    target.write_text("absolute path content\n", encoding="utf-8")

    assert _data_read(ctx, str(target)) == "# notes/plan.md — lines 1–1 of 1\nabsolute path content\n"


def test_data_read_strips_tmp_data_prefix(tmp_path):
    drive_root = tmp_path / ".tmp-data-test" / "data"
    target = drive_root / "notes" / "plan.md"
    target.parent.mkdir(parents=True)
    target.write_text("temporary path content\n", encoding="utf-8")
    ctx = _FakeCtx(repo_dir=tmp_path / "repo", drive_root=drive_root)

    assert (
        _data_read(ctx, ".tmp-data-test/data/notes/plan.md")
        == "# notes/plan.md — lines 1–1 of 1\ntemporary path content\n"
    )


# ---------------------------------------------------------------------------
# Confinement cannot be bypassed through memory paths
# ---------------------------------------------------------------------------


def test_confined_task_cannot_read_global_knowledge(tmp_path):
    get_memory().save("patterns", "knowledge", "Error patterns", "REVIEW_BLOCKED means the plan changed\n")

    result = _data_read(_confined_ctx(tmp_path), "memory/knowledge/patterns.md")

    assert "REVIEW_BLOCKED" not in result
    assert "DATA_READ_BLOCKED" in result


def test_confined_task_cannot_read_the_global_scratchpad(tmp_path):
    get_scratchpad().append_block("working note the skill must not see")

    result = _data_read(_confined_ctx(tmp_path), "memory/scratchpad.md")

    assert "working note" not in result
    assert "DATA_READ_BLOCKED" in result


def test_confined_task_cannot_list_global_knowledge(tmp_path):
    get_memory().save("patterns", "knowledge", "Error patterns", "content\n")

    listing = _data_list(_confined_ctx(tmp_path), "memory/knowledge")

    assert "patterns.md" not in listing
    assert "DATA_LIST_BLOCKED" in listing


# ---------------------------------------------------------------------------
# The legacy blobs stop being a read path
# ---------------------------------------------------------------------------


def test_long_term_memory_path_does_not_serve_the_legacy_blob(tmp_path):
    ctx = _ctx(tmp_path)
    blob = ctx.drive_root / "memory" / "global" / "user_role.md"
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_text("stale pre-cutover body\n", encoding="utf-8")

    result = _data_read(ctx, "memory/global/user_role.md")

    assert "stale pre-cutover body" not in result
    assert "memory_read" in result


def test_scratchpad_path_does_not_serve_the_legacy_blob(tmp_path):
    ctx = _ctx(tmp_path)
    blob = ctx.drive_root / "memory" / "scratchpad.md"
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_text("stale rendered scratchpad\n", encoding="utf-8")

    result = _data_read(ctx, "memory/scratchpad.md")

    assert "stale rendered scratchpad" not in result
    assert "update_scratchpad" in result


def test_knowledge_path_serves_neither_the_row_nor_the_blob(tmp_path):
    ctx = _ctx(tmp_path)
    get_memory().save("patterns", "knowledge", "Error patterns", "live row body\n")
    blob = ctx.drive_root / "memory" / "knowledge" / "patterns.md"
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_text("stale blob body\n", encoding="utf-8")

    result = _data_read(ctx, "memory/knowledge/patterns.md")

    assert "live row body" not in result
    assert "stale blob body" not in result
    assert "memory_read" in result


def test_knowledge_index_path_is_refused_like_any_topic(tmp_path):
    get_memory().save("git-recipes", "knowledge", "Rebase workflow", "Rebase before push\n")

    result = _data_read(_ctx(tmp_path), "memory/knowledge/index-full.md")

    assert "Rebase before push" not in result
    assert "memory_read" in result


# ---------------------------------------------------------------------------
# Each refusal names its owning tool and is not an absence report
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "owner"),
    [
        ("memory/global/user_role.md", "memory_read"),
        ("memory/scratchpad.md", "update_scratchpad"),
        ("scratchpad.md", "update_scratchpad"),
        ("memory/knowledge/patterns.md", "memory_read"),
    ],
)
def test_data_read_refusal_names_the_owning_tool(tmp_path, path, owner):
    result = _data_read(_ctx(tmp_path), path)

    assert "DATA_READ_BLOCKED" in result
    assert owner in result
    assert "DATA_NOT_YET_CREATED" not in result


@pytest.mark.parametrize(
    ("directory", "owner"),
    [
        ("memory/global", "memory_list"),
        ("memory/knowledge", "memory_list"),
        ("memory/knowledge/", "memory_list"),
    ],
)
def test_data_list_refusal_names_the_owning_tool(tmp_path, directory, owner):
    listing = _data_list(_ctx(tmp_path), directory)

    assert "DATA_LIST_BLOCKED" in listing
    assert owner in listing


def test_refusal_is_distinguishable_from_a_missing_unmigrated_file(tmp_path):
    """The two empty states stay apart: refused is not 'not created yet'."""
    ctx = _ctx(tmp_path)

    refused = _data_read(ctx, "memory/knowledge/patterns.md")
    missing = _data_read(ctx, "memory/deep_review.md")

    assert "DATA_READ_BLOCKED" in refused
    assert "DATA_NOT_YET_CREATED" not in refused
    assert "DATA_NOT_YET_CREATED" in missing
    assert "DATA_READ_BLOCKED" not in missing


# ---------------------------------------------------------------------------
# Identity migrated too, so its path joins the refused set
# ---------------------------------------------------------------------------


def test_identity_path_serves_neither_the_row_nor_the_blob(tmp_path):
    ctx = _ctx(tmp_path)
    target = ctx.drive_root / "memory" / "identity.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("stale blob identity\n", encoding="utf-8")

    result = _data_read(ctx, "memory/identity.md")

    assert "stale blob identity" not in result
    assert get_memory().load_identity() not in result
    assert "DATA_READ_BLOCKED" in result
    assert "memory_read" in result
    assert "DATA_NOT_YET_CREATED" not in result


# ---------------------------------------------------------------------------
# The refusal is not spelling-sensitive, and covers every leftover
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "memory/./global/user_role.md",
        "memory//global/user_role.md",
        "./memory/global/user_role.md",
        "Memory/global/user_role.md",
        "memory/knowledge/../global/user_role.md",
        "memory/global/",
    ],
)
def test_refusal_survives_the_spellings_pathlib_collapses(tmp_path, path):
    """The table is compared as a string; the target is resolved with pathlib.

    Every spelling here resolves to the same refused blob, so the refusal has to
    normalize the way the resolver does or it fails open.
    """
    ctx = _ctx(tmp_path)
    blob = ctx.drive_root / "memory" / "global" / "user_role.md"
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_text("stale pre-cutover body\n", encoding="utf-8")

    result = _data_read(ctx, path)

    assert "stale pre-cutover body" not in result
    assert "DATA_READ_BLOCKED" in result


@pytest.mark.parametrize(
    "path",
    [
        "memory/scratchpad_blocks.json",
        "memory/scratchpad_journal.jsonl",
        "memory/knowledge_journal.jsonl",
        "memory/WORLD.md",
    ],
)
def test_every_leftover_of_a_migrated_capability_is_refused(tmp_path, path):
    """Backfill sources, pre-cutover journals, and the retired world profile.

    ``_is_cognitive_data_path`` is true for all of them, so a miss did not just
    leak — it returned the whole file with no line window.
    """
    ctx = _ctx(tmp_path)
    target = ctx.drive_root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("stale leftover payload\n", encoding="utf-8")

    result = _data_read(ctx, path)

    assert "stale leftover payload" not in result
    assert "DATA_READ_BLOCKED" in result


def test_the_scratchpad_refusal_never_calls_its_write_tool_a_read_tool(tmp_path):
    """update_scratchpad appends a block; obeying it as a read mutates working memory."""
    result = _data_read(_ctx(tmp_path), "memory/scratchpad.md")

    assert "DATA_READ_BLOCKED" in result
    assert "read via update_scratchpad" not in result
    assert "already in context as `## Scratchpad`" in result
    assert "appends" in result


@pytest.mark.parametrize(
    "path",
    ["memory/global/user_role.md", "memory/./notes.md", "Memory/notes.md", "memory//notes.md"],
)
def test_data_write_refuses_the_memory_prefix_however_it_is_spelled(tmp_path, path):
    from ouroboros.tools.core import _data_write

    result = _data_write(_ctx(tmp_path), path, "sneaky")

    assert "DATA_WRITE_BLOCKED" in result


@pytest.mark.parametrize(
    "path",
    ["scratchpad.md", "identity.md", "world.md", "./identity.md", "Scratchpad.md", "memory/./identity.md"],
)
def test_data_write_refuses_the_root_level_aliases_of_migrated_memory(tmp_path, path):
    """The aliases data_read already refuses must not be writable either.

    A write that succeeds where the matching read is blocked is worse than
    either rule alone: the tool answers OK, a blob lands on disk, and nothing in
    the runtime will ever read it back.
    """
    from ouroboros.tools.core import _data_write

    ctx = _ctx(tmp_path)

    result = _data_write(ctx, path, "sneaky")

    assert "DATA_WRITE_BLOCKED" in result
    assert not (ctx.drive_root / path.lstrip("./")).exists()


@pytest.mark.parametrize(
    ("path", "expected_tool"),
    [
        ("scratchpad.md", "update_scratchpad"),
        ("memory/scratchpad.md", "update_scratchpad"),
        ("identity.md", "memory_save"),
        ("memory/global/user_role.md", "memory_save"),
    ],
)
def test_the_write_refusal_names_the_tool_that_owns_the_path(tmp_path, path, expected_tool):
    """Scratchpad and durable memory have different write tools; one message cannot serve both."""
    from ouroboros.tools.core import _data_write

    result = _data_write(_ctx(tmp_path), path, "sneaky")

    assert "DATA_WRITE_BLOCKED" in result
    assert expected_tool in result


def test_data_write_still_refuses_unmigrated_paths_under_memory(tmp_path):
    """Narrowing the guard to the migrated table alone would open the rest of memory/."""
    from ouroboros.tools.core import _data_write

    result = _data_write(_ctx(tmp_path), "memory/deep_review.md", "sneaky")

    assert "DATA_WRITE_BLOCKED" in result


def test_memory_parent_directory_still_enumerates(tmp_path):
    """memory/ is not migrated wholesale — the unmigrated files stay discoverable."""
    ctx = _ctx(tmp_path)
    (ctx.drive_root / "memory").mkdir(parents=True, exist_ok=True)
    (ctx.drive_root / "memory" / "deep_review.md").write_text("a review\n", encoding="utf-8")

    listing = _data_list(ctx, "memory")

    assert "deep_review.md" in listing
    assert "DATA_LIST_BLOCKED" not in listing
