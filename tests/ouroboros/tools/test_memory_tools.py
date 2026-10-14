"""The four memory tools: the schema the model sees, and what each call does.

The schema is where most of this lives. A model that cannot see an option does
not spend a turn discovering it is forbidden — so the accepted-kind enum is built
per caller rather than from the storage enum, and these tests pin that. Domain
rejection is the second line of defence and is covered in ``test_memory_domain``.
"""

from __future__ import annotations

import pathlib

import pytest

from new_core.tools.context import ToolContext
from ouroboros.storage.core_memory.memory import get_memory
from ouroboros.tools.memory_tools import get_tools
from tests.helpers.shared import TEST_SCOPE, make_tool_ctx


@pytest.fixture
def ctx(tmp_path: pathlib.Path) -> ToolContext:
    return make_tool_ctx(repo_dir=tmp_path / "repo", drive_root=tmp_path / "data", task_id="t1")


@pytest.fixture
def distillation_ctx(tmp_path: pathlib.Path) -> ToolContext:
    return make_tool_ctx(
        repo_dir=tmp_path / "repo",
        drive_root=tmp_path / "data",
        task_id="t1",
        current_task_type="distillation",
    )


def _handlers(caller: ToolContext | None = None) -> dict:
    return {entry.name: entry.handler for entry in get_tools(caller)}


def _schemas(caller: ToolContext | None = None) -> dict:
    return {entry.name: entry.schema for entry in get_tools(caller)}


# ------------------------------------------------------------------- schemas


def test_exactly_four_tools_serve_durable_memory():
    assert set(_schemas()) == {"memory_save", "memory_read", "memory_list", "memory_delete"}


def test_the_knowledge_tools_are_gone():
    from new_core.tools.providers.builtin import FROZEN_TOOL_MODULES

    assert "knowledge" not in FROZEN_TOOL_MODULES
    with pytest.raises(ModuleNotFoundError):
        import ouroboros.tools.knowledge  # noqa: F401


def test_the_write_tool_does_not_advertise_identity():
    enum = _schemas()["memory_save"].parameters["properties"]["memory_type"]["enum"]

    assert "identity" not in enum
    assert set(enum) == {"user", "feedback", "project", "reference", "knowledge"}


def test_the_schema_built_for_distillation_offers_neither_user_nor_feedback(distillation_ctx):
    enum = _schemas(distillation_ctx)["memory_save"].parameters["properties"]["memory_type"]["enum"]

    assert enum == ["project", "reference", "knowledge"]


def test_the_write_tool_accepts_both_modes():
    mode = _schemas()["memory_save"].parameters["properties"]["mode"]

    assert mode["enum"] == ["overwrite", "append"]
    assert mode["default"] == "overwrite"
    # Required stays the four things a memory cannot be written without.
    assert _schemas()["memory_save"].parameters["required"] == ["name", "memory_type", "description", "content"]


def test_memory_save_cas_conflict_is_exact_and_does_not_mutate(ctx):
    from ouroboros.storage.core_memory.memory import MemoryConflictError

    save = _handlers()["memory_save"]
    save(ctx, "notes", "project", "Running notes", "original")
    updated_at = get_memory().fetch(ctx.scope, "notes").updated_at

    with pytest.raises(MemoryConflictError) as conflict:
        get_memory().save(
            ctx.scope,
            "notes",
            "project",
            "Running notes",
            "domain loser",
            expected_updated_at=updated_at - 1,
        )

    assert (conflict.value.name, conflict.value.expected, conflict.value.actual) == (
        "notes",
        updated_at - 1,
        updated_at,
    )
    assert (
        save(ctx, "notes", "project", "Running notes", "loser", expected_updated_at=updated_at - 1) == "MEMORY_CONFLICT"
    )
    assert get_memory().fetch(ctx.scope, "notes").content == "original\n"
    assert save(ctx, "notes", "project", "Running notes", "winner", expected_updated_at=updated_at).startswith("✅")


def test_memory_read_exposes_the_exact_numeric_cas_token(ctx):
    import re

    handlers = _handlers()
    handlers["memory_save"](ctx, "notes", "project", "Running notes", "original")
    read_back = handlers["memory_read"](ctx, "notes")
    token = float(re.search(r"updated_at_epoch: ([0-9.]+)", read_back).group(1))

    assert handlers["memory_save"](
        ctx, "notes", "project", "Running notes", "updated", expected_updated_at=token
    ).startswith("✅")


def test_the_list_tool_accepts_an_optional_kind_filter():
    params = _schemas()["memory_list"].parameters

    assert set(params["properties"]["memory_type"]["enum"]) == {"user", "feedback", "project", "reference", "knowledge"}
    assert params["required"] == []


# --------------------------------------------------------------------- calls


def test_a_knowledge_memory_round_trips_through_the_memory_tools(ctx):
    handlers = _handlers()

    saved = handlers["memory_save"](ctx, "git-recipes", "knowledge", "Rebase workflow", "Rebase before push\n")
    assert saved == "✅ Memory 'git-recipes' saved (knowledge, overwrite)."

    assert "Rebase before push" in handlers["memory_read"](ctx, "git-recipes")


def test_append_mode_extends_the_body(ctx):
    handlers = _handlers()
    handlers["memory_save"](ctx, "notes", "project", "Running notes", "first")

    result = handlers["memory_save"](ctx, "notes", "project", "Running notes", "second", mode="append")

    assert result == "✅ Memory 'notes' saved (project, append)."
    assert "first\nsecond" in handlers["memory_read"](ctx, "notes")


def test_an_unknown_mode_is_refused_without_touching_the_memory(ctx):
    handlers = _handlers()
    handlers["memory_save"](ctx, "notes", "project", "Running notes", "original")

    result = handlers["memory_save"](ctx, "notes", "project", "Running notes", "ignored", mode="prepend")

    assert result == "⚠️ Invalid mode 'prepend'. Use 'overwrite' or 'append'."
    assert "original" in handlers["memory_read"](ctx, "notes")


def test_removed_kind_is_rejected_without_mutating_memory(ctx):
    handlers = _handlers()
    with pytest.raises(ValueError, match="Invalid memory kind"):
        get_memory().save(ctx.scope, "legacy", "identity", "d", "body")
    assert "must be one of" in handlers["memory_save"](ctx, "legacy", "identity", "d", "body")
    assert "must be one of" in handlers["memory_list"](ctx, memory_type="identity")
    assert get_memory().list_headers(ctx.scope) == []


def test_retired_document_name_is_an_ordinary_memory_name(ctx):
    handlers = _handlers()
    assert handlers["memory_save"](ctx, "identity", "project", "Identity work", "A project fact").startswith("✅")
    assert "A project fact" in handlers["memory_read"](ctx, "identity")
    assert "n=identity" in handlers["memory_list"](ctx)
    assert handlers["memory_delete"](ctx, "identity").startswith("✅")
    assert get_memory().list_headers(ctx.scope) == []


def test_rejection_still_applies_when_the_distillation_schema_is_ignored(distillation_ctx):
    """Domain-side rejection is the second line of defence; the schema is advisory."""
    handlers = _handlers(distillation_ctx)

    result = handlers["memory_save"](distillation_ctx, "user_role", "feedback", "Injected", "attacker content")

    assert "cannot be set by automated distillation" in result
    assert get_memory().fetch(TEST_SCOPE, "user_role") is None


def test_an_invalid_name_is_reported_with_a_valid_form(ctx):
    result = _handlers()["memory_save"](ctx, "git recipes", "knowledge", "Rebase workflow", "body")

    assert result.startswith("⚠️")
    assert "'git-recipes'" in result
    assert get_memory().fetch(TEST_SCOPE, "git_recipes") is None


def test_a_write_without_a_description_is_refused(ctx):
    result = _handlers()["memory_save"](ctx, "notes", "project", "   ", "body")

    assert result.startswith("⚠️")
    assert get_memory().fetch(TEST_SCOPE, "notes") is None


def test_listing_can_be_filtered_by_kind(ctx):
    handlers = _handlers()
    handlers["memory_save"](ctx, "git-recipes", "knowledge", "Rebase workflow", "body")
    handlers["memory_save"](ctx, "project_x", "project", "Project context", "body")

    knowledge_only = handlers["memory_list"](ctx, memory_type="knowledge")

    assert "n=git-recipes" in knowledge_only
    assert "project_x" not in knowledge_only


def test_listing_rejects_a_kind_outside_the_enum(ctx):
    assert _handlers()["memory_list"](ctx, memory_type="nonsense").startswith("⚠️")


def test_a_fresh_store_is_empty(ctx):
    result = _handlers()["memory_list"](ctx)

    assert result.startswith("Memory index is empty.")
    assert get_memory().list_headers(ctx.scope) == []


def test_the_audit_listing_shows_archived_rows_and_their_status(ctx):
    handlers = _handlers()
    handlers["memory_save"](ctx, "project_x", "project", "Project context", "body")
    get_memory()._repo.set_status(TEST_SCOPE, ["project_x"], "archived")

    active = handlers["memory_list"](ctx)
    audit = handlers["memory_list"](ctx, include_archived=True)

    assert "project_x" not in active
    assert "status=archived n=project_x" in audit or "status=archived" in audit
    assert "project_x" in audit


def test_reading_an_absent_memory_points_at_memory_list(ctx):
    assert "memory_list" in _handlers()["memory_read"](ctx, "never-written")


def test_deleting_an_absent_memory_points_at_memory_list(ctx):
    assert "memory_list" in _handlers()["memory_delete"](ctx, "never-written")


def test_regular_memory_save_allows_user_and_feedback_types(ctx):
    """The counterpart to the distillation narrowing: a task caller may write both."""
    handlers = _handlers()

    user_result = handlers["memory_save"](
        ctx,
        name="user_role",
        memory_type="user",
        description="User role",
        content="The user owns product decisions.",
    )
    feedback_result = handlers["memory_save"](
        ctx,
        name="feedback_style",
        memory_type="feedback",
        description="Feedback preference",
        content="Keep implementation changes narrow.",
    )

    assert "saved (user, overwrite)" in user_result
    assert "saved (feedback, overwrite)" in feedback_result

    store = get_memory()
    user_content, _, _ = store.read(TEST_SCOPE, "user_role")
    feedback_content, _, _ = store.read(TEST_SCOPE, "feedback_style")
    assert "The user owns product decisions." in user_content
    assert "Keep implementation changes narrow." in feedback_content


def test_memory_read_always_shows_recorded_timestamp(ctx):
    get_memory().save(TEST_SCOPE, "exchange_rates", "user", "FX rates", "1 USD = 50 RUB")

    result = _handlers()["memory_read"](ctx, name="exchange_rates")

    assert result.startswith("Recorded:")
    assert "exchange_rates" in result


def test_memory_save_uses_observed_at_for_new_topic(ctx):
    observed = "2026-06-24T05:03:00+00:00"

    result = _handlers()["memory_save"](
        ctx,
        name="exchange_rates",
        memory_type="user",
        description="FX rates",
        content=f"- observed_at: {observed} — 1 USD = 50 RUB",
        observed_at=observed,
    )
    assert "saved (user, overwrite)" in result

    store = get_memory()
    topics = store.list_headers(TEST_SCOPE)
    assert topics[0].first_recorded_at == topics[0].updated_at or topics[0].first_recorded_at > 0
    content, first_recorded_at, _ = store.read(TEST_SCOPE, "exchange_rates")
    assert f"recorded_at: {observed}" in content
    assert first_recorded_at > 0
