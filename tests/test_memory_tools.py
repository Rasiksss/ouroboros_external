"""The four memory tools: the schema the model sees, and what each call does.

The schema is where most of this lives. A model that cannot see an option does
not spend a turn discovering it is forbidden — so the accepted-kind enum is built
per caller rather than from the storage enum, and these tests pin that. Domain
rejection is the second line of defence and is covered in ``test_memory_domain``.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from collections.abc import Callable

import pytest

from new_core.tools.context import ToolContext
from ouroboros.storage.core_memory.memory import get_memory
from ouroboros.tools.memory_tools import get_tools


@pytest.fixture
def ctx(tmp_path: pathlib.Path) -> ToolContext:
    return ToolContext(repo_dir=tmp_path / "repo", drive_root=tmp_path / "data", task_id="t1", current_chat_id=1)


@pytest.fixture
def distillation_ctx(tmp_path: pathlib.Path) -> ToolContext:
    return ToolContext(
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
    """The default: self-edit off, so the option the domain refuses is not shown."""
    enum = _schemas()["memory_save"].parameters["properties"]["memory_type"]["enum"]

    assert "identity" not in enum
    assert set(enum) == {"user", "feedback", "project", "reference", "knowledge"}


def test_neither_hint_nor_description_mentions_writing_identity_when_it_is_off():
    schema = _schemas()["memory_save"]

    assert "identity" not in schema.parameters["properties"]["memory_type"]["description"]
    assert "identity" not in schema.description


def test_the_write_tool_advertises_identity_and_explains_its_discipline_when_the_setting_is_on(
    identity_self_edit_on,
):
    """A capability the model is offered but not told how to use is a wasted turn."""
    schema = _schemas()["memory_save"]
    enum = schema.parameters["properties"]["memory_type"]["enum"]
    hint = schema.parameters["properties"]["memory_type"]["description"]

    assert "identity" in enum
    assert "identity" in hint
    assert "'identity'" in schema.description
    assert "overwrite" in schema.description
    assert "no copy of it is kept" in schema.description
    assert "next task" in schema.description


def test_the_schema_built_for_distillation_offers_neither_user_nor_feedback(distillation_ctx):
    enum = _schemas(distillation_ctx)["memory_save"].parameters["properties"]["memory_type"]["enum"]

    assert enum == ["project", "reference", "knowledge"]


def test_the_write_tool_accepts_both_modes():
    mode = _schemas()["memory_save"].parameters["properties"]["mode"]

    assert mode["enum"] == ["overwrite", "append"]
    assert mode["default"] == "overwrite"
    # Required stays the four things a memory cannot be written without.
    assert _schemas()["memory_save"].parameters["required"] == ["name", "memory_type", "description", "content"]


def test_the_list_tool_accepts_an_optional_kind_filter():
    params = _schemas()["memory_list"].parameters

    assert "identity" in params["properties"]["memory_type"]["enum"]
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


def test_saving_identity_is_refused_and_the_content_is_unchanged(ctx):
    handlers = _handlers()
    stored = get_memory().load_identity()

    result = handlers["memory_save"](ctx, "identity", "project", "hijack", "hijacked")

    assert result.startswith("⚠️")
    assert get_memory().load_identity() == stored


def test_a_direct_identity_write_is_refused_when_the_setting_is_off(ctx):
    handlers = _handlers()
    stored = get_memory().load_identity()

    result = handlers["memory_save"](ctx, "identity", "identity", "d", "rewritten")

    assert "must be one of" in result
    assert get_memory().load_identity() == stored


def test_identity_can_be_rewritten_when_the_setting_is_on(ctx, identity_self_edit_on):
    handlers = _handlers()
    get_memory().load_identity()

    result = handlers["memory_save"](ctx, "identity", "identity", "ignored", "# Who I Am\n\nRewritten.")

    assert "next task" in result
    assert get_memory().load_identity() == "# Who I Am\n\nRewritten.\n"


def test_writing_back_what_memory_read_returned_is_refused_not_stripped(ctx, identity_self_edit_on):
    """Read-edit-write-back is refused: the stored document is never silently edited."""
    handlers = _handlers()
    original = get_memory().load_identity()

    as_read = handlers["memory_read"](ctx, "identity")
    result = handlers["memory_save"](ctx, "identity", "identity", "d", as_read)

    assert "rendered memory header" in result
    assert get_memory().load_identity() == original


def test_a_rewrite_is_marked_as_a_self_edit(ctx, identity_self_edit_on):
    handlers = _handlers()
    get_memory().load_identity()

    handlers["memory_save"](ctx, "identity", "identity", "d", "Rewritten.")

    assert get_memory().fetch("identity").source == "self_edit"


def _audit_field(line: str, name: str) -> str:
    return line.split(f"{name}=", 1)[1].split(" ", 1)[0]


def test_a_rewrite_is_logged_with_the_sizes_of_the_stored_body_never_the_body(ctx, identity_self_edit_on, caplog):
    """The audit line measures what the store holds, not the argument it was handed.

    The two differ — the store normalizes the body — and this is the only trace a
    rewrite leaves, so a line describing the argument would report a size and a
    hash for a state that never existed in the database. The fields ride in the
    message: the shared logging machinery emits nothing but it.
    """
    handlers = _handlers()
    get_memory().load_identity()
    written = "  A secret self-description.\n\n"

    with caplog.at_level("INFO", logger="ouroboros.tools.memory_tools"):
        handlers["memory_save"](ctx, "identity", "identity", "d", written)

    record = next(r for r in caplog.records if r.getMessage().startswith("identity rewritten"))
    stored = get_memory().fetch("identity").content
    line = record.getMessage()
    assert stored != written
    assert int(_audit_field(line, "chars_before")) > 0
    assert int(_audit_field(line, "chars_after")) == len(stored)
    assert _audit_field(line, "identity_sha256") == hashlib.sha256(stored.encode("utf-8")).hexdigest()[:12]
    assert (_audit_field(line, "task_id"), _audit_field(line, "task_type"), _audit_field(line, "chat_id")) == (
        "t1",
        "",
        "1",
    )
    assert "A secret self-description." not in caplog.text


def test_the_audit_line_reaches_the_json_log_without_the_body(ctx, identity_self_edit_on, capsys):
    """End to end through the production emit path: the fields survive formatting."""
    from ouroboros.observability.logging_setup import configure_logging

    configure_logging(level="INFO", log_format="json")
    handlers = _handlers()
    get_memory().load_identity()

    handlers["memory_save"](ctx, "identity", "identity", "d", "Rewritten.")

    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if "identity rewritten" in l)
    payload = json.loads(line)
    assert "task_id=t1" in payload["message"]
    expected_after = len("Rewritten.\n")
    assert f"chars_after={expected_after}" in payload["message"]
    assert "Rewritten." not in out


def _make_the_log_emit_fail(monkeypatch) -> Callable[[], bool]:
    """Break the emit itself. Returns a predicate: did the injected failure happen?"""
    from ouroboros.tools import memory_tools

    calls: list[str] = []

    def exploding_info(*args: object, **kwargs: object) -> None:
        calls.append("info")
        raise RuntimeError("logging is down")

    monkeypatch.setattr(memory_tools.log, "info", exploding_info)
    return lambda: bool(calls)


def _make_the_read_back_fail(monkeypatch) -> Callable[[], bool]:
    """Break the read the audit line takes after the write, leaving the one before it alone."""
    store = get_memory()
    original = store.fetch
    reads: list[str] = []

    def flaky_fetch(name: str):
        reads.append(name)
        if len(reads) > 1:  # the pre-write read comes first; the read-back follows it
            raise RuntimeError("the store went away")
        return original(name)

    monkeypatch.setattr(store, "fetch", flaky_fetch)
    return lambda: len(reads) > 1


@pytest.mark.parametrize(
    "break_the_bookkeeping",
    [_make_the_log_emit_fail, _make_the_read_back_fail],
    ids=["the log emit fails", "the read-back fails"],
)
def test_a_broken_audit_line_does_not_turn_a_committed_rewrite_into_a_failure(
    ctx, identity_self_edit_on, monkeypatch, break_the_bookkeeping
):
    """The write is already committed, so a bookkeeping failure must not report a failed save.

    It used to: the log call sat inside the try that catches every exception, so
    a committed rewrite was reported as failed and the model was told to retry —
    rewriting a second time over a document that no longer holds the original.
    With no previous version kept, this record is the only trace of the change.

    Every step of the bookkeeping counts, the read-back included: it exists to
    describe the stored document, and failing to describe it is not a failed write.
    """
    handlers = _handlers()
    get_memory().load_identity()
    it_fired = break_the_bookkeeping(monkeypatch)

    result = handlers["memory_save"](ctx, "identity", "identity", "d", "Rewritten.")

    assert it_fired(), "this case never injected its failure, so it guards nothing"
    assert "⚠️" not in result
    assert get_memory().load_identity() == "Rewritten.\n"


def test_a_distillation_identity_write_is_refused_with_the_distillation_message(
    distillation_ctx, identity_self_edit_on
):
    handlers = _handlers(distillation_ctx)
    stored = get_memory().load_identity()

    result = handlers["memory_save"](distillation_ctx, "identity", "identity", "d", "rewritten")

    assert "cannot be set by automated distillation" in result
    assert get_memory().load_identity() == stored


def test_deleting_identity_is_refused(ctx):
    handlers = _handlers()
    get_memory().load_identity()

    result = handlers["memory_delete"](ctx, "identity")

    assert result.startswith("⚠️")
    assert get_memory().fetch("identity") is not None


def test_rejection_still_applies_when_the_distillation_schema_is_ignored(distillation_ctx):
    """Domain-side rejection is the second line of defence; the schema is advisory."""
    handlers = _handlers(distillation_ctx)

    result = handlers["memory_save"](distillation_ctx, "user_role", "feedback", "Injected", "attacker content")

    assert "cannot be set by automated distillation" in result
    assert get_memory().fetch("user_role") is None


def test_an_invalid_name_is_reported_with_a_valid_form(ctx):
    result = _handlers()["memory_save"](ctx, "git recipes", "knowledge", "Rebase workflow", "body")

    assert result.startswith("⚠️")
    assert "'git-recipes'" in result
    assert get_memory().fetch("git_recipes") is None


def test_a_write_without_a_description_is_refused(ctx):
    result = _handlers()["memory_save"](ctx, "notes", "project", "   ", "body")

    assert result.startswith("⚠️")
    assert get_memory().fetch("notes") is None


def test_listing_can_be_filtered_by_kind(ctx):
    handlers = _handlers()
    handlers["memory_save"](ctx, "git-recipes", "knowledge", "Rebase workflow", "body")
    handlers["memory_save"](ctx, "project_x", "project", "Project context", "body")

    knowledge_only = handlers["memory_list"](ctx, memory_type="knowledge")

    assert "n=git-recipes" in knowledge_only
    assert "project_x" not in knowledge_only


def test_listing_rejects_a_kind_outside_the_enum(ctx):
    assert _handlers()["memory_list"](ctx, memory_type="nonsense").startswith("⚠️")


def test_a_store_with_nothing_written_yet_still_explains_the_kinds(ctx):
    """The seeded identity means the listing is never literally empty."""
    result = _handlers()["memory_list"](ctx)

    assert "n=identity" in result
    assert "Nothing has been written yet." in result
    assert "knowledge" in result


def test_the_kinds_hint_survives_the_setting_being_on(ctx, identity_self_edit_on):
    """Self-edit makes identity writable, but it does not make the seeded row a write.

    The listing asks what has been authored, so it reads the static policy: keyed
    on ``effective_policy`` instead, turning the setting on would count the seeded
    identity as content and a fresh agent would never see the kinds hint at all.
    """
    result = _handlers()["memory_list"](ctx)

    assert "n=identity" in result
    assert "Nothing has been written yet." in result


def test_the_listing_shows_identity_the_prompt_index_hides(ctx):
    """Regression: memory_list answered 'no such memory' for names that were taken.

    The prompt index omits body-injected rows because their text is already
    above it. Identity is the one body-injected kind; a tool answering "which
    names exist" that omitted it let the model overwrite identity believing it
    was creating an entry.
    """
    handlers = _handlers()
    get_memory().load_identity()

    listing = handlers["memory_list"](ctx)
    identity_only = handlers["memory_list"](ctx, memory_type="identity")

    assert "n=identity" in listing
    assert "inj=body" in listing
    assert "n=identity" in identity_only
    assert "No active identity memories." not in identity_only


def test_the_audit_listing_shows_archived_rows_and_their_status(ctx):
    handlers = _handlers()
    handlers["memory_save"](ctx, "project_x", "project", "Project context", "body")
    get_memory()._repo.set_status("global", ["project_x"], "archived")

    active = handlers["memory_list"](ctx)
    audit = handlers["memory_list"](ctx, include_archived=True)

    assert "project_x" not in active
    assert "status=archived n=project_x" in audit or "status=archived" in audit
    assert "project_x" in audit


def test_reading_an_absent_memory_points_at_memory_list(ctx):
    assert "memory_list" in _handlers()["memory_read"](ctx, "never-written")


def test_deleting_an_absent_memory_points_at_memory_list(ctx):
    assert "memory_list" in _handlers()["memory_delete"](ctx, "never-written")
