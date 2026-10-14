"""PluginAPI registration for the ``type: extension`` fixture.

The loader imports this module, builds a ``PluginAPI`` bound to the manifest's
permission set, and calls ``register(api)`` exactly once. Everything the
extension is allowed to touch has to come through that ``api`` handle.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

MARKER = "OUROBOROS_TEST_EXTENSION_OK"
STATE_FILENAME = "probe.json"

# Declared in the manifest's env_from_settings; anything outside it returns nothing.
_ALLOWED_SETTING_KEYS = ("OUROBOROS_TEST_SKILL_TOKEN",)

_api: Any = None


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _ext_ping(ctx: Any) -> str:
    """Fixed marker proving register() ran and the tool routed back here."""
    return _json(
        {
            "marker": MARKER,
            "tool": "ext_ping",
            "task_id": str(getattr(ctx, "task_id", "") or ""),
        }
    )


def _ext_state(ctx: Any, *, value: str = "") -> str:
    """Round-trip a file through the skill state dir (requires the fs permission)."""
    del ctx
    if _api is None:
        return _json({"status": "error", "message": "extension is not initialized"})
    state_dir = pathlib.Path(_api.get_state_dir())
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / STATE_FILENAME
    target.write_text(_json({"value": value}), encoding="utf-8")
    return _json(
        {
            "marker": MARKER,
            "tool": "ext_state",
            "state_dir": str(state_dir),
            "path": str(target),
            "written": value,
            "read_back": json.loads(target.read_text(encoding="utf-8")).get("value", ""),
        }
    )


def _ext_settings(ctx: Any) -> str:
    """Report which allowlisted settings arrived (requires the read_settings permission).

    Values are reported as presence flags only — a fixture must not print secrets
    into a transcript, and presence is what the allowlist test needs.
    """
    if _api is None:
        return _json({"status": "error", "message": "extension is not initialized"})
    # A key outside env_from_settings must come back absent, not empty-but-present.
    settings = _api.get_settings([*_ALLOWED_SETTING_KEYS, "OUROBOROS_TELEGRAM_TOKEN"])
    return _json(
        {
            "marker": MARKER,
            "tool": "ext_settings",
            "keys": sorted(settings.keys()),
            "present": {key: bool(str(settings.get(key) or "").strip()) for key in sorted(settings)},
            "undeclared_key_leaked": "OUROBOROS_TELEGRAM_TOKEN" in settings,
        }
    )


def register(api: Any) -> None:
    global _api
    _api = api

    api.register_tool(
        "ext_ping",
        _ext_ping,
        description="Return a fixed marker proving the test-extension skill registered through PluginAPI.",
        schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        timeout_sec=10,
    )
    api.register_tool(
        "ext_state",
        _ext_state,
        description="Write a value into the skill state dir and read it back, returning both paths.",
        schema={
            "type": "object",
            "properties": {"value": {"type": "string", "description": "Value to persist and read back."}},
            "required": ["value"],
            "additionalProperties": False,
        },
        timeout_sec=10,
    )
    api.register_tool(
        "ext_settings",
        _ext_settings,
        description="Report which allowlisted settings keys reached the extension, as presence flags.",
        schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        timeout_sec=10,
    )

    api.log("info", "test-extension registered", marker=MARKER, tools=3)


__all__ = ["MARKER", "STATE_FILENAME", "register"]
