"""Contract tests for the frozen v1 ABI in ``ouroboros.contracts``.

These tests protect the minimum guarantees the skill/extension layer will
rely on once external packages start consuming them:

- The concrete ``ToolContext`` dataclass structurally satisfies
  ``ToolContextProtocol``; removing a field is a hard regression.
- Every ``ToolEntry`` produced by the real registry matches
  ``ToolEntryProtocol``.
- The WS/HTTP envelopes emitted by ``server.py`` and
  ``supervisor.message_bus`` still carry the keys declared in ``api_v1``.
- ``SkillManifest`` parses the unified SKILL.md / skill.json format
  tolerantly without raising on missing optional fields.

No test in this file requires network access or a running supervisor.
"""

from __future__ import annotations

import ast
import inspect
import logging
import pathlib
import re
import tempfile
import time

import pytest
from pydantic import ValidationError

from new_core.skills.manifest import (
    SKILL_MANIFEST_SCHEMA_VERSION,
    SkillManifest,
    SkillManifestError,
    parse_skill_manifest_text,
)
from new_core.tools.schema import ToolSchema
from ouroboros import contracts
from ouroboros.contracts import (
    SCHEMA_VERSION_KEY,
    GetToolsProtocol,  # noqa: F401  — imported for ``public API`` assertion
    ToolContextProtocol,
    ToolEntryProtocol,
    read_schema_version,
    with_schema_version,
)
from ouroboros.contracts.attachments import ATTACHMENTS_ADAPTER
from ouroboros.contracts.skill_payload_policy import canonical_declared_script_rel
from tests.helpers.files import stored_file
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import make_tool_ctx, make_tool_env

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


@parametrize_from_dict(
    {
        "missing_mime": {"field": "mime", "value": None},
        "missing_storage_key": {"field": "storage_key", "value": None},
        "missing_status": {"field": "status", "value": None},
        "unknown_status": {"field": "status", "value": "invented"},
    }
)
def test_attachment_metadata_rejects_incomplete_or_unknown_catalog_values(field, value):
    metadata = stored_file()
    assert ATTACHMENTS_ADAPTER.validate_python([metadata]) == [metadata]
    if value is None:
        metadata.pop(field)
    else:
        metadata[field] = value
    with pytest.raises(ValidationError):
        ATTACHMENTS_ADAPTER.validate_python([metadata])


def test_public_attachment_exposes_identity_without_storage_or_authority():
    from ouroboros.contracts.attachments import ATTACHMENT_IDS_ADAPTER, public_attachment

    stored = stored_file()
    projected = public_attachment(stored)
    assert projected == {key: stored[key] for key in ("id", "kind", "filename", "mime", "size", "status", "expires_at")}
    assert ATTACHMENT_IDS_ADAPTER.validate_python([projected])[0].model_dump() == {"id": stored["id"]}


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def test_public_api_is_stable():
    """The frozen ABI must expose at least this set of names."""
    expected = {
        "ToolContextProtocol",
        "ToolEntryProtocol",
        "GetToolsProtocol",
        "SCHEMA_VERSION_KEY",
        "with_schema_version",
        "read_schema_version",
    }
    missing = expected - set(dir(contracts))
    assert missing == set(), f"contracts package missing public names: {missing}"


# ---------------------------------------------------------------------------
# ToolContextProtocol <-> concrete ToolContext
# ---------------------------------------------------------------------------


def test_toolcontext_satisfies_protocol():
    """The concrete registry ToolContext must satisfy the frozen protocol."""

    tmp = pathlib.Path(tempfile.mkdtemp())
    ctx = make_tool_ctx(repo_dir=tmp, drive_root=tmp)
    assert isinstance(ctx, ToolContextProtocol), (
        "ouroboros.tools.registry.ToolContext no longer matches "
        "ToolContextProtocol; a required field was removed or renamed."
    )


def test_toolcontext_protocol_fields_match_dataclass():
    """Every field in ToolContextProtocol must also exist on ToolContext."""
    from new_core.tools.context import ToolContext

    source = inspect.getsource(ToolContextProtocol)
    protocol_field_names = set(re.findall(r"^    ([a-zA-Z_][a-zA-Z0-9_]*)\s*:", source, flags=re.MULTILINE))
    dataclass_field_names = {field.name for field in ToolContext.__dataclass_fields__.values()}
    missing = protocol_field_names - dataclass_field_names
    assert missing == set(), f"ToolContextProtocol declares fields not present on ToolContext: {missing}"


def test_toolcontext_declares_the_events_channel_as_an_interface():
    """The ABI must name the outward channel, and name it as an interface.

    Its predecessor, ``event_queue``, was never declared here, which is why two call sites
    reached it through ``getattr``. Declaring a queue instead of an interface would put the
    transport back in the contract every tool is written against.
    """
    from new_core.tools.interfaces import ToolEvents

    source = inspect.getsource(ToolContextProtocol)
    assert re.search(r"^    events\s*:\s*ToolEvents\s*$", source, flags=re.MULTILINE)

    tmp = pathlib.Path(tempfile.mkdtemp())
    assert isinstance(make_tool_ctx(repo_dir=tmp, drive_root=tmp).events, ToolEvents)


def test_toolcontext_path_helpers_resolve_inside_root():
    """repo_path()/drive_path()/drive_logs() must stay inside the declared roots."""

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        ctx = make_tool_ctx(repo_dir=root, drive_root=root)
        assert ctx.repo_path("a/b.py").is_relative_to(root.resolve())
        assert ctx.drive_path("memory/x.md").is_relative_to(root.resolve())
        assert ctx.drive_logs() == (root / "logs").resolve()


# ---------------------------------------------------------------------------
# ToolEntryProtocol <-> real registry
# ---------------------------------------------------------------------------


def test_every_registered_tool_matches_protocol():
    """Every entry returned by ``ToolRegistry`` must satisfy ToolEntryProtocol."""

    tmp = pathlib.Path(tempfile.mkdtemp())
    catalog, _ctx = make_tool_env(tmp, tmp)
    # Access the private ``_entries`` map — we are the contract test.
    entries = list(catalog.local_entries().values())  # type: ignore[attr-defined]
    assert entries, "Tool registry discovered zero tools"
    for entry in entries:
        assert isinstance(entry, ToolEntryProtocol), (
            f"Tool entry '{getattr(entry, 'name', '?')}' no longer matches ToolEntryProtocol"
        )
        # Sanity-check required keys in the JSON Schema (OpenAI style).
        schema = entry.schema
        assert isinstance(schema, ToolSchema)
        assert schema.name == entry.name
        assert hasattr(schema, "description")
        assert isinstance(schema.parameters, dict)


# ---------------------------------------------------------------------------
# The confirmation vocabulary
# ---------------------------------------------------------------------------


@parametrize_from_dict(
    {
        "bare yes": {"text": "да", "decision": "approved", "confirmation_id": ""},
        "bare no, padded and punctuated": {"text": " Нет. ", "decision": "denied", "confirmation_id": ""},
        "yes naming a request": {"text": "да a1b2c3d4", "decision": "approved", "confirmation_id": "a1b2c3d4"},
        "command declining a request": {
            "text": "/confirm a1b2c3d4 no",
            "decision": "denied",
            "confirmation_id": "a1b2c3d4",
        },
        "command naming a whole id": {
            "text": "/confirm " + "f" * 32 + " yes",
            "decision": "approved",
            "confirmation_id": "f" * 32,
        },
    }
)
def test_an_answer_names_its_decision_and_optionally_its_request(text, decision, confirmation_id):
    from ouroboros.contracts.chat_commands import confirmation_answer

    answer = confirmation_answer(text)

    assert answer is not None
    assert (answer.decision, answer.confirmation_id) == (decision, confirmation_id)


@parametrize_from_dict(
    {
        "a sentence that starts with yes": {"text": "да, отправляй отчёт заказчику"},
        "command without a decision": {"text": "/confirm a1b2c3d4"},
        "command without an id": {"text": "/confirm yes"},
        "command with an unknown decision": {"text": "/confirm a1b2c3d4 maybe"},
        "command with a non-hex id": {"text": "/confirm zzzz yes"},
        "yes with a non-hex id": {"text": "да zzzz"},
        "yes with a number shorter than the id hint": {"text": "да 1234"},
        "command with a number shorter than the id hint": {"text": "/confirm 1234 yes"},
        "empty": {"text": ""},
    }
)
def test_anything_else_is_ordinary_work(text):
    """A sentence that merely starts with "да" is work being described, not a decision."""
    from ouroboros.contracts.chat_commands import confirmation_answer

    assert confirmation_answer(text) is None


@parametrize_from_dict(
    {
        "command the vocabulary does not accept": {"text": "/confirm 1234 maybe", "command": True},
        "a longer command word": {"text": "/confirmed a1b2c3d4 yes", "command": False},
    }
)
def test_a_confirm_command_is_recognized_whatever_follows_it(text, command):
    from ouroboros.contracts.chat_commands import is_confirm_command

    assert is_confirm_command(text) is command


@parametrize_from_dict(
    {
        "the head the question shows": {"value": "a1b2c3d4", "accepted": True},
        "a whole id": {"value": "f" * 32, "accepted": True},
        "shorter than the head": {"value": "a1b2c3", "accepted": False},
        "not hex": {"value": "zzzzzzzz", "accepted": False},
        "a trailing newline": {"value": "f" * 32 + "\n", "accepted": False},
    }
)
def test_what_counts_as_a_request_id(value, accepted):
    from ouroboros.contracts.chat_commands import is_confirmation_id

    assert is_confirmation_id(value) is accepted


# ---------------------------------------------------------------------------
# api_v1 envelopes <-> real broadcasters
# ---------------------------------------------------------------------------


def test_api_v1_declares_core_outbound_message_types():
    """api_v1 must declare outbound envelopes still used by ingress/event paths."""
    from ouroboros.contracts import api_v1

    for name in ("ChatOutbound", "PhotoOutbound", "TypingOutbound", "LogOutbound"):
        assert hasattr(api_v1, name), f"api_v1 missing {name}"


def _dict_literal_keys(node: ast.Dict) -> tuple[set[str], list[str]]:
    """Return (string_keys, non_constant_key_descriptions) for a dict literal.

    ``non_constant_key_descriptions`` surfaces non-string keys (including
    ``**kwargs`` expansions, which appear as ``None`` keys in
    ``ast.Dict.keys``) so tests fail loudly instead of silently dropping
    envelopes assembled via ``{**base, 'type': 'chat'}``.
    """
    string_keys: set[str] = set()
    unknown: list[str] = []
    for key in node.keys:
        if key is None:
            unknown.append("**kwargs-expansion")
            continue
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            string_keys.add(key.value)
            continue
        unknown.append(ast.dump(key))
    return string_keys, unknown


def _dict_type_discriminator(node: ast.Dict) -> str | None:
    """Return the ``"type"`` discriminator value when the dict has one."""
    for k, v in zip(node.keys, node.values, strict=False):
        if not isinstance(k, ast.Constant) or k.value != "type":
            continue
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            return v.value
    return None


def _collect_dict_literals_with_type(
    source_path: pathlib.Path,
    discriminator: str,
) -> list[ast.Dict]:
    """Return every ``ast.Dict`` literal in ``source_path`` whose ``type`` key
    equals ``discriminator``."""
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    matches = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        if _dict_type_discriminator(node) == discriminator:
            matches.append(node)
    return matches


_CHAT_OUTBOUND_REQUIRED = frozenset({"type", "role", "content", "ts"})
_PHOTO_OUTBOUND_REQUIRED = frozenset({"type", "role", "image_base64", "mime", "ts"})
_CONFIRMATION_REQUEST_REQUIRED = frozenset({"type", "confirmation_id", "tool", "arguments_preview", "text"})
_TYPING_OUTBOUND_REQUIRED = frozenset({"type", "action"})
_LOG_OUTBOUND_REQUIRED = frozenset({"type", "data"})


def _assert_envelope_parity(
    source_path: pathlib.Path,
    discriminator: str,
    declared_keys: set[str],
    required_keys: frozenset[str],
    *,
    envelope_name: str,
) -> None:
    """Shared body for WS-envelope parity assertions.

    Walks every ``ast.Dict`` literal in ``source_path`` whose ``type`` key
    equals ``discriminator`` and enforces:

    - no ``**kwargs`` expansion (would silently widen the emission surface);
    - no leaked keys outside ``declared_keys``;
    - every ``required_keys`` element is present.
    """
    literals = _collect_dict_literals_with_type(source_path, discriminator)
    assert literals, f"no {envelope_name} envelopes (type={discriminator!r}) found in {source_path.name}"
    for literal in literals:
        keys, unknown = _dict_literal_keys(literal)
        assert not unknown, (
            f"{envelope_name} envelope in {source_path.name} uses non-constant "
            f"keys (e.g. **kwargs expansion): {unknown}"
        )
        leaked = keys - declared_keys
        assert not leaked, f"{envelope_name} envelope in {source_path.name} uses keys not declared in api_v1: {leaked}"
        missing_required = required_keys - keys
        assert not missing_required, (
            f"{envelope_name} envelope in {source_path.name} is missing required keys: {missing_required}"
        )


def test_chat_outbound_contract_declares_core_keys():
    from transport.browser.contracts import ChatOutbound

    assert set(ChatOutbound.__annotations__.keys()) >= _CHAT_OUTBOUND_REQUIRED


def test_photo_outbound_matches_the_browser_frame():
    """PhotoOutbound must match every photo frame the browser transport emits.

    The frame is built where it is sent — the supervisor publishes an
    ``OutboundMessage``, not a WS envelope.
    """
    from transport.browser.contracts import PhotoOutbound

    declared = set(PhotoOutbound.__annotations__.keys())
    assert declared >= _PHOTO_OUTBOUND_REQUIRED, (
        f"PhotoOutbound lost a required key: {_PHOTO_OUTBOUND_REQUIRED - declared}"
    )
    _assert_envelope_parity(
        REPO_ROOT / "transport" / "browser" / "ws.py",
        discriminator="photo",
        declared_keys=declared,
        required_keys=_PHOTO_OUTBOUND_REQUIRED,
        envelope_name="PhotoOutbound",
    )


def test_confirmation_request_matches_the_browser_frame():
    from transport.browser.contracts import ConfirmationRequestOutbound

    declared = set(ConfirmationRequestOutbound.__annotations__.keys())
    _assert_envelope_parity(
        REPO_ROOT / "transport" / "browser" / "ws.py",
        discriminator="confirmation_request",
        declared_keys=declared,
        required_keys=_CONFIRMATION_REQUEST_REQUIRED,
        envelope_name="ConfirmationRequestOutbound",
    )


def test_history_confirmation_names_the_request_the_live_frame_names():
    from transport.browser.contracts import ConfirmationRequestOutbound, HistoryConfirmation

    frame = set(ConfirmationRequestOutbound.__annotations__) - {"type", "text", "ts", "source"}
    assert set(HistoryConfirmation.__annotations__) == frame | {"open"}


def _api_route_return_dict_literals(route_fn: ast.AsyncFunctionDef) -> list[ast.Dict]:
    """Return every ``return {...}`` dict literal inside an async route."""
    out: list[ast.Dict] = []
    for node in ast.walk(route_fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            out.append(node.value)
    return out


def _find_async_fn(tree: ast.AST, name: str) -> ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    return None


def test_health_response_matches_server_payload():
    """HealthResponse declared keys must cover ``/api/health`` return payload."""
    from transport.browser.contracts import HealthResponse

    tree = ast.parse((REPO_ROOT / "ouroboros" / "gateway" / "state.py").read_text(encoding="utf-8"))
    api_health_fn = _find_async_fn(tree, "api_health")
    assert api_health_fn is not None

    declared = set(HealthResponse.__annotations__.keys())
    response_dicts = _api_route_return_dict_literals(api_health_fn)
    assert response_dicts, "api_health exposes no return dict literal"

    for literal in response_dicts:
        keys, unknown = _dict_literal_keys(literal)
        assert not unknown, f"api_health response uses non-constant keys: {unknown}"
        leaked = keys - declared
        assert not leaked, f"/api/health emits keys not declared in HealthResponse: {leaked}"
        missing = declared - keys
        assert not missing, f"/api/health is missing declared HealthResponse keys: {missing}"


# ---------------------------------------------------------------------------
# SkillManifest parser
# ---------------------------------------------------------------------------


def test_skill_manifest_parses_frontmatter():
    text = (
        "---\n"
        "name: weather\n"
        "description: Check the weather.\n"
        "version: 0.1.0\n"
        "type: script\n"
        "runtime: python3\n"
        "timeout_sec: 30\n"
        "requires: [gigasearch]\n"
        "permissions: [net]\n"
        "scripts:\n"
        "  - name: fetch.py\n"
        "    description: Fetch current weather\n"
        "---\n"
        "# Weather skill\n\n"
        "Call fetch.py with a city.\n"
    )
    manifest = parse_skill_manifest_text(text)
    assert isinstance(manifest, SkillManifest)
    assert manifest.name == "weather"
    assert manifest.type == "script"
    assert manifest.runtime == "python3"
    assert manifest.timeout_sec == 30
    assert manifest.requires == ["gigasearch"]
    assert manifest.permissions == ["net"]
    assert len(manifest.scripts) == 1 and manifest.scripts[0]["name"] == "fetch.py"
    assert "Weather skill" in manifest.body


def test_skill_manifest_parses_json():
    raw = (
        '{"name": "jira", "description": "Jira bridge", '
        '"version": "0.2.0", "type": "extension", "entry": "plugin.py", '
        '"permissions": ["net", "widget"]}'
    )
    manifest = parse_skill_manifest_text(raw)
    assert manifest.type == "extension"
    assert manifest.entry == "plugin.py"
    assert manifest.permissions == ["net", "widget"]


def test_skill_manifest_is_tolerant_of_missing_fields():
    """Body-only markdown must parse as an instruction skill without raising."""
    text = "# Hello World Skill\n\nJust a guide.\n"
    manifest = parse_skill_manifest_text(text)
    assert manifest.type == "instruction"
    assert manifest.is_instruction()
    assert manifest.body.strip().startswith("# Hello World Skill")


@parametrize_from_dict(
    {
        "bare name lives under scripts/": {"declared": "run.py", "expected": "scripts/run.py"},
        "nested name resolves as written": {"declared": "helpers/run.py", "expected": "helpers/run.py"},
        "dotted name resolves as written": {"declared": "./run.py", "expected": "run.py"},
        "explicit scripts/ prefix is kept": {"declared": "scripts/run.py", "expected": "scripts/run.py"},
        "surrounding blanks are ignored": {"declared": "  run.py  ", "expected": "scripts/run.py"},
        "empty name resolves to nothing": {"declared": "   ", "expected": None},
    }
)
def test_a_declared_script_name_resolves_to_one_package_relative_path(declared, expected):
    resolved = canonical_declared_script_rel(declared)
    assert (resolved.as_posix() if resolved is not None else None) == expected


@parametrize_from_dict(
    {
        "script runs a subprocess": {"declared_type": "script", "executable": True},
        "extension imports a module": {"declared_type": "extension", "executable": True},
        "tool imports a module": {"declared_type": "tool", "executable": True},
        "instruction is read, not run": {"declared_type": "instruction", "executable": False},
        "a type this runtime does not know is not run": {"declared_type": "skill", "executable": False},
    }
)
def test_only_the_code_running_manifest_types_are_executable(declared_type, executable):
    manifest = parse_skill_manifest_text(f'{{"name": "p", "description": "d", "type": "{declared_type}"}}')

    assert manifest.is_executable() is executable


# Removed in v5.15.x: test_skill_manifest_body_only_markdown_can_start_with_link_syntax
# and test_skill_manifest_body_only_markdown_can_start_with_thematic_break.
# Both pinned narrow YAML-frontmatter-vs-body-markdown edge cases that
# never occur in real skill payloads (the OpenClaw / Ouroboros adapters
# all emit canonical frontmatter). The base parser tests above cover
# the contract; the removed edge cases were paranoid defenses against
# hand-written manifests that we don't ship.


def test_skill_manifest_rejects_structural_damage():
    """Malformed JSON should raise SkillManifestError, not silently succeed."""
    with pytest.raises(SkillManifestError):
        parse_skill_manifest_text('{"name": ')  # truncated JSON


def test_skill_manifest_rejects_unsupported_schema_version():
    text = "---\nname: future\ntype: script\nschema_version: 2\nscripts:\n  - name: run.py\n---\nbody\n"
    with pytest.raises(SkillManifestError):
        parse_skill_manifest_text(text)


def test_skill_manifest_rejects_malformed_structured_ui_tab():
    text = "---\nname: widgety\ntype: extension\nentry: plugin.py\nui_tab: [oops\n---\nbody\n"
    with pytest.raises(SkillManifestError):
        parse_skill_manifest_text(text)


# test_skill_manifest_rejects_malformed_block_sequence_item removed in
# v5.15.x — paranoid edge case (malformed mid-sequence YAML key without
# colon) that real skills never produce. The base rejection contract is
# proved by test_skill_manifest_rejects_structural_damage above.


# Removed in v5.15.x:
#   test_skill_manifest_accepts_nested_mapping_with_pyyaml — pinned PyYAML
#     deep-nesting behavior. OpenClaw manifests we ship use the simpler
#     shape covered by base parser tests, and PyYAML's own contract is
#     externally tested.
#   test_skill_manifest_block_sequence_tolerates_blank_lines — regression
#     for a long-fixed mid-sequence-blank-line bug. The fix is stable;
#     real skills don't produce this exact shape.


def test_skill_manifest_validate_returns_warnings():
    manifest = SkillManifest(
        name="broken",
        description="",
        version="",
        type="instruction",
        runtime="perl",
        permissions=["bogus"],
        timeout_sec=-1,
    )
    warnings = manifest.validate()
    text = "\n".join(warnings)
    assert "unknown runtime" in text
    assert "unknown permission" in text
    assert "timeout_sec" in text


def test_skill_manifest_allows_v5_7_runtimes():
    from new_core.skills.manifest import VALID_SKILL_RUNTIMES

    for runtime in ("deno", "ruby", "go"):
        assert runtime in VALID_SKILL_RUNTIMES
        manifest = SkillManifest(
            name=f"skill-{runtime}",
            description="ok",
            version="0.1.0",
            type="script",
            runtime=runtime,
            scripts=[{"name": "run"}],
            timeout_sec=30,
        )
        assert not any("unknown runtime" in item for item in manifest.validate())


# ---------------------------------------------------------------------------
# Schema-version helpers
# ---------------------------------------------------------------------------


def test_schema_version_helpers_round_trip():
    payload = {"a": 1}
    stamped = with_schema_version(payload, 2)
    assert stamped[SCHEMA_VERSION_KEY] == 2
    # Input must not be mutated.
    assert SCHEMA_VERSION_KEY not in payload
    assert read_schema_version(stamped) == 2


def test_schema_version_missing_defaults_to_zero():
    assert read_schema_version({"no": "version"}) == 0
    assert read_schema_version(None) == 0
    assert read_schema_version({SCHEMA_VERSION_KEY: "not-an-int"}) == 0


def test_schema_version_rejects_non_mapping_input():
    with pytest.raises(TypeError):
        with_schema_version([1, 2, 3], 1)  # type: ignore[arg-type]


def test_schema_version_preserves_key_shape():
    """``with_schema_version`` must not silently coerce keys to strings.

    Regression for a bug where the helper used a ``{str(k): v ...}`` dict
    comprehension — which collapses e.g. ``1`` and ``"1"`` into a single
    entry even though the caller handed in a mapping that distinguished
    them. The contract is ``dict(payload) + one added key``, preserving
    shapes.
    """
    payload = {1: "int_one", "1": "str_one"}
    stamped = with_schema_version(payload, 5)
    # Both original keys must survive with their original types.
    assert stamped[1] == "int_one"
    assert stamped["1"] == "str_one"
    # The schema-version tag lives alongside them.
    assert stamped[SCHEMA_VERSION_KEY] == 5
    # Input is untouched.
    assert SCHEMA_VERSION_KEY not in payload


# ---------------------------------------------------------------------------
# Schema stability guard
# ---------------------------------------------------------------------------


def test_skill_manifest_schema_version_is_stable():
    """Phase 1 pins SkillManifest schema to 1 — bump deliberately."""
    assert SKILL_MANIFEST_SCHEMA_VERSION == 1


def test_skill_and_extension_permissions_are_kept_in_sync():
    """Final-review regression: Phase 4 ``VALID_EXTENSION_PERMISSIONS``
    introduced ``route``, ``tool``, ``read_settings`` but the Phase 1
    ``VALID_SKILL_PERMISSIONS`` still validated manifests — extension
    manifests declaring the new perms triggered 'unknown permission'
    warnings. The Phase 1 frozen set must be a superset of the Phase 4
    extension-only set."""
    from new_core.skills.manifest import VALID_SKILL_PERMISSIONS
    from ouroboros.contracts.plugin_api import VALID_EXTENSION_PERMISSIONS

    assert VALID_EXTENSION_PERMISSIONS <= VALID_SKILL_PERMISSIONS, (
        f"VALID_EXTENSION_PERMISSIONS has keys not in VALID_SKILL_PERMISSIONS: "
        f"{sorted(VALID_EXTENSION_PERMISSIONS - VALID_SKILL_PERMISSIONS)}"
    )


def test_plugin_api_surface_is_frozen():
    """Phase 4 exposes ``PluginAPI`` as a runtime-checkable Protocol
    with a fixed method set. Additive optional methods must update this
    expected set + release docs; breaking changes require a schema bump."""
    from ouroboros.contracts.plugin_api import PluginAPI

    expected = {
        "register_tool",
        "inject_chat",
        "send_message",
        "allocate_internal_chat_id",
        "on_unload",
        "log",
        "get_settings",
        "get_state_dir",
        "get_state_store",
        "skill_job_dir",
        "get_runtime_info",
        "register_supervised_task",
        "register_companion_process",
        "subscribe_event",
        "operation_scope",
    }
    members = {m for m in dir(PluginAPI) if not m.startswith("_") and callable(getattr(PluginAPI, m, None))}
    assert members == expected, (
        f"PluginAPI method set changed. Missing={expected - members}; extra={members - expected}"
    )


def test_plugin_api_version_matches_documented_surface():
    """1.6 added ``register_tool(log_redacted_args=...)``; 1.7 added ``result_limit``; 1.8 added ``retrieval_aliases`` + ``parallel_safe``; 1.9 added ``safety_policy`` + ``capability_class``; 1.10 adds ``operation_scope()``."""
    from ouroboros.contracts.plugin_api import PLUGIN_API_VERSION

    assert PLUGIN_API_VERSION == "1.10"


def test_tool_entry_satisfies_the_frozen_tool_abi_protocol():
    """The dataclass and ToolEntryProtocol must not drift apart (#84 fields)."""
    from new_core.tools.entry import ToolEntry
    from ouroboros.contracts.tool_abi import ToolEntryProtocol

    entry = ToolEntry(
        name="t",
        schema={"name": "t", "description": "d", "parameters": {"type": "object", "properties": {}}},
        handler=lambda ctx: "",
    )
    assert isinstance(entry, ToolEntryProtocol)
    # isinstance only proves presence on the instance; pin the contract side
    # too, or reverting the protocol fields would go unnoticed.
    assert {
        "safety_policy",
        "capability_class",
        "result_limit",
        "parallel_safe",
        "retrieval_aliases",
        "log_redacted_args",
    } <= set(ToolEntryProtocol.__annotations__)


def test_a_tool_context_requires_a_scope():
    """The old projections let a tool address a chat the run never had."""
    from new_core.tools.context import ToolContext

    tmp = pathlib.Path(tempfile.mkdtemp())
    with pytest.raises(TypeError):
        ToolContext(repo_dir=tmp, drive_root=tmp)  # type: ignore[call-arg]


def test_tool_context_carries_internal_detached_invocation_identity():
    from new_core.tools.context import ToolContext
    from tests.helpers.shared import TEST_SCOPE

    tmp = pathlib.Path(tempfile.mkdtemp())
    context = ToolContext(repo_dir=tmp, drive_root=tmp, scope=TEST_SCOPE, skill_surface=None)
    assert (context.job_id, context.detached, context.tool_call_id) == ("", False, "")

    from ouroboros.contracts.tool_context import ToolContextProtocol

    assert {"job_id", "detached", "tool_call_id"} <= set(ToolContextProtocol.__annotations__)


def test_no_tool_body_reads_a_removed_identity_projection():
    """Every address a tool has goes through ``ctx.scope``."""
    import pathlib

    removed = ("current_chat_id", "current_task_source", "memory_owner_id", "ctx.conversation_id")
    tools_dir = pathlib.Path(__file__).resolve().parents[3] / "ouroboros" / "tools"
    offenders = [
        f"{path.name}: {name}"
        for path in tools_dir.rglob("*.py")
        for name in removed
        if name in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_stored_pair_identity_widens_a_stored_pair_without_reading_configuration():
    """A deployment-wide sweep addresses one owner at a time once storage is keyed by owner.

    The pair comes from the rows being swept, so it must not be the configured one — and
    the result is still a ``ServiceScope``, not something a message could have named.
    """
    from new_core.identity import OwnerScope, ServiceScope
    from ouroboros.contracts.owner_scope import ServiceJob, configured_owner_id, stored_pair_identity

    identity = stored_pair_identity(owner_id="bob", agent_id="second", job=ServiceJob.CONTEXT_CLEANUP)

    assert isinstance(identity, ServiceScope)
    assert not isinstance(identity, OwnerScope)
    assert (identity.owner_id, identity.agent_id) == ("bob", "second")
    assert identity.owner_id != configured_owner_id()
    assert identity.as_scope().owner_id == "bob"


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------


def test_the_wire_helpers_are_generic_and_name_no_seam_type():
    """Two seams share them, so anything naming one seam's types belongs in its codec."""
    from ouroboros.contracts import wire

    source = pathlib.Path(inspect.getfile(wire)).read_text(encoding="utf-8")

    assert "OutboundMessage" not in source and "InboundEnvelope" not in source
    assert "AgentEvent" not in source and "OwnerScope" not in source
    assert set(wire.__all__) == {
        "check_declared_types",
        "check_json_safe",
        "copied",
        "declared_types",
        "known_fields",
        "runtime_classes",
    }


def test_an_unknown_key_is_dropped_and_an_absent_one_takes_its_default():
    """Instances upgrade one at a time; refusing a newer field is a dropped message."""
    import dataclasses

    from ouroboros.contracts.wire import known_fields

    @dataclasses.dataclass
    class Payload:
        text: str = ""
        count: int = 0

    known = known_fields(Payload, {"text": "hi", "invented_later": "x"})

    assert known == {"text": "hi"}
    assert Payload(**known).count == 0


def test_a_field_that_decodes_to_the_wrong_runtime_type_is_refused():
    """A wire ``"false"`` is a wrong object, not a failure, on the hop nobody watches."""
    import dataclasses

    from ouroboros.contracts.wire import check_declared_types

    @dataclasses.dataclass
    class Payload:
        is_progress: bool = False
        tags: list[str] = dataclasses.field(default_factory=list)
        note: str | None = None

    check_declared_types(Payload, {"is_progress": True, "tags": ["a"], "note": None})
    for wrong in ({"is_progress": "false"}, {"tags": "a,b"}, {"note": 7}):
        with pytest.raises(TypeError):
            check_declared_types(Payload, wrong)


def test_a_free_form_container_carries_only_what_a_wire_carries():
    """Refused rather than coerced: a stringified value delivers a wrong answer."""
    import pathlib as pathlib_module

    from ouroboros.contracts.wire import check_json_safe, copied

    check_json_safe({"a": [1, "b", True, None, {"c": 2.5}]}, "transport")
    with pytest.raises(TypeError) as refusal:
        check_json_safe({"a": {"b": pathlib_module.Path("/tmp/x")}}, "transport")
    assert "transport['a']['b']" in str(refusal.value)

    original = {"a": {"b": [1]}}
    duplicate = copied(original)
    duplicate["a"]["b"].append(2)
    assert original == {"a": {"b": [1]}}, "a copy at both ends of the wire, never an alias"


def test_the_wire_helpers_do_not_keep_the_types_they_memoize_alive():
    """A memo must not decide what exists.

    The seam codecs read ``__subclasses__()`` as "every kind on this seam", so a cache
    holding the last reference to a type makes a dead one immortal: a kind declared inside
    one test then appears in another module's pinned set, and which test ran first decides
    whether the suite passes.
    """
    import dataclasses
    import gc
    import weakref

    from ouroboros.contracts.wire import declared_types

    @dataclasses.dataclass
    class Ephemeral:
        text: str = ""

    assert declared_types(Ephemeral) == {"text": (str,)}
    reference = weakref.ref(Ephemeral)
    del Ephemeral
    gc.collect()

    assert reference() is None, "a memoized type must still be collectable"


# ---------------------------------------------------------------------------
# The one policy in this package: what a Redis stream reader does with an outage
# ---------------------------------------------------------------------------


class _BrokerThatIsNotThere:
    """A client whose every command fails, as one pointed at a Redis that is restarting."""

    def __getattr__(self, name: str):
        import redis

        def _fail(*_args, **_kwargs):
            raise redis.exceptions.ConnectionError("the broker is not there")

        return _fail


def _redis_stream_readers():
    from background_memory.infra.events.redis_transport import RedisMemoryEventConsumer
    from ouroboros.agent_events.redis_transport import RedisAgentEventConsumer
    from transport.infra.redis_gateway import RedisInbox

    broker = _BrokerThatIsNotThere()
    return (
        RedisInbox(client=broker, append_client=broker, stream="s:inbound", group="supervisor", consumer="s-1"),
        RedisAgentEventConsumer(client=broker, stream="s:agent-events", group="s:agent-events:supervisor"),
        RedisMemoryEventConsumer(client=broker, stream="s:memory-events", group="g", consumer="c"),
    )


def _kafka_clients_in_an_outage():
    from tests.helpers.kafka_double import KafkaDoubleClientFactory, KafkaDoubleCluster
    from tests.helpers.kafka_helpers import create_fleet_topics, kafka_connection
    from tests.helpers.kafka_recording import KafkaRecordingClientFactory

    cluster = KafkaDoubleCluster()
    connection = kafka_connection("fleet-a")
    create_fleet_topics(cluster, connection)
    cluster.set_outage(True)
    return KafkaRecordingClientFactory(clients=KafkaDoubleClientFactory(cluster=cluster)), connection


def _kafka_stream_readers(clients, connection):
    from background_memory.infra.events.kafka_transport import (
        KAFKA_MEMORY_EVENT_MAX_POLL_INTERVAL_MS,
        KafkaMemoryEventConsumer,
    )
    from ouroboros.agent_events.kafka_transport import KAFKA_AGENT_EVENT_MAX_POLL_INTERVAL_MS, KafkaAgentEventConsumer
    from ouroboros.app.broker_composition import (
        AGENT_EVENT_STREAM,
        INBOUND_STREAM,
        INBOX_GROUP,
        MEMORY_EVENT_GROUP,
        MEMORY_EVENT_STREAM,
    )
    from transport.infra.kafka_gateway import KAFKA_INBOX_MAX_POLL_INTERVAL_MS, KafkaInbox

    return (
        KafkaInbox(
            clients=clients,
            topic=connection.topic(INBOUND_STREAM),
            group_id=connection.group_id(INBOUND_STREAM, INBOX_GROUP),
            client_id="supervisor-contract-inbox",
            max_poll_interval_ms=KAFKA_INBOX_MAX_POLL_INTERVAL_MS,
        ),
        KafkaAgentEventConsumer(
            clients=clients,
            topic=connection.topic(AGENT_EVENT_STREAM),
            group_id=connection.group_id(AGENT_EVENT_STREAM, INBOX_GROUP),
            client_id="supervisor-contract-agent-events",
            max_poll_interval_ms=KAFKA_AGENT_EVENT_MAX_POLL_INTERVAL_MS,
        ),
        KafkaMemoryEventConsumer(
            clients=clients,
            topic=connection.topic(MEMORY_EVENT_STREAM),
            group_id=connection.group_id(MEMORY_EVENT_STREAM, MEMORY_EVENT_GROUP),
            client_id="background-memory-contract",
            max_poll_interval_ms=KAFKA_MEMORY_EVENT_MAX_POLL_INTERVAL_MS,
        ),
    )


def _stream_readers():
    return (*_redis_stream_readers(), *_kafka_stream_readers(*_kafka_clients_in_an_outage()))


def _polled(reader, timeout: float = 0.0):
    """One poll, awaited where the reader's is a coroutine.

    The memory-event seam's consuming half is awaitable and the other two seams' are not
    (`openspec/specs/background-memory-events`); the *policy* under test is the same on all
    three, so the case drives them alike rather than excusing one of them.
    """
    import asyncio
    import inspect

    answer = reader.poll(timeout)
    if inspect.isawaitable(answer):
        return asyncio.get_event_loop().run_until_complete(answer)
    return answer


def test_every_broker_stream_reader_answers_a_dead_broker_the_same_way() -> None:
    """The drift this module exists to prevent, on the one *policy* it holds.

    Three readers on three seams answered "the broker is not there" three ways. A fourth
    backend must not invent a fourth answer.

    Asserted by *calling* each poll rather than by reading the sources for a name: the
    substring this case used to look for survived every mutation that removed the handler,
    which is the regression it is here to catch.
    """
    for reader in _stream_readers():
        assert _polled(reader) is None, f"{type(reader).__name__}.poll must not raise a broker failure at its caller"


def test_every_kafka_stream_reader_asks_a_dead_broker_nothing_but_its_fetch(caplog) -> None:
    clients, connection = _kafka_clients_in_an_outage()

    for reader in _kafka_stream_readers(clients, connection):
        module = type(reader).__module__
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=module):
            assert [_polled(reader) for _ in range(5)] == [None] * 5
        assert _outage_reports(caplog.records, module) == [], f"{type(reader).__name__} reported an idle outage"

    # Two accessors because the memory reader is `aiokafka`'s and the other three are
    # `kafka-python`'s; one fetch name each, and `start`, which that library requires and the
    # other does not. Nothing else is asked of a broker that answers nothing.
    assert {recorded.accessor for recorded in clients.built} == {"group_consumer", "async_group_consumer"}
    assert {call.method for recorded in clients.built for call in recorded.client.calls} == {
        "subscribe",
        "start",
        "poll",
        "getmany",
    }


def _outage_reports(records: list[logging.LogRecord], module: str) -> list[str]:
    return [record.getMessage() for record in records if record.name == module and record.levelno == logging.WARNING]


def test_a_broker_outage_is_a_delay_rather_than_a_latch(caplog) -> None:
    """A reader must recover on its own: the tier cannot need a restart to see Redis return."""
    from ouroboros.contracts.stream_replies import BrokerOutage

    outage = BrokerOutage(logger=logging.getLogger("tests.broker_outage"), retry_after_sec=0.05, report_every_sec=60.0)
    spent = time.monotonic()  # nothing left of the caller's timeout

    assert outage.stands(spent) is False
    with caplog.at_level(logging.WARNING, logger="tests.broker_outage"):
        outage.record(spent, "the broker at %r is unreachable", "s:inbound")
        outage.record(spent, "the broker at %r is unreachable", "s:inbound")

    assert outage.stands(spent) is True, "a command must not be issued again at once"
    assert len(caplog.records) == 1, "one outage is one report, not one per attempt"
    time.sleep(0.06)
    assert outage.stands(spent) is False, "the pause is a delay, not a latch"


def test_a_second_outage_is_reported_with_its_own_cause_inside_the_first_ones_interval(caplog) -> None:
    """The gate is per outage, not per process, or the diagnosis an operator has is the wrong one.

    With one gate for the life of the process, a failure that followed a *different* one
    inside the report interval was logged nowhere at all, and the line the operator did have
    named the first cause with no traceback and nothing saying the fault had changed. Since
    the argument for catching every `RedisError` is that a permanent one becomes a warning
    somebody can act on, this is the half that makes that argument true.
    """
    from ouroboros.contracts.stream_replies import BrokerOutage

    outage = BrokerOutage(logger=logging.getLogger("tests.broker_outage"), retry_after_sec=0.0, report_every_sec=60.0)
    spent = time.monotonic()

    with caplog.at_level(logging.WARNING, logger="tests.broker_outage"):
        outage.record(spent, "connection refused on %r", "s:inbound")
        outage.cleared("%r can be read again", "s:inbound")
        outage.record(spent, "WRONGTYPE on %r", "s:inbound")

    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "connection refused on 's:inbound'",
        "'s:inbound' can be read again",
        "WRONGTYPE on 's:inbound'",
    ]
    assert [record.exc_info is not None for record in caplog.records] == [True, False, True], (
        "the first report of each outage carries its cause; a recovery has none to carry"
    )


def test_a_recovered_reader_may_issue_a_command_at_once(caplog) -> None:
    """The retry pause belongs to the outage: a poll that succeeded must not sit out the next one."""
    from ouroboros.contracts.stream_replies import BrokerOutage

    outage = BrokerOutage(logger=logging.getLogger("tests.broker_outage"), retry_after_sec=30.0, report_every_sec=60.0)
    spent = time.monotonic()

    with caplog.at_level(logging.WARNING, logger="tests.broker_outage"):
        outage.record(spent, "connection refused on %r", "s:inbound")
        assert outage.stands(spent) is True
        outage.cleared("%r can be read again", "s:inbound")

    assert outage.stands(spent) is False, "a cleared outage must not hold the reader for its retry interval"
    outage.cleared("%r can be read again", "s:inbound")
    assert len(caplog.records) == 2, "a recovery that recovers nothing is not a line"


def test_a_failed_poll_spends_the_caller_timeout_rather_than_returning_at_once() -> None:
    """Otherwise the loop that polls again immediately turns one outage into a hot loop."""
    from ouroboros.contracts.stream_replies import BrokerOutage

    outage = BrokerOutage(logger=logging.getLogger("tests.broker_outage"), retry_after_sec=5.0, report_every_sec=60.0)
    started = time.monotonic()

    outage.record(started + 0.2, "the broker at %r is unreachable", "s:inbound")
    waited = time.monotonic() - started
    suppressed = time.monotonic()
    outage.stands(suppressed + 0.2)

    assert waited >= 0.15, "the record must wait out what the caller meant to spend"
    assert time.monotonic() - suppressed >= 0.15, "and so must a poll the outage suppresses"


# ---------------------------------------------------------------------------
# Command vocabulary
# ---------------------------------------------------------------------------


@parametrize_from_dict(
    {
        "on": {"text": "/reasoning on", "expected": True},
        "off": {"text": "/reasoning off", "expected": False},
        "padded and shouted": {"text": "  /Reasoning OFF ", "expected": False},
        "no argument": {"text": "/reasoning", "expected": None},
        "an argument it does not take": {"text": "/reasoning maybe", "expected": None},
        "plain language": {"text": "не показывай размышления", "expected": None},
        "empty": {"text": "", "expected": None},
    }
)
def test_reasoning_visibility_reads_the_commands_argument(text, expected) -> None:
    from ouroboros.contracts.chat_commands import reasoning_visibility

    assert reasoning_visibility(text) is expected


@parametrize_from_dict(
    {
        "bare": {"text": "/reasoning", "expected": "/reasoning"},
        "with an argument": {"text": "/reasoning off", "expected": "/reasoning"},
        "plain language is work, not a command": {"text": "скрой размышления", "expected": ""},
        "a slash command nobody owns": {"text": "/clean", "expected": ""},
        "a plain request to clear the chat": {"text": "почисти историю", "expected": ""},
    }
)
def test_only_the_owned_slash_spellings_reach_the_runtime(text, expected) -> None:
    from ouroboros.contracts.chat_commands import runtime_slash_command

    assert runtime_slash_command(text) == expected


@parametrize_from_dict(
    {
        "bare": {"text": "/clean", "expected": "/clean"},
        "shouted, with an argument": {"text": "/CLEAN everything", "expected": "/clean"},
        "a command that never existed stays work": {"text": "/panic", "expected": ""},
        "plain language": {"text": "почисти историю", "expected": ""},
    }
)
def test_a_retired_command_is_named_however_it_was_typed(text, expected) -> None:
    from ouroboros.contracts.chat_commands import retired_slash_command

    assert retired_slash_command(text) == expected


def test_only_a_leading_slash_reserves_a_message_for_the_owner() -> None:
    from ouroboros.contracts.chat_commands import is_slash_command

    assert is_slash_command("/clean") is True
    assert is_slash_command("очисти чат") is False


def test_task_plan_contract_keeps_user_requirements_separate_from_assumptions() -> None:
    from ouroboros.contracts.task_plan import PlanProposal

    schema = PlanProposal.model_json_schema()
    assert {"requirements", "assumptions", "questions", "steps", "base_revision"} <= schema["properties"].keys()
    assert "source" in schema["$defs"]["Requirement"]["required"]
    assert "reason" in schema["$defs"]["Assumption"]["required"]
    assert "verification_method" in schema["$defs"]["Criterion"]["required"]
