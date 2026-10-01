"""Frozen PluginAPI contract for extension skills.

``plugin.py`` exposes ``register(api: PluginAPI)`` and may use only this ABI.
Signature or permission tightening requires a schema/version bump; additive
optional methods are allowed when documented here and pinned by contract tests.
Registrations are declarative and are torn down when a skill unloads.

Version 1.5 added the shared skill state storage surface
(``SkillStateStore`` / ``get_state_store``).

Version 1.6 adds the optional ``register_tool(log_redacted_args=...)`` keyword:
argument NAMES whose values must be omitted from tool-call logs. The loader
validates the names against the tool schema and callers redact them generically;
core code never hardcodes per-tool argument names.

Version 1.7 adds the optional ``register_tool(result_limit=...)`` keyword: the
per-tool char cap the core enforces on the tool result (``None`` = the runtime
default). Extension tools declare it here instead of relying on the core's
built-in per-tool map.

Version 1.8 adds the optional ``register_tool(retrieval_aliases=...)`` and
``register_tool(parallel_safe=...)`` keywords: extra search terms merged into
working-memory retrieval for the tool's invocations, and whether the core may
schedule the tool inside a parallel read-only batch. ``result_limit`` also
gains a registration-time hard ceiling (``RESULT_LIMIT_HARD_MAX`` in the tool
catalog).

Version 1.9 adds the optional ``register_tool(safety_policy=...)`` and
``register_tool(capability_class=...)`` keywords: the unified runtime-metadata
contract (#84). ``safety_policy`` (``"check"``/``"skip"``, default ``"check"``
when unstated) is applied by the core dispatcher on the canonical namespaced
name for every tool kind, so an extension no longer needs an in-plugin safety
gate and distribution ownership no longer bypasses the supervisor.
``capability_class`` (``"read_only"``/``"mutative"``/``"external_network"``)
classifies what the tool touches. ``parallel_safe=True`` now takes effect only
together with ``safety_policy="skip"``; otherwise the tool registers as not
parallel-safe (1.8 registrations keep loading).
"""

from __future__ import annotations

import pathlib
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol, runtime_checkable

PLUGIN_API_VERSION = "1.9"


# Core settings stay hidden unless the trusted distribution manifest declares them.
FORBIDDEN_SKILL_SETTINGS: frozenset[str] = frozenset(
    {
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY",
        "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN",
    }
)
# Backwards-compatible alias for the extension name.
FORBIDDEN_EXTENSION_SETTINGS: frozenset[str] = FORBIDDEN_SKILL_SETTINGS


# Keep in sync with skill_manifest.VALID_SKILL_PERMISSIONS.
VALID_EXTENSION_PERMISSIONS: frozenset[str] = frozenset(
    {
        "net",
        "fs",
        "subprocess",
        "tool",
        "read_settings",
        "companion_process",
        "supervised_task",
        "subscribe_event",
        "inject_chat",
        "send_message",
    }
)


@runtime_checkable
class SkillStateStore(Protocol):
    """Durable JSON/bytes storage restricted to one skill's state namespace."""

    def get_bytes(self, key: str) -> bytes | None: ...

    def put_bytes(self, key: str, data: bytes) -> None: ...

    def put_json(self, key: str, value: dict[str, Any]) -> None: ...

    def create_json_if_absent(self, key: str, value: dict[str, Any]) -> bool: ...

    def replace_json_if_matches(
        self,
        key: str,
        expected: dict[str, Any],
        replacement: dict[str, Any],
    ) -> bool: ...

    def list_keys(self, prefix: str = "") -> list[str]: ...

    def delete(self, key: str) -> None: ...


@runtime_checkable
class PluginAPI(Protocol):
    """Runtime-checkable ABI exposed to each extension's ``register(api)``."""

    def register_tool(
        self,
        name: str,
        handler: Callable[..., str] | Callable[..., Awaitable[str]],
        *,
        description: str,
        schema: dict[str, Any],
        timeout_sec: int = 60,
        log_redacted_args: Sequence[str] = (),
        result_limit: int | None = None,
        retrieval_aliases: Sequence[str] = (),
        parallel_safe: bool = False,
        safety_policy: str | None = None,
        capability_class: str | None = None,
    ) -> None:
        """Register a namespaced tool.

        ``log_redacted_args`` lists argument names whose values are omitted
        from tool-call logs (e.g. payloads that must never be logged).

        ``result_limit`` is the char cap the core enforces on this tool's
        result (``None`` = the runtime default); must be a positive int no
        larger than the catalog's ``RESULT_LIMIT_HARD_MAX``.

        ``retrieval_aliases`` (1.8) lists extra search terms merged into
        working-memory retrieval indexing for this tool's invocations.

        ``parallel_safe`` (1.8) declares the tool safe to run inside a
        parallel read-only tool-call batch.

        ``safety_policy`` (1.9) is ``"check"`` or ``"skip"``; unstated defaults
        to ``"check"``. The core dispatcher applies it on the canonical
        namespaced name for every tool kind.

        ``capability_class`` (1.9) is one of ``"read_only"``, ``"mutative"``,
        ``"external_network"``; unstated falls back to the core default.
        """
        ...

    def register_supervised_task(
        self,
        name: str,
        factory: Callable[[], Awaitable[None]],
        *,
        restart_policy: str = "on_failure",
        max_restarts: int = 5,
        backoff_seconds: float = 2.0,
    ) -> None:
        """Register an extension-lifecycle-bound in-process asyncio task."""
        ...

    def register_companion_process(
        self,
        name: str,
    ) -> None:
        """Register a companion subprocess declared in the trusted manifest."""
        ...

    def subscribe_event(
        self,
        topic: str,
        handler: Callable[[dict[str, Any]], Awaitable[None] | None],
    ) -> str:
        """Subscribe to a manifest-declared event; unload removes subscriptions."""
        ...

    def inject_chat(
        self,
        text: str,
        *,
        chat_id: int = 1,
        user_id: int = 1,
        sender_label: str = "",
        image_base64: str = "",
        image_mime: str = "",
        image_caption: str = "",
        transport: dict[str, Any] | None = None,
        delivery_source: str = "",
    ) -> None:
        """Enqueue a message into the supervisor inbox (requires inject_chat capability)."""
        ...

    def send_message(
        self,
        text: str,
        *,
        chat_id: int,
        source: str,
        task_id: str = "",
    ) -> None:
        """Deliver assistant text through the normal outbound flow."""
        ...

    def allocate_internal_chat_id(self, range_name: str = "a2a") -> int:
        """Allocate a chat id from the skill bridge's window.

        Internal means routing, not secrecy: the id suppresses live delivery to browsers and
        dialogs, and the conversation still appears in chat history.
        """
        ...

    def on_unload(self, callback: Callable[[], Any]) -> None:
        """Register fast, idempotent best-effort cleanup on skill unload."""
        ...

    def log(
        self,
        level: str,
        message: str,
        **fields: Any,
    ) -> None:
        """Structured log. ``level`` one of ``debug``/``info``/``warning``/``error``."""
        ...

    def get_settings(self, keys: Sequence[str]) -> dict[str, Any]:
        """Return allowlisted settings; core keys require owner grants."""
        ...

    def get_state_dir(self) -> str:
        """Return the canonical private state dir for reviewed extension state."""
        ...

    def get_state_store(self) -> SkillStateStore:
        """Return durable storage restricted to this skill's state namespace."""
        ...

    def skill_job_dir(self, job_id: str) -> pathlib.Path:
        """Return a sanitized per-job state directory with assets/output/tmp."""
        ...

    def get_runtime_info(self) -> dict[str, Any]:
        """Return a read-only runtime snapshot; additive within schema v1."""
        ...


class ExtensionRegistrationError(Exception):
    """Raised when a registration violates namespace, permission, or schema."""


__all__ = [
    "FORBIDDEN_EXTENSION_SETTINGS",
    "FORBIDDEN_SKILL_SETTINGS",
    "PLUGIN_API_VERSION",
    "VALID_EXTENSION_PERMISSIONS",
    "ExtensionRegistrationError",
    "PluginAPI",
    "SkillStateStore",
]
