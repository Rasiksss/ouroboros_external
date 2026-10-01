"""Frozen ABI contracts between runtime, tools, gateway, and skill layer.

This package is intentionally small and additive: Protocols/TypedDicts describe
existing surfaces, while behavior-changing code belongs outside contracts.
"""

from __future__ import annotations

from ouroboros.contracts.plugin_api import (
    FORBIDDEN_EXTENSION_SETTINGS,
    VALID_EXTENSION_PERMISSIONS,
    ExtensionRegistrationError,
    PluginAPI,
    SkillStateStore,
)
from ouroboros.contracts.sandbox_policy import (
    NetworkPolicy,
    SandboxBackend,
    SandboxPolicy,
    SandboxResult,
    preflight_default_policy,
    skill_default_policy,
)
from ouroboros.contracts.schema_versions import (
    SCHEMA_VERSION_KEY,
    read_schema_version,
    with_schema_version,
)
from ouroboros.contracts.skill_manifest import (
    SKILL_MANIFEST_SCHEMA_VERSION,
    VALID_SKILL_PERMISSIONS,
    VALID_SKILL_RUNTIMES,
    VALID_SKILL_TYPES,
    SkillManifest,
    SkillManifestError,
    parse_skill_manifest_text,
)
from ouroboros.contracts.tool_abi import GetToolsProtocol, ToolEntryProtocol
from ouroboros.contracts.tool_context import ToolContextProtocol

__all__ = [
    "FORBIDDEN_EXTENSION_SETTINGS",
    "SCHEMA_VERSION_KEY",
    "SKILL_MANIFEST_SCHEMA_VERSION",
    "VALID_EXTENSION_PERMISSIONS",
    "VALID_SKILL_PERMISSIONS",
    "VALID_SKILL_RUNTIMES",
    "VALID_SKILL_TYPES",
    "ExtensionRegistrationError",
    "GetToolsProtocol",
    "NetworkPolicy",
    "PluginAPI",
    "SandboxBackend",
    "SandboxPolicy",
    "SandboxResult",
    "SkillManifest",
    "SkillManifestError",
    "SkillStateStore",
    "ToolContextProtocol",
    "ToolEntryProtocol",
    "parse_skill_manifest_text",
    "preflight_default_policy",
    "read_schema_version",
    "skill_default_policy",
    "with_schema_version",
]
