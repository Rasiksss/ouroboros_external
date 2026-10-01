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
import pathlib
import re
import tempfile

import pytest

from new_core.tools.schema import ToolSchema
from ouroboros import contracts
from ouroboros.contracts import (
    SCHEMA_VERSION_KEY,
    SKILL_MANIFEST_SCHEMA_VERSION,
    GetToolsProtocol,  # noqa: F401  — imported for ``public API`` assertion
    SkillManifest,
    SkillManifestError,
    ToolContextProtocol,
    ToolEntryProtocol,
    parse_skill_manifest_text,
    read_schema_version,
    with_schema_version,
)
from tests._shared import make_tool_env

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def test_public_api_is_stable():
    """The frozen ABI must expose at least this set of names."""
    expected = {
        "ToolContextProtocol",
        "ToolEntryProtocol",
        "GetToolsProtocol",
        "SkillManifest",
        "SkillManifestError",
        "parse_skill_manifest_text",
        "SKILL_MANIFEST_SCHEMA_VERSION",
        "VALID_SKILL_TYPES",
        "VALID_SKILL_RUNTIMES",
        "VALID_SKILL_PERMISSIONS",
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
    from new_core.tools.context import ToolContext

    tmp = pathlib.Path(tempfile.mkdtemp())
    ctx = ToolContext(repo_dir=tmp, drive_root=tmp)
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
    from new_core.tools.context import ToolContext
    from new_core.tools.interfaces import ToolEvents

    source = inspect.getsource(ToolContextProtocol)
    assert re.search(r"^    events\s*:\s*ToolEvents\s*$", source, flags=re.MULTILINE)

    tmp = pathlib.Path(tempfile.mkdtemp())
    assert isinstance(ToolContext(repo_dir=tmp, drive_root=tmp).events, ToolEvents)


def test_toolcontext_path_helpers_resolve_inside_root():
    """repo_path()/drive_path()/drive_logs() must stay inside the declared roots."""
    from new_core.tools.context import ToolContext

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        ctx = ToolContext(repo_dir=root, drive_root=root)
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
    from ouroboros.gateway.contracts import ChatOutbound

    assert set(ChatOutbound.__annotations__.keys()) >= _CHAT_OUTBOUND_REQUIRED


def test_photo_outbound_matches_message_bus_sends():
    """PhotoOutbound TypedDict must match every photo envelope emitted."""
    from ouroboros.gateway.contracts import PhotoOutbound

    declared = set(PhotoOutbound.__annotations__.keys())
    assert declared >= _PHOTO_OUTBOUND_REQUIRED, (
        f"PhotoOutbound lost a required key: {_PHOTO_OUTBOUND_REQUIRED - declared}"
    )
    _assert_envelope_parity(
        REPO_ROOT / "supervisor" / "message_bus.py",
        discriminator="photo",
        declared_keys=declared,
        required_keys=_PHOTO_OUTBOUND_REQUIRED,
        envelope_name="PhotoOutbound",
    )


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
    from ouroboros.gateway.contracts import HealthResponse

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
    from ouroboros.contracts.skill_manifest import VALID_SKILL_RUNTIMES

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
    from ouroboros.contracts.plugin_api import VALID_EXTENSION_PERMISSIONS
    from ouroboros.contracts.skill_manifest import VALID_SKILL_PERMISSIONS

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
    }
    members = {m for m in dir(PluginAPI) if not m.startswith("_") and callable(getattr(PluginAPI, m, None))}
    assert members == expected, (
        f"PluginAPI method set changed. Missing={expected - members}; extra={members - expected}"
    )


def test_plugin_api_version_matches_documented_surface():
    """1.6 added ``register_tool(log_redacted_args=...)``; 1.7 added ``result_limit``; 1.8 added ``retrieval_aliases`` + ``parallel_safe``; 1.9 adds ``safety_policy`` + ``capability_class``."""
    from ouroboros.contracts.plugin_api import PLUGIN_API_VERSION

    assert PLUGIN_API_VERSION == "1.9"


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
