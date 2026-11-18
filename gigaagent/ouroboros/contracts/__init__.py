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
    skill_default_policy,
)
from ouroboros.contracts.schema_versions import (
    SCHEMA_VERSION_KEY,
    read_schema_version,
    with_schema_version,
)
from ouroboros.contracts.tool_abi import GetToolsProtocol, ToolEntryProtocol
from ouroboros.contracts.tool_context import ToolContextProtocol

__all__ = [
    "FORBIDDEN_EXTENSION_SETTINGS",
    "SCHEMA_VERSION_KEY",
    "VALID_EXTENSION_PERMISSIONS",
    "ExtensionRegistrationError",
    "GetToolsProtocol",
    "NetworkPolicy",
    "PluginAPI",
    "SandboxBackend",
    "SandboxPolicy",
    "SandboxResult",
    "SkillStateStore",
    "ToolContextProtocol",
    "ToolEntryProtocol",
    "read_schema_version",
    "skill_default_policy",
    "with_schema_version",
]
