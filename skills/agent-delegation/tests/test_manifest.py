"""The delegation pack is a loadable instruction skill, and it says the things it must say.

An instruction skill has no code to test: what it ships is the text. So what is checked is that
the text parses as a manifest, that the pack is bound to outbound A2A being on, and that the
rule the change exists to state — the order for long work — is actually in it, rather than
having been edited away.
"""

from __future__ import annotations

import pathlib

from new_core.skills.manifest import parse_skill_manifest_text

SKILL_DIR = pathlib.Path(__file__).resolve().parents[1]
SKILL_TEXT = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def _manifest():
    return parse_skill_manifest_text(SKILL_TEXT)


def test_the_manifest_is_valid_and_loadable():
    manifest = _manifest()
    assert manifest.name == "agent-delegation"
    assert manifest.is_instruction()
    assert manifest.description.strip()
    assert manifest.when_to_use.strip()
    assert manifest.validate() == []


def test_the_pack_is_offered_by_the_egress_flag(tmp_path):
    """Nothing in the manifest turns it on: the bundled registry's predicate does."""
    from ouroboros.skills.bundled import BUNDLED_SKILLS
    from ouroboros.storage.settings.schema import AppSettings

    entry = next(skill for skill in BUNDLED_SKILLS if skill.name == "agent-delegation")
    assert entry.is_active(AppSettings.model_validate({"A2A_EGRESS_ENABLED": True})) is True
    assert entry.is_active(AppSettings.model_validate({"A2A_EGRESS_ENABLED": False})) is False

    # An instruction skill declares no runtime, entry, or permissions to grant.
    manifest = _manifest()
    assert manifest.entry == ""
    assert manifest.permissions == []


def test_the_instructions_carry_the_order_for_long_work():
    body = _manifest().body
    assert "wait: false" in body
    assert "a2a_task_status" in body
    assert "a2a_task_cancel" in body
    # The order itself, not just the tool names: submit, record, continue, retrieve.
    submit = body.index("wait: false")
    record = body.index("Record the `task_id`")
    retrieve = body.index("Retrieve the result")
    assert submit < record < retrieve


def test_the_instructions_bound_clarification_rounds():
    body = _manifest().body
    assert "A2A_INPUT_REQUIRED" in body
    assert "at most three clarification rounds" in body


def test_the_task_handle_is_kept_where_compaction_cannot_reach_it():
    """A task id held only in a reply is lost the moment the reply is summarised away."""
    body = _manifest().body
    assert "update_scratchpad" in body


def test_the_outbound_message_is_told_to_carry_no_secrets():
    body = _manifest().body
    assert "never secrets" in body


def test_refusals_are_told_apart():
    body = _manifest().body
    assert "auth-required" in body
    assert "do not retry" in body


def test_the_instructions_teach_handles_and_credential_hints():
    body = _manifest().body
    assert "exactly as the result reported it" in body
    assert "never shortened, edited or composed" in body
    assert "not started by this owner and agent" in body
    assert "personal token" in body
    assert "owner settings" in body
    assert "sign in again" in body
    assert "operator" in body
    assert "do not repeat the call" in body
