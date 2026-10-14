"""Core-suite coverage for tool-arg log redaction and the external-actions prompt rule."""

from __future__ import annotations

import pathlib

from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.execution_memory import sql_execution_memory_repository
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


redact_keys_cases = {
    "declared redact keys are omitted": {
        "args": {"subject": "secret subject", "body": "secret body", "retry_assignment_id": "abc"},
        "redact_keys": frozenset({"subject", "body"}),
        "expected": {
            "subject": "[OMITTED: redacted argument]",
            "body": "[OMITTED: redacted argument]",
            "retry_assignment_id": "abc",
        },
    },
    "no redact keys means no tool-name-based omission": {
        "args": {"subject": "plain subject", "body": "plain body"},
        "redact_keys": frozenset(),
        "expected": {"subject": "plain subject", "body": "plain body"},
    },
    "redact_keys None omits every argument value": {
        "args": {"subject": "secret subject", "body": "secret body", "count": 3},
        "redact_keys": None,
        "expected": {
            "subject": "[OMITTED: redacted argument]",
            "body": "[OMITTED: redacted argument]",
            "count": "[OMITTED: redacted argument]",
        },
    },
}


@parametrize_from_dict(redact_keys_cases)
def test_redact_keys_modes(args: dict, redact_keys, expected: dict) -> None:
    from ouroboros.utils import sanitize_tool_args_for_log

    result = sanitize_tool_args_for_log("any_tool", args, redact_keys=redact_keys)
    assert result == expected


secret_detection_cases = {
    "embedded secret patterns in arg values": {
        "args": {
            "query": "use key sk-ant-abcdefghijklmnopqrstuvwxyz012345 please",
            "nested": {"note": "push with ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"},
            "items": ["plain", "token AKIAABCDEFGHIJKLMNOP inside"],
        },
        "redact_keys": frozenset(),
        "assertions": [
            ("not_in_flat", "sk-ant-abcdefghijklmnopqrstuvwxyz012345"),
            ("not_in_flat", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"),
            ("not_in_flat", "AKIAABCDEFGHIJKLMNOP"),
            ("in_query", "***REDACTED***"),
            ("in_nested_note", "***REDACTED***"),
            ("items_0_plain", None),
            ("in_items_1", "***REDACTED***"),
        ],
    },
    "secret named keys beyond the narrow set": {
        "args": {
            "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "access_token": "opaque-value-no-prefix",
            "client_secret": "another-opaque-secret",
            "query": "harmless",
        },
        "redact_keys": frozenset(),
        "assertions": [
            ("eq", "aws_secret_access_key", "*** REDACTED ***"),
            ("eq", "access_token", "*** REDACTED ***"),
            ("eq", "client_secret", "*** REDACTED ***"),
            ("eq", "query", "harmless"),
        ],
    },
    "bearer and url credentials in values": {
        "args": {
            "note": "call with Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature-abcdefghijklmnop now",
            "dsn": "postgres://svc:S3cretPass@db.internal:5432/app",
        },
        "redact_keys": frozenset(),
        "assertions": [
            ("not_in", "note", "eyJhbGciOiJIUzI1NiJ9.payload.signature-abcdefghijklmnop"),
            ("in", "note", "***REDACTED***"),
            ("not_in", "dsn", "S3cretPass"),
            ("in", "dsn", "***REDACTED***"),
        ],
    },
    "wide secret key names": {
        "args": {
            "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "client_secret": "s3cret-value",
            "refresh_token": "rt-abcdef",
            "X-Api-Key": "plain-looking-key",
            "query": "quarterly report",
        },
        "redact_keys": frozenset(),
        "assertions": [
            ("eq", "aws_secret_access_key", "*** REDACTED ***"),
            ("eq", "client_secret", "*** REDACTED ***"),
            ("eq", "refresh_token", "*** REDACTED ***"),
            ("eq", "X-Api-Key", "*** REDACTED ***"),
            ("eq", "query", "quarterly report"),
        ],
    },
    "bearer and url credentials scrubbed from values (no redact_keys)": {
        "args": {
            "note": "login with Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature",
            "dsn": "postgres://svc:S3cretPass@db.internal:5432/app",
        },
        "redact_keys": frozenset(),
        "assertions": [
            ("not_in", "note", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"),
            ("eq", "note", "login with Bearer ***REDACTED***"),
            ("eq", "dsn", "postgres://svc:***REDACTED***@db.internal:5432/app"),
        ],
    },
}


@parametrize_from_dict(secret_detection_cases)
def test_secret_detection(args: dict, redact_keys, assertions: list) -> None:
    from ouroboros.utils import sanitize_tool_args_for_log

    result = sanitize_tool_args_for_log("any_tool", args, redact_keys=redact_keys)
    flat = str(result)

    for assertion in assertions:
        match assertion:
            case ("not_in_flat", value):
                assert value not in flat
            case ("in_query", value):
                assert value in result["query"]
            case ("in_nested_note", value):
                assert value in result["nested"]["note"]
            case ("items_0_plain", _):
                assert result["items"][0] == "plain"
            case ("in_items_1", value):
                assert value in result["items"][1]
            case ("not_in", key, value):
                assert value not in result[key]
            case ("in", key, value):
                assert value in result[key]
            case ("eq", key, value):
                assert result[key] == value


def test_result_side_sanitizer_scrubs_the_same_classes() -> None:
    from ouroboros.utils import sanitize_tool_result_for_log

    scrubbed = sanitize_tool_result_for_log(
        "401 for Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature "
        "on postgres://svc:S3cretPass@db.internal:5432/app"
    )
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in scrubbed
    assert "S3cretPass" not in scrubbed


def test_metadata_redaction_lookup_distinguishes_missing_metadata() -> None:
    from ouroboros.skills.extensions.loader import extension_surface_name
    from ouroboros.tools.runtime_metadata import resolve_tool_runtime_metadata

    assert resolve_tool_runtime_metadata("data_read", scope=TEST_SCOPE).log_redacted_args == frozenset()
    assert (
        resolve_tool_runtime_metadata(
            extension_surface_name("ghost-skill", "vanished"), scope=TEST_SCOPE
        ).log_redacted_args
        is None
    )
    assert resolve_tool_runtime_metadata("ghost-skill.vanished", scope=TEST_SCOPE).log_redacted_args is None


def test_execution_flow_fails_closed_when_extension_metadata_disappears(monkeypatch, tmp_path) -> None:
    import json

    from new_core.llm.types import ToolCall, ToolCallFunction
    from ouroboros.policy import safety
    from ouroboros.skills.extensions.loader import extension_surface_name
    from tests.helpers.factories import permissive_safety
    from tests.helpers.shared import collecting_event_sink, make_catalog, make_tool_ctx

    fn_name = extension_surface_name("ghost-skill", "send")
    monkeypatch.setattr(safety, "check_safety", permissive_safety)

    catalog = make_catalog([])
    ctx = make_tool_ctx(repo_dir=tmp_path, drive_root=tmp_path)
    events = collecting_event_sink()
    tc = ToolCall(
        id="call-ext-1",
        function=ToolCallFunction(name=fn_name, arguments=json.dumps({"subject": "тайна", "body": "текст"})),
    )

    from ouroboros.agent.loop_adapters import build_tool_call_executor

    executor = build_tool_call_executor(
        confirmation_gate=False,
        catalog=catalog,
        events=events,
        execution_memory_repository=sql_execution_memory_repository(),
        audit_event_repository=sql_audit_event_repository(),
    )
    results = executor.execute([tc], ctx=ctx, task_id="t-ext", decisions={})

    omitted = {"subject": "[OMITTED: redacted argument]", "body": "[OMITTED: redacted argument]"}
    assert results[0]["result"].startswith(f"⚠️ Unknown tool: {fn_name}")
    assert results[0]["args_for_log"] == omitted
    started = [payload for payload in events.log_payloads if payload.get("type") == "tool_call_started"]
    assert started and started[0]["args"] == omitted
    assert "тайна" not in str(events.log_payloads)


def test_a_non_string_key_does_not_fail_the_whole_payload() -> None:
    from ouroboros.utils import sanitize_tool_args_for_log

    result = sanitize_tool_args_for_log("demo", {"status": {200: "ok"}, "query": "x"})
    assert result == {"status": {200: "ok"}, "query": "x"}


def test_sanitization_failure_fails_closed() -> None:
    import ouroboros.utils as utils_mod

    class _Exploding(dict):
        def items(self):
            raise RuntimeError("boom")

    result = utils_mod.sanitize_tool_args_for_log("demo", _Exploding({"token": "sk-live-secret"}))
    assert result == {"_error": "sanitization_failed"}
