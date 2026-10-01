"""Core-suite coverage for tool-arg log redaction and the external-actions prompt rule.

Skill-specific behavior lives in ``skills/assignment-delegation/tests``; this
module must survive deletion of that skill directory.
"""

from __future__ import annotations

import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_declared_redact_keys_are_omitted_for_logs() -> None:
    from ouroboros.utils import sanitize_tool_args_for_log

    result = sanitize_tool_args_for_log(
        "any_tool",
        {
            "subject": "secret subject",
            "body": "secret body",
            "retry_assignment_id": "abc",
        },
        redact_keys=frozenset({"subject", "body"}),
    )

    assert result == {
        "subject": "[OMITTED: redacted argument]",
        "body": "[OMITTED: redacted argument]",
        "retry_assignment_id": "abc",
    }


def test_no_redact_keys_means_no_tool_name_based_omission() -> None:
    """Redaction is declared per tool, never hardcoded by tool name."""
    from ouroboros.utils import sanitize_tool_args_for_log

    args = {"subject": "plain subject", "body": "plain body"}

    result = sanitize_tool_args_for_log("send_assignment", args)

    assert result == args


def test_redact_keys_none_omits_every_argument_value() -> None:
    """None = redaction metadata unavailable: fail closed, omit everything."""
    from ouroboros.utils import sanitize_tool_args_for_log

    result = sanitize_tool_args_for_log(
        "any_tool",
        {"subject": "secret subject", "body": "secret body", "count": 3},
        redact_keys=None,
    )

    assert result == {
        "subject": "[OMITTED: redacted argument]",
        "body": "[OMITTED: redacted argument]",
        "count": "[OMITTED: redacted argument]",
    }


def test_metadata_redaction_lookup_distinguishes_missing_metadata() -> None:
    """The snapshot the loop takes at tool-call resolve fails closed.

    ``frozenset()`` means "a plain tool that never carries extension redaction
    metadata"; ``None`` means "an extension-shaped name nothing resolves" — the caller
    must then omit every argument value.
    """
    from ouroboros.skills.extensions.loader import extension_surface_name
    from ouroboros.tools.runtime_metadata import resolve_tool_runtime_metadata

    # Core/plain tool names never carry extension redaction metadata.
    assert resolve_tool_runtime_metadata("data_read").log_redacted_args == frozenset()
    # Extension-shaped names without live metadata must fail closed.
    assert resolve_tool_runtime_metadata(extension_surface_name("ghost-skill", "vanished")).log_redacted_args is None
    assert resolve_tool_runtime_metadata("ghost-skill.vanished").log_redacted_args is None


def test_execution_flow_fails_closed_when_extension_metadata_disappears(monkeypatch, tmp_path) -> None:
    """Snapshot taken at tool resolve: missing metadata omits args in every log.

    The dangerous shape is a model calling an extension-shaped name that nothing resolves
    — a skill that unloaded, or was never there. The call cannot succeed, but the
    *arguments* still reach the logs on the way to failing, and they may be exactly the
    values that tool declared as redacted. Nothing knows which ones, so none of them go.
    """
    import json

    from new_core.llm.types import ToolCall, ToolCallFunction
    from new_core.tools.context import ToolContext
    from ouroboros.policy import safety
    from ouroboros.skills.extensions.loader import extension_surface_name
    from tests._shared import collecting_event_sink, make_catalog

    # The ghost skill is not loaded, so no provider offers the name and the metadata
    # resolver fails closed (log_redacted_args=None) without any monkeypatching.
    fn_name = extension_surface_name("ghost-skill", "send")
    monkeypatch.setattr(safety, "check_safety", lambda *args, **kwargs: (True, ""))

    catalog = make_catalog([])
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    events = collecting_event_sink()
    tc = ToolCall(
        id="call-ext-1",
        function=ToolCallFunction(name=fn_name, arguments=json.dumps({"subject": "тайна", "body": "текст"})),
    )

    from ouroboros.agent.loop_adapters import build_tool_call_executor

    executor = build_tool_call_executor(catalog=catalog, drive_logs=tmp_path / "logs", events=events)
    results = executor.execute(
        [tc],
        ctx=ctx,
        task_id="t-ext",
    )

    omitted = {"subject": "[OMITTED: redacted argument]", "body": "[OMITTED: redacted argument]"}
    assert results[0]["result"].startswith(f"⚠️ Unknown tool: {fn_name}")
    assert results[0]["args_for_log"] == omitted
    started = [payload for payload in events.log_payloads if payload.get("type") == "tool_call_started"]
    assert started and started[0]["args"] == omitted
    assert "тайна" not in str(events.log_payloads)


def test_system_prompt_has_generic_external_actions_rule_and_no_tool_reference() -> None:
    text = (_REPO_ROOT / "prompts" / "SYSTEM.md").read_text(encoding="utf-8")

    assert "send_assignment" not in text
    assert "assignment-delegation" not in text
    assert "use a suitable available tool" in text
    assert "not available in this contour" in text
    assert "do not invent tools" in text


def test_embedded_secret_patterns_are_redacted_in_arg_values() -> None:
    from ouroboros.utils import sanitize_tool_args_for_log

    result = sanitize_tool_args_for_log(
        "any_tool",
        {
            "query": "use key sk-ant-abcdefghijklmnopqrstuvwxyz012345 please",
            "nested": {"note": "push with ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"},
            "items": ["plain", "token AKIAABCDEFGHIJKLMNOP inside"],
        },
        redact_keys=frozenset(),
    )

    flat = str(result)
    assert "sk-ant-abcdefghijklmnopqrstuvwxyz012345" not in flat
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in flat
    assert "AKIAABCDEFGHIJKLMNOP" not in flat
    assert "***REDACTED***" in result["query"]
    assert "***REDACTED***" in result["nested"]["note"]
    assert result["items"][0] == "plain"
    assert "***REDACTED***" in result["items"][1]


def test_secret_named_keys_beyond_the_narrow_set_are_redacted() -> None:
    # The narrow _SECRET_KEYS set let these through; the wide _secret_key_name
    # detector covers them (leak found by the trace-toggle review wave).
    from ouroboros.utils import sanitize_tool_args_for_log

    result = sanitize_tool_args_for_log(
        "any_tool",
        {
            "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "access_token": "opaque-value-no-prefix",
            "client_secret": "another-opaque-secret",
            "query": "harmless",
        },
        redact_keys=frozenset(),
    )

    assert result["aws_secret_access_key"] == "*** REDACTED ***"
    assert result["access_token"] == "*** REDACTED ***"
    assert result["client_secret"] == "*** REDACTED ***"
    assert result["query"] == "harmless"


def test_bearer_and_url_credentials_are_redacted_in_values() -> None:
    from ouroboros.utils import sanitize_tool_args_for_log

    result = sanitize_tool_args_for_log(
        "any_tool",
        {
            "note": "call with Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature-abcdefghijklmnop now",
            "dsn": "postgres://svc:S3cretPass@db.internal:5432/app",
        },
        redact_keys=frozenset(),
    )

    assert "eyJhbGciOiJIUzI1NiJ9.payload.signature-abcdefghijklmnop" not in result["note"]
    assert "***REDACTED***" in result["note"]
    assert "S3cretPass" not in result["dsn"]
    assert "***REDACTED***" in result["dsn"]


def test_wide_secret_key_names_are_redacted() -> None:
    """The narrow _SECRET_KEYS set let these through; the payload view is opt-in
    debug output, so a secret-shaped name must never reach it verbatim."""
    from ouroboros.utils import sanitize_tool_args_for_log

    result = sanitize_tool_args_for_log(
        "demo",
        {
            "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "client_secret": "s3cret-value",
            "refresh_token": "rt-abcdef",
            "X-Api-Key": "plain-looking-key",
            "query": "quarterly report",
        },
    )

    assert result["aws_secret_access_key"] == "*** REDACTED ***"
    assert result["client_secret"] == "*** REDACTED ***"
    assert result["refresh_token"] == "*** REDACTED ***"
    assert result["X-Api-Key"] == "*** REDACTED ***"
    assert result["query"] == "quarterly report"


def test_bearer_and_url_credentials_are_scrubbed_from_values() -> None:
    from ouroboros.utils import sanitize_tool_args_for_log

    result = sanitize_tool_args_for_log(
        "demo",
        {
            "note": "login with Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature",
            "dsn": "postgres://svc:S3cretPass@db.internal:5432/app",
        },
    )

    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result["note"]
    assert result["note"] == "login with Bearer ***REDACTED***"
    # Scheme and user stay readable; only the password is cut.
    assert result["dsn"] == "postgres://svc:***REDACTED***@db.internal:5432/app"


def test_result_side_sanitizer_scrubs_the_same_classes() -> None:
    """The two sanitizers share one pass: a token cut from the arguments must
    not survive in the result preview of the same call."""
    from ouroboros.utils import sanitize_tool_result_for_log

    scrubbed = sanitize_tool_result_for_log(
        "401 for Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature "
        "on postgres://svc:S3cretPass@db.internal:5432/app"
    )

    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in scrubbed
    assert "S3cretPass" not in scrubbed


def test_a_non_string_key_does_not_fail_the_whole_payload() -> None:
    """Tool RESULT mappings reach this sanitizer too and may key by int; one
    odd key must not send everything into the fail-closed branch."""
    from ouroboros.utils import sanitize_tool_args_for_log

    result = sanitize_tool_args_for_log("demo", {"status": {200: "ok"}, "query": "x"})

    # Keys are preserved verbatim; only the secret-name checks coerce.
    assert result == {"status": {200: "ok"}, "query": "x"}


def test_sanitization_failure_fails_closed() -> None:
    """A crash inside the walker must not hand consumers the raw arguments."""
    import ouroboros.utils as utils_mod

    class _Exploding(dict):
        def items(self):
            raise RuntimeError("boom")

    result = utils_mod.sanitize_tool_args_for_log("demo", _Exploding({"token": "sk-live-secret"}))

    assert result == {"_error": "sanitization_failed"}
