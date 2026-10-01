"""Load trusted distribution ``type: extension`` skills through PluginAPI.

Manifest capabilities/settings are the host API allowlist. Registered surfaces
are provider-safe namespaced and tracked per skill so reload/unload can tear
them down and purge staged modules cleanly.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import importlib
import importlib.util
import inspect
import logging
import os
import pathlib
import re
import shutil
import sys
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

from ouroboros.app.config import REPO_DIR
from ouroboros.contracts.plugin_api import (
    FORBIDDEN_EXTENSION_SETTINGS,
    VALID_EXTENSION_PERMISSIONS,
    ExtensionRegistrationError,
    SkillStateStore,
)
from ouroboros.ingress.skill_bridge import SkillBridgeError, get_skill_bridge
from ouroboros.observability.event_bus import get_global_event_bus
from ouroboros.skills.extensions.companion import (
    CompanionDescriptor,
    get_global_supervisor,
    is_server_process,
)
from ouroboros.skills.extensions.isolated_deps import (
    _isolated_python_site_dirs,
    async_isolated_site_dirs_scope,
    is_skill_cache_path,
    isolated_site_dirs_scope,
)
from ouroboros.skills.loader import (
    _SKILL_DIR_CACHE_NAMES,
    LoadedSkill,
    SkillPayloadUnreadable,
    compute_content_hash,
    discover_skills,
    find_skill,
    grant_status_for_skill,
    requested_core_setting_keys,
    skill_state_dir,
)
from ouroboros.storage.blob.keys import normalize_storage_key
from ouroboros.storage.blob.protocol import StorageProvider
from ouroboros.storage.blob.scopes import SCOPE_STATE
from ouroboros.storage.settings.schema import SettingsLike
from ouroboros.tools.catalog import (
    DECLARABLE_CAPABILITY_CLASSES,
    DECLARABLE_SAFETY_POLICIES,
    RESULT_LIMIT_HARD_MAX,
)
from ouroboros.tools.skill_exec import _scrub_env

log = logging.getLogger(__name__)


# Registration bookkeeping.


@dataclass
class _ExtensionRegistrations:
    """Attached surfaces owned by one loaded extension."""

    tools: list[str] = field(default_factory=list)
    unload_callbacks: list[Callable[[], Any]] = field(default_factory=list)
    event_subscriptions: list[str] = field(default_factory=list)
    companion_names: list[str] = field(default_factory=list)
    supervised_futures: list[Any] = field(default_factory=list)
    api_instances: list[Any] = field(default_factory=list)
    content_hash: str | None = None
    skill_dir: str | None = None
    import_root: str | None = None

    def is_empty(self) -> bool:
        return not (
            self.tools
            or self.unload_callbacks
            or self.event_subscriptions
            or self.companion_names
            or self.supervised_futures
        )


@dataclass
class _ExtensionLoadFailure:
    content_hash: str
    skill_dir: str
    error: str


@dataclass
class _PluginAPIConfig:
    skill_name: str
    permissions: Sequence[str]
    env_allowlist: Sequence[str]
    state_dir: pathlib.Path
    settings_reader: Callable[[], SettingsLike]
    storage_provider: StorageProvider | None = None
    granted_keys: Sequence[str] | None = None
    subscribe_events: Sequence[str] | None = None
    companion_processes: Sequence[dict[str, Any]] | None = None
    skill_dir: pathlib.Path | None = None
    source_kind: str = ""
    distribution_owned: bool = False
    runtime_skill_dir: pathlib.Path | None = None
    dependency_site_dirs_enabled: bool = False


# Lock-guarded registries; per-surface maps keep unload proportional to one extension.
_lock = threading.RLock()
_extensions: dict[str, _ExtensionRegistrations] = {}
_extension_modules: dict[str, ModuleType] = {}
_load_failures: dict[str, _ExtensionLoadFailure] = {}
_unloading: set[str] = set()
_lifecycle_locks: dict[str, threading.RLock] = {}
_tools: dict[str, Any] = {}  # {"ext_<len>_<token>_<name>": ToolEntry-like}
_EXTENSION_NAME_PREFIX = "ext_"
_EXTENSION_SKILL_TOKEN_MAX = 32
_EXTENSION_SHORT_MAX = 24
_EXTENSION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _extension_skill_token(skill_name: str) -> str:
    """Return a short ASCII token without changing skill identity."""
    text = str(skill_name or "").strip()
    safe = "".join(ch if (ch.isascii() and (ch.isalnum() or ch in "-_")) else "_" for ch in text)
    safe = re.sub(r"_+", "_", safe).strip("_-")
    raw_budget = _EXTENSION_SKILL_TOKEN_MAX - 2
    if safe and safe == text and len(safe) <= raw_budget:
        return f"r_{safe}"
    digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:10]
    prefix_budget = _EXTENSION_SKILL_TOKEN_MAX - len(digest) - 3
    prefix = (safe or "skill")[:prefix_budget].strip("_-") or "skill"
    return f"h_{prefix}_{digest}"


def extension_name_prefix(skill_name: str) -> str:
    """Return the provider-safe prefix for one extension."""
    token = _extension_skill_token(skill_name)
    return f"{_EXTENSION_NAME_PREFIX}{len(token)}_{token}_"


def extension_surface_name(skill_name: str, short_name: str) -> str:
    """Return a provider-safe canonical surface name."""
    full = f"{extension_name_prefix(skill_name)}{short_name}"
    if not _EXTENSION_NAME_RE.match(full):
        raise ExtensionRegistrationError(f"extension surface name {full!r} must match provider tool-name limits")
    return full


def resolve_extension_tool_name(name: str) -> str | None:
    """Resolve provider-safe names and ``skill.tool`` aliases to live tool names."""
    text = str(name or "").strip()
    if not text:
        return None
    with _lock:
        if text in _tools:
            return text
    skill_name, sep, short_name = text.rpartition(".")
    if sep != "." or not skill_name or not short_name:
        return None
    try:
        candidate = extension_surface_name(skill_name, short_name)
    except ExtensionRegistrationError:
        return None
    with _lock:
        return candidate if candidate in _tools else None


def parse_extension_surface_name(name: str) -> tuple[str, str] | None:
    """Return ``(encoded_skill_token, short_name)`` for extension surface names."""
    text = str(name or "").strip()
    if not _EXTENSION_NAME_RE.match(text) or not text.startswith(_EXTENSION_NAME_PREFIX):
        return None
    rest = text[len(_EXTENSION_NAME_PREFIX) :]
    length_text, sep, remainder = rest.partition("_")
    if sep != "_" or not length_text.isdigit():
        return None
    token_len = int(length_text)
    if token_len < 1 or len(remainder) <= token_len or remainder[token_len] != "_":
        return None
    token = remainder[:token_len]
    short = remainder[token_len + 1 :]
    return token, short


def _lifecycle_lock_for(skill_name: str) -> threading.RLock:
    with _lock:
        lock = _lifecycle_locks.get(skill_name)
        if lock is None:
            lock = threading.RLock()
            _lifecycle_locks[skill_name] = lock
        return lock


def _run_unload_callback(skill_name: str, callback: Callable[[], Any], timeout_sec: float = 2.0) -> None:
    errors: list[BaseException] = []

    def runner() -> None:
        try:
            callback()
        except BaseException as exc:  # pragma: no cover - surfaced via log
            errors.append(exc)

    thread = threading.Thread(target=runner, name=f"ouroboros-ext-unload-{skill_name}", daemon=True)
    thread.start()
    thread.join(timeout=timeout_sec)
    if thread.is_alive():
        log.warning("extension %s unload callback timed out after %.1fs", skill_name, timeout_sec)
        return
    if errors:
        exc = errors[0]
        log.warning("extension %s unload callback failed", skill_name, exc_info=(type(exc), exc, exc.__traceback__))


# PluginAPI implementation.


def _assert_log_redacted_args(tool_name: str, value: Any, schema: dict[str, Any] | None) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ExtensionRegistrationError(f"tool {tool_name!r} log_redacted_args must be a list/tuple of argument names")
    names: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ExtensionRegistrationError(f"tool {tool_name!r} log_redacted_args entries must be non-empty strings")
        names.append(item.strip())
    properties = (schema or {}).get("properties")
    if names and isinstance(properties, dict):
        unknown = sorted(set(names) - set(properties))
        if unknown:
            raise ExtensionRegistrationError(
                f"tool {tool_name!r} log_redacted_args not in schema properties: {', '.join(unknown)}"
            )
    return names


def _assert_result_limit(tool_name: str, value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExtensionRegistrationError(f"tool {tool_name!r} result_limit must be a positive int or None")
    if value <= 0:
        raise ExtensionRegistrationError(f"tool {tool_name!r} result_limit must be a positive int or None")
    if value > RESULT_LIMIT_HARD_MAX:
        raise ExtensionRegistrationError(f"tool {tool_name!r} result_limit must be <= {RESULT_LIMIT_HARD_MAX}")
    return value


def _assert_retrieval_aliases(tool_name: str, value: Any) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ExtensionRegistrationError(f"tool {tool_name!r} retrieval_aliases must be a sequence of search terms")
    aliases: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ExtensionRegistrationError(f"tool {tool_name!r} retrieval_aliases entries must be non-empty strings")
        aliases.append(item.strip())
    return aliases


def _assert_parallel_safe(tool_name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ExtensionRegistrationError(f"tool {tool_name!r} parallel_safe must be a bool")
    return value


# The declarable vocabulary lives beside the core one it narrows:
# "check_conditional" is shell-specific, skill/shell/control/git are core-internal.
_EXTENSION_SAFETY_POLICIES = DECLARABLE_SAFETY_POLICIES
_EXTENSION_CAPABILITY_CLASSES = DECLARABLE_CAPABILITY_CLASSES


def _assert_safety_policy(tool_name: str, value: Any) -> str | None:
    if value is None:
        return None  # unstated: the descriptor falls back to the core spec default ("check")
    candidate = str(value or "").strip().lower()
    if candidate not in _EXTENSION_SAFETY_POLICIES:
        raise ExtensionRegistrationError(
            f"tool {tool_name!r} safety_policy must be one of {sorted(_EXTENSION_SAFETY_POLICIES)}"
        )
    return candidate


def _assert_capability_class(tool_name: str, value: Any) -> str | None:
    if value is None:
        return None  # unstated: the descriptor falls back to the core spec default
    candidate = str(value or "").strip().lower()
    if candidate not in _EXTENSION_CAPABILITY_CLASSES:
        raise ExtensionRegistrationError(
            f"tool {tool_name!r} capability_class must be one of {sorted(_EXTENSION_CAPABILITY_CLASSES)}"
        )
    return candidate


def _assert_tool_name(name: str) -> str:
    candidate = str(name or "").strip()
    if not candidate:
        raise ExtensionRegistrationError("tool name must be non-empty")
    if len(candidate) > _EXTENSION_SHORT_MAX:
        raise ExtensionRegistrationError(f"tool name must be <= {_EXTENSION_SHORT_MAX} characters: {candidate!r}")
    if not candidate.replace("_", "").isalnum():
        raise ExtensionRegistrationError(f"tool name must be alnum/underscore only: {candidate!r}")
    return candidate


class _SkillStateStore:
    def __init__(
        self,
        provider: StorageProvider,
        *,
        skill_name: str,
        guard: Callable[[], None],
    ) -> None:
        self._provider = provider
        self._prefix = f"skills/{skill_name}/"
        self._guard = guard

    def _key(self, key: str) -> str:
        return f"{self._prefix}{normalize_storage_key(key)}"

    def get_bytes(self, key: str) -> bytes | None:
        self._guard()
        return self._provider.get_bytes(SCOPE_STATE, self._key(key))

    def put_bytes(self, key: str, data: bytes) -> None:
        self._guard()
        self._provider.put_bytes(SCOPE_STATE, self._key(key), data, content_type="application/octet-stream")

    def put_json(self, key: str, value: dict[str, Any]) -> None:
        self._guard()
        self._provider.atomic_put_json(SCOPE_STATE, self._key(key), value)

    def create_json_if_absent(self, key: str, value: dict[str, Any]) -> bool:
        self._guard()
        return self._provider.create_json_if_absent(SCOPE_STATE, self._key(key), value)

    def replace_json_if_matches(
        self,
        key: str,
        expected: dict[str, Any],
        replacement: dict[str, Any],
    ) -> bool:
        self._guard()
        return self._provider.replace_json_if_matches(SCOPE_STATE, self._key(key), expected, replacement)

    def list_keys(self, prefix: str = "") -> list[str]:
        self._guard()
        full_prefix = self._prefix + (normalize_storage_key(prefix) if prefix else "")
        return [
            key.removeprefix(self._prefix)
            for key in self._provider.list_keys(SCOPE_STATE, full_prefix)
            if key.startswith(self._prefix)
        ]

    def delete(self, key: str) -> None:
        self._guard()
        self._provider.delete(SCOPE_STATE, self._key(key))


class PluginAPIImpl:
    """PluginAPI bound to one skill, permission set, and state dir."""

    def __init__(self, config: _PluginAPIConfig | None = None, **legacy: Any) -> None:
        if config is None:
            config = _PluginAPIConfig(**legacy)
        self._skill = config.skill_name
        self._permissions = frozenset(str(p).strip() for p in (config.permissions or []))
        self._env_allow = frozenset(str(k).strip() for k in (config.env_allowlist or []))
        self._env_allow_upper = frozenset(k.upper() for k in self._env_allow)
        self._state_dir = pathlib.Path(config.state_dir)
        self._subscribe_events = frozenset(str(t).strip() for t in (config.subscribe_events or []) if str(t).strip())
        self._companion_specs = {
            str(item.get("name") or "").strip(): dict(item)
            for item in (config.companion_processes or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        }
        # Keep runtime_info cheap and tied to the loaded payload.
        self._skill_dir = pathlib.Path(config.skill_dir) if config.skill_dir is not None else None
        self._source_kind = str(config.source_kind or "").strip()
        self._distribution_owned = bool(config.distribution_owned)
        self._runtime_skill_dir = (
            pathlib.Path(config.runtime_skill_dir) if config.runtime_skill_dir is not None else self._skill_dir
        )
        self._dependency_site_dirs_enabled = bool(config.dependency_site_dirs_enabled)
        self._settings_reader = config.settings_reader
        self._state_store = (
            _SkillStateStore(
                config.storage_provider,
                skill_name=self._skill,
                guard=lambda: self._require("fs"),
            )
            if config.storage_provider is not None
            else None
        )
        self._registration_closed = False
        self._runtime_closing = False
        self._runtime_closed = False
        self._api_lock = threading.RLock()
        # Protected settings are exposed only when distribution trust has
        # authorized the matching manifest declaration.
        self._granted_upper = frozenset(str(k).strip().upper() for k in (config.granted_keys or []) if str(k).strip())

    # --- internal helpers ---

    def _require(self, perm: str) -> None:
        with _lock:
            self._require_runtime_open_locked()
        self._require_permission(perm)

    def _require_registration(self, perm: str) -> None:
        with _lock:
            self._require_registration_open_locked()
        self._require_permission(perm)

    def _require_permission(self, perm: str) -> None:
        if perm not in VALID_EXTENSION_PERMISSIONS:
            raise ExtensionRegistrationError(f"unknown extension permission {perm!r}")
        if perm not in self._permissions:
            raise ExtensionRegistrationError(
                f"skill {self._skill!r} cannot {perm!r} — manifest permissions={sorted(self._permissions)}"
            )

    def _require_runtime_open_locked(self) -> None:
        if self._runtime_closing or self._runtime_closed or self._skill in _unloading:
            raise ExtensionRegistrationError(f"skill {self._skill!r} cannot use runtime after unload has started")

    def _require_registration_open_locked(self) -> None:
        self._require_runtime_open_locked()
        if self._registration_closed:
            raise ExtensionRegistrationError(f"skill {self._skill!r} cannot register after registration has closed")

    def _wrap_runtime_handler(self, handler: Callable[..., Any]) -> Callable[..., Any]:
        skill_dir = self._skill_dir
        if skill_dir is None:
            return handler

        # ``functools.wraps`` is load-bearing, not cosmetic: it sets ``__wrapped__``, which
        # is how ``inspect.signature`` sees through to the real handler. The dispatcher
        # decides whether to pass ``ctx`` from that signature, so a bare ``*args, **kwargs``
        # wrapper would make every handler look like it takes one.
        if inspect.iscoroutinefunction(handler):

            @functools.wraps(handler)
            async def _async_wrapped(*args: Any, **kwargs: Any) -> Any:
                async with async_isolated_site_dirs_scope(
                    skill_dir,
                    enabled=self._dependency_site_dirs_enabled,
                ):
                    return await handler(*args, **kwargs)

            return _async_wrapped

        @functools.wraps(handler)
        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            with isolated_site_dirs_scope(skill_dir, enabled=self._dependency_site_dirs_enabled):
                result = handler(*args, **kwargs)
                return result

        return _wrapped

    # --- registration ---

    def register_tool(
        self,
        name: str,
        handler: Callable[..., str],
        *,
        description: str,
        schema: dict[str, Any],
        timeout_sec: int = 300,
        log_redacted_args: Sequence[str] = (),
        result_limit: int | None = None,
        retrieval_aliases: Sequence[str] = (),
        parallel_safe: bool = False,
        safety_policy: str | None = None,
        capability_class: str | None = None,
    ) -> None:
        self._require_registration("tool")
        short = _assert_tool_name(name)
        full = extension_surface_name(self._skill, short)
        redacted_args = _assert_log_redacted_args(full, log_redacted_args, schema)
        result_limit = _assert_result_limit(full, result_limit)
        aliases = _assert_retrieval_aliases(full, retrieval_aliases)
        parallel_safe = _assert_parallel_safe(full, parallel_safe)
        safety = _assert_safety_policy(full, safety_policy)
        capability = _assert_capability_class(full, capability_class)
        if parallel_safe and safety != "skip":
            # A parallel batch of "check" tools would fire that many concurrent
            # safety-LLM calls per round; only skip-policy tools batch.
            # Demoted rather than rejected: parallel_safe predates safety_policy
            # (1.8), so an existing registration must keep loading.
            log.warning(
                "tool %r declares parallel_safe without safety_policy='skip'; registering as not parallel-safe", full
            )
            parallel_safe = False
        with _lock:
            self._require_registration_open_locked()
            if full in _tools:
                raise ExtensionRegistrationError(f"tool {full!r} already registered")
            _tools[full] = {
                "name": full,
                "handler": self._wrap_runtime_handler(handler),
                "description": str(description or ""),
                "schema": dict(schema or {}),
                "timeout_sec": max(1, int(timeout_sec)),
                "log_redacted_args": redacted_args,
                "result_limit": result_limit,
                "retrieval_aliases": aliases,
                "parallel_safe": parallel_safe,
                "safety_policy": safety,
                "capability_class": capability,
                "skill": self._skill,
                "skill_dir": str(self._skill_dir) if self._skill_dir is not None else "",
                "source_kind": self._source_kind,
                "distribution_owned": self._distribution_owned,
            }
            _extensions.setdefault(self._skill, _ExtensionRegistrations()).tools.append(full)

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
        self._require("inject_chat")
        try:
            get_skill_bridge().enqueue_skill_message(
                self._skill,
                text,
                chat_id=chat_id,
                user_id=user_id,
                sender_label=sender_label,
                image_base64=image_base64,
                image_mime=image_mime,
                image_caption=image_caption,
                transport=transport,
                delivery_source=delivery_source,
            )
        except SkillBridgeError as exc:
            raise ExtensionRegistrationError(str(exc)) from exc

    def send_message(
        self,
        text: str,
        *,
        chat_id: int,
        source: str,
        task_id: str = "",
    ) -> None:
        self._require("send_message")
        try:
            get_skill_bridge().send_skill_message(
                self._skill,
                text,
                chat_id=chat_id,
                source=source,
                task_id=task_id,
            )
        except Exception as exc:
            raise ExtensionRegistrationError(str(exc)) from exc

    def allocate_internal_chat_id(self, range_name: str = "a2a") -> int:
        self._require("inject_chat")
        try:
            return get_skill_bridge().allocate_internal_chat_id(self._skill, range_name)
        except (SkillBridgeError, ValueError) as exc:
            raise ExtensionRegistrationError(str(exc)) from exc

    def register_supervised_task(
        self,
        name: str,
        factory: Callable[[], Any],
        *,
        restart_policy: str = "on_failure",
        max_restarts: int = 5,
        backoff_seconds: float = 2.0,
    ) -> None:
        """Declare a server-owned supervised task; workers only record it."""
        self._require_registration("supervised_task")
        clean_name = _assert_tool_name(name)
        future = None
        if is_server_process():
            loop = getattr(get_global_event_bus(), "_loop", None)
            if loop is not None and loop.is_running():

                async def _runner() -> None:
                    restarts = 0
                    while True:
                        try:
                            result = factory()
                            if inspect.isawaitable(result):
                                await result
                            return
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            restarts += 1
                            if restart_policy != "on_failure" or restarts > max_restarts:
                                log.warning(
                                    "supervised task %s/%s stopped after failure",
                                    self._skill,
                                    clean_name,
                                    exc_info=True,
                                )
                                return
                            await asyncio.sleep(max(0.1, float(backoff_seconds)))

                future = asyncio.run_coroutine_threadsafe(_runner(), loop)
        with _lock:
            self._require_registration_open_locked()
            bundle = _extensions.setdefault(self._skill, _ExtensionRegistrations())
            bundle.companion_names.append(f"task:{clean_name}")
            if future is not None:
                bundle.supervised_futures.append(future)

    def register_companion_process(
        self,
        name: str,
    ) -> None:
        self._require_registration("companion_process")
        clean_name = _assert_tool_name(name)
        spec = self._companion_specs.get(clean_name)
        if spec is None:
            raise ExtensionRegistrationError(
                f"companion {clean_name!r} is not declared in manifest.companion_processes"
            )
        expected_cmd = [str(part) for part in (spec.get("command") or []) if str(part)]
        expected_runtime = str(spec.get("runtime") or "").strip()
        cmd = list(expected_cmd)
        if not cmd:
            raise ExtensionRegistrationError("companion command must be declared in manifest")
        if expected_runtime in {"python", "python3"} and cmd[0] in {"python", "python3"}:
            cmd = [sys.executable, *cmd[1:]]
        if not is_server_process():
            with _lock:
                _extensions.setdefault(self._skill, _ExtensionRegistrations()).companion_names.append(
                    f"worker-skip:{clean_name}"
                )
            return
        supervisor = get_global_supervisor()
        if supervisor is None:
            raise ExtensionRegistrationError("companion supervisor is not initialized")
        base_env = _scrub_env(
            list(self._env_allow),
            self._state_dir,
            self._skill,
            granted_keys=list(self._granted_upper),
        )
        for key, value in (spec.get("env") or {}).items():
            key_text = str(key)
            if key_text.upper() in FORBIDDEN_EXTENSION_SETTINGS:
                continue
            base_env[key_text] = str(value)
        if self._skill_dir is not None:
            site_dirs = [str(path) for path in _isolated_python_site_dirs(self._skill_dir)]
            if site_dirs:
                existing_pythonpath = base_env.get("PYTHONPATH")
                base_env["PYTHONPATH"] = os.pathsep.join(
                    [*site_dirs, existing_pythonpath] if existing_pythonpath else site_dirs
                )
        workdir = self._runtime_skill_dir or self._skill_dir or self._state_dir
        descriptor = CompanionDescriptor(
            skill_name=self._skill,
            name=clean_name,
            command=cmd,
            cwd=workdir,
            env=base_env,
            ports=[int(port) for port in (spec.get("ports") or []) if str(port).isdigit()],
            restart_policy=str(spec.get("restart_policy") or "on_failure"),
            max_restarts=max(0, int(spec.get("max_restarts") or 5)),
        )
        supervisor.start(descriptor)
        with _lock:
            _extensions.setdefault(self._skill, _ExtensionRegistrations()).companion_names.append(clean_name)

    def subscribe_event(self, topic: str, handler: Callable[[dict[str, Any]], Any]) -> str:
        self._require_registration("subscribe_event")
        topic = str(topic or "").strip()
        if topic not in self._subscribe_events:
            raise ExtensionRegistrationError(f"skill {self._skill!r} cannot subscribe to undeclared topic {topic!r}")
        sub_id = get_global_event_bus().subscribe(self._skill, topic, self._wrap_runtime_handler(handler))
        with _lock:
            _extensions.setdefault(self._skill, _ExtensionRegistrations()).event_subscriptions.append(sub_id)
        return sub_id

    def on_unload(self, callback: Callable[[], Any]) -> None:
        if not callable(callback):
            raise ExtensionRegistrationError("on_unload callback must be callable")
        with _lock:
            self._require_registration_open_locked()
            _extensions.setdefault(self._skill, _ExtensionRegistrations()).unload_callbacks.append(callback)

    def _close_registration(self) -> None:
        with _lock:
            self._registration_closed = True

    def _close_runtime_access(self) -> None:
        with _lock:
            self._registration_closed = True
            self._runtime_closing = True
        with self._api_lock, _lock:
            self._runtime_closed = True

    # --- runtime access ---

    def log(self, level: str, message: str, **fields: Any) -> None:
        lvl = str(level or "info").lower()
        levels = {"debug": 10, "info": 20, "warning": 30, "error": 40}
        log.log(
            levels.get(lvl, 20),
            "[ext %s] %s %s",
            self._skill,
            message,
            fields if fields else "",
        )

    def get_settings(self, keys: Sequence[str]) -> dict[str, Any]:
        with self._api_lock:
            with _lock:
                if self._runtime_closing or self._runtime_closed or self._skill in _unloading:
                    return {}
            if "read_settings" not in self._permissions:
                # Missing permission fails closed without leaking key presence.
                return {}
            settings = self._settings_reader() or {}
            with _lock:
                if self._runtime_closing or self._runtime_closed or self._skill in _unloading:
                    return {}
            out: dict[str, Any] = {}
            protected_upper = {k.upper() for k in FORBIDDEN_EXTENSION_SETTINGS}
            protected_upper.update(requested_core_setting_keys(list(self._env_allow)))
            for raw_key in keys or ():
                key = str(raw_key).strip()
                canonical = key.upper()
                if not key:
                    continue
                if canonical in protected_upper and canonical not in self._granted_upper:
                    # Do not reveal forbidden/core key presence without a grant.
                    continue
                if key not in self._env_allow and canonical not in self._env_allow_upper:
                    continue
                settings_key = canonical if canonical in protected_upper else key
                if settings_key in settings:
                    out[settings_key] = settings[settings_key]
            return out

    def get_state_dir(self) -> str:
        return str(self._state_dir)

    def get_state_store(self) -> SkillStateStore:
        self._require("fs")
        if self._state_store is None:
            raise ExtensionRegistrationError("skill state storage is unavailable")
        return self._state_store

    def skill_job_dir(self, job_id: str) -> pathlib.Path:
        raw = str(job_id or "").strip()
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in raw).strip("._")
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
        prefix = (safe or "_job")[:55].rstrip("._-") or "_job"
        safe = f"{prefix}-{digest}"
        root = self._state_dir / "jobs" / safe
        for child in ("assets", "output", "tmp"):
            (root / child).mkdir(parents=True, exist_ok=True)
        return root

    def get_runtime_info(self) -> dict[str, Any]:
        """Return the PluginAPI runtime-info snapshot without manifest I/O."""
        try:
            from ouroboros.app.config import DATA_DIR as _DATA_DIR

            data_dir = str(_DATA_DIR)
        except Exception:
            data_dir = ""
        try:
            from ouroboros.app.config import AGENT_SERVER_PORT as _agent_port, PORT_FILE as _PORT_FILE

            server_port = 0
            try:
                port_text = pathlib.Path(_PORT_FILE).read_text(encoding="utf-8").strip()
                if port_text:
                    server_port = int(port_text)
            except Exception:
                server_port = 0
            if server_port <= 0:
                server_port = int(_agent_port)
        except Exception:
            server_port = 0
        skill_dir = str(getattr(self, "_skill_dir", "") or "")
        return {
            "data_dir": data_dir,
            "skill_dir": skill_dir,
            "state_dir": str(self._state_dir),
            "server_port": server_port,
        }


# Loader.


def _plugin_entry_path(skill: LoadedSkill) -> pathlib.Path | None:
    """Resolve manifest.entry inside the skill directory."""
    entry = str(skill.manifest.entry or "").strip()
    if not entry:
        return None
    candidate = (skill.skill_dir / entry).resolve()
    try:
        candidate.relative_to(skill.skill_dir.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _module_key(skill_name: str) -> str:
    digest = hashlib.sha1(str(skill_name or "").encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"ouroboros._extensions.m_{digest}"


def _purge_extension_bytecode(skill_dir: pathlib.Path) -> None:
    """Drop bytecode so rapid edits reload fresh source."""
    for pycache in skill_dir.rglob("__pycache__"):
        if pycache.is_dir():
            shutil.rmtree(pycache, ignore_errors=True)


def _stage_extension_import_tree(
    skill: LoadedSkill,
    *,
    entry_path: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Stage an extension under a fresh import root to avoid stale module reuse."""
    resolved_root = skill.skill_dir.resolve()
    relative_entry = entry_path.relative_to(resolved_root)
    for path in sorted(skill.skill_dir.rglob("*")):
        if is_skill_cache_path(path, resolved_root):
            continue
        if not path.is_symlink():
            continue
        try:
            resolved = path.resolve()
            resolved.relative_to(resolved_root)
        except Exception as exc:
            raise RuntimeError(
                f"extension {skill.name!r} contains a symlink that resolves outside the skill tree: {path}"
            ) from exc
    import_root = pathlib.Path(
        tempfile.mkdtemp(
            prefix=f"ouroboros-{re.sub(r'[^A-Za-z0-9_-]+', '-', skill.name)[:32] or 'skill'}-",
            dir="/tmp",
        )
    )
    staged_skill_dir = import_root / "skill"
    shutil.copytree(
        skill.skill_dir,
        staged_skill_dir,
        ignore=shutil.ignore_patterns(*_SKILL_DIR_CACHE_NAMES),
    )
    _purge_extension_bytecode(staged_skill_dir)
    staged_entry = (staged_skill_dir / relative_entry).resolve()
    staged_entry.relative_to(staged_skill_dir.resolve())
    return import_root, staged_entry


def _extension_runtime_state(
    skill: LoadedSkill,
    *,
    current_hash: str | None = None,
) -> dict[str, Any]:
    """Return the liveness authority for one extension."""
    hash_now = current_hash or skill.content_hash
    skill_dir_now = str(skill.skill_dir.resolve())
    with _lock:
        live_bundle = _extensions.get(skill.name)
        live_loaded = bool(
            live_bundle and live_bundle.content_hash == hash_now and live_bundle.skill_dir == skill_dir_now
        )
        loaded_present = live_bundle is not None
        load_failure = _load_failures.get(skill.name)
        matched_failure = bool(
            load_failure and load_failure.content_hash == hash_now and load_failure.skill_dir == skill_dir_now
        )

    reason = "ready"
    desired_live = True
    if not skill.manifest.is_extension():
        desired_live = False
        reason = "not_extension"
    elif skill.load_error:
        desired_live = False
        reason = "load_error"
    elif not skill.selected or not skill.eligible:
        desired_live = False
        reason = next(iter(skill.blocked_reasons), "not_eligible")
    elif not skill.enabled:
        desired_live = False
        reason = "disabled"
    elif matched_failure:
        reason = "load_error"

    return {
        "skill": skill.name,
        "type": skill.manifest.type,
        "enabled": skill.enabled,
        "load_error": skill.load_error or (load_failure.error if matched_failure and load_failure else None),
        "desired_live": desired_live,
        "live_loaded": live_loaded,
        "loaded_present": loaded_present,
        "loaded_matches_current": live_loaded,
        "reason": reason,
    }


def _deps_block_reason(drive_root: pathlib.Path, skill: LoadedSkill) -> str:
    """Return the dependency block reason, if live dispatch must refuse load."""
    try:
        from ouroboros.skills.dependencies import auto_install_specs_for_skill
        from ouroboros.skills.install_specs import install_specs_hash
        from ouroboros.skills.isolated_deps import read_deps_state

        auto_specs = auto_install_specs_for_skill(drive_root, skill)
        if not auto_specs:
            return ""
        deps_state = read_deps_state(drive_root, skill.name)
        status = str(deps_state.get("status") or "")
        if status != "installed":
            return "deps_failed" if status == "failed" else "deps_missing"
        if deps_state.get("specs_hash") != install_specs_hash(auto_specs):
            return "deps_stale"
        return ""
    except Exception:
        log.debug("extension deps readiness probe failed", exc_info=True)
        return ""


def runtime_state_for_skill_name(
    skill_name: str,
    drive_root: pathlib.Path,
    *,
    repo_path: str | None = None,
    workspace_root: pathlib.Path | None = None,
) -> dict[str, Any]:
    resolved_repo_path = repo_path
    resolved_workspace_root = pathlib.Path(REPO_DIR) if workspace_root is None and repo_path is None else workspace_root
    skill = find_skill(
        drive_root,
        skill_name,
        repo_path=resolved_repo_path,
        workspace_root=resolved_workspace_root,
    )
    if skill is None:
        with _lock:
            live_loaded = skill_name in _extensions
        return {
            "skill": skill_name,
            "type": "extension",
            "enabled": False,
            "load_error": "skill not found",
            "desired_live": False,
            "live_loaded": live_loaded,
            "loaded_present": live_loaded,
            "loaded_matches_current": False,
            "reason": "missing",
        }
    state = _extension_runtime_state(skill)
    if state.get("desired_live"):
        deps_reason = _deps_block_reason(pathlib.Path(drive_root), skill)
        if deps_reason:
            state["desired_live"] = False
            state["reason"] = deps_reason
            state["load_error"] = deps_reason
    return state


def runtime_state_for_loaded_skill(skill: LoadedSkill, drive_root: pathlib.Path | None = None) -> dict[str, Any]:
    """Runtime state for an already-discovered skill; avoids repeated FS walks."""
    state = _extension_runtime_state(skill)
    if drive_root is not None and state.get("desired_live"):
        deps_reason = _deps_block_reason(pathlib.Path(drive_root), skill)
        if deps_reason:
            state["desired_live"] = False
            state["reason"] = deps_reason
            state["load_error"] = deps_reason
    return state


def is_extension_live(
    skill_name: str,
    drive_root: pathlib.Path,
    *,
    repo_path: str | None = None,
    workspace_root: pathlib.Path | None = None,
) -> bool:
    state = runtime_state_for_skill_name(
        skill_name,
        drive_root,
        repo_path=repo_path,
        workspace_root=workspace_root,
    )
    return bool(state.get("desired_live")) and bool(state.get("live_loaded"))


def reconcile_extension(
    skill_name: str,
    drive_root: pathlib.Path,
    settings_reader: Callable[[], SettingsLike],
    *,
    repo_path: str | None = None,
    workspace_root: pathlib.Path | None = None,
    retry_load_error: bool = False,
    preloaded_skill: LoadedSkill | None = None,
    storage_provider: StorageProvider | None = None,
) -> dict[str, Any]:
    """Reconcile one extension's desired and actual live state."""
    resolved_workspace_root = pathlib.Path(REPO_DIR) if workspace_root is None and repo_path is None else workspace_root

    def current_state() -> dict[str, Any]:
        if preloaded_skill is not None:
            return runtime_state_for_loaded_skill(preloaded_skill, drive_root)
        return runtime_state_for_skill_name(
            skill_name,
            drive_root,
            repo_path=repo_path,
            workspace_root=resolved_workspace_root,
        )

    lifecycle_lock = _lifecycle_lock_for(skill_name)
    with lifecycle_lock:
        state = current_state()
        loaded_present = bool(state.get("loaded_present"))
        was_live = bool(state.get("live_loaded"))
        if retry_load_error and state.get("reason") == "load_error" and not was_live:
            with _lock:
                _load_failures.pop(skill_name, None)
            state = current_state()
            loaded_present = bool(state.get("loaded_present"))
            was_live = bool(state.get("live_loaded"))
        elif state.get("reason") == "load_error" and not loaded_present:
            state["action"] = "extension_load_error"
            return state
        if state.get("reason") == "missing" or state.get("reason") == "not_extension":
            if loaded_present:
                unload_extension(skill_name)
            state["action"] = "extension_unloaded" if loaded_present else "extension_inactive"
            state["live_loaded"] = False
            state["loaded_present"] = False
            return state

        if not state.get("desired_live"):
            if loaded_present:
                unload_extension(skill_name)
            state["action"] = "extension_unloaded" if loaded_present else "extension_inactive"
            state["live_loaded"] = False
            state["loaded_present"] = False
            return state

        if was_live:
            state["action"] = "extension_already_live"
            return state

        loaded = preloaded_skill
        if loaded is None:
            loaded = find_skill(
                drive_root,
                skill_name,
                repo_path=repo_path,
                workspace_root=resolved_workspace_root,
            )
        if loaded is None:
            state["reason"] = "missing"
            state["action"] = "extension_inactive"
            return state
        if loaded_present:
            unload_extension(skill_name)
        err = load_extension(
            loaded,
            settings_reader,
            drive_root=drive_root,
            storage_provider=storage_provider,
        )
        if err:
            with _lock:
                _load_failures[skill_name] = _ExtensionLoadFailure(
                    content_hash=loaded.content_hash,
                    skill_dir=str(loaded.skill_dir.resolve()),
                    error=err,
                )
            state["reason"] = "load_error"
            state["load_error"] = err
            state["action"] = "extension_load_error"
            return state
        refreshed = current_state()
        refreshed["action"] = "extension_loaded"
        return refreshed


def load_extension(
    skill: LoadedSkill,
    settings_reader: Callable[[], SettingsLike],
    *,
    drive_root: pathlib.Path | None = None,
    storage_provider: StorageProvider | None = None,
) -> str | None:
    """Load a trusted extension with manifest/dependency gates, returning a UI-safe error.

    ``drive_root`` must be explicit; defaulting to owner data would pollute
    tests and alternate-drive runtimes.
    """
    if drive_root is None:
        raise TypeError("load_extension requires explicit drive_root")
    if not skill.manifest.is_extension():
        return f"skill {skill.name!r} is not type=extension"
    if skill.load_error:
        return f"skill {skill.name!r} has load_error: {skill.load_error}"
    if not skill.enabled:
        return f"skill {skill.name!r} is disabled"
    try:
        current_hash = compute_content_hash(
            skill.skill_dir,
            manifest_entry=skill.manifest.entry,
            manifest_scripts=skill.manifest.scripts,
        )
    except SkillPayloadUnreadable as exc:
        return f"skill {skill.name!r} payload unreadable at load time: {exc}. Fix filesystem state and re-enable."
    skill.content_hash = current_hash
    runtime_state = _extension_runtime_state(skill, current_hash=current_hash)
    if runtime_state["reason"] == "disabled":
        return f"skill {skill.name!r} is disabled"
    entry_path = _plugin_entry_path(skill)
    if entry_path is None:
        return f"skill {skill.name!r} manifest.entry does not resolve to a file inside the skill directory"

    drive_root = pathlib.Path(drive_root)
    state_dir = skill_state_dir(drive_root, skill.name)
    try:
        from ouroboros.skills.dependencies import auto_install_specs_for_skill

        auto_specs = auto_install_specs_for_skill(pathlib.Path(drive_root), skill)
    except Exception:
        log.debug("extension dependency spec probe failed for %s", skill.name, exc_info=True)
        auto_specs = []
    if auto_specs:
        deps_reason = _deps_block_reason(pathlib.Path(drive_root), skill)
        if deps_reason:
            return f"skill {skill.name!r} cannot load until isolated dependencies are ready: {deps_reason}"

    # Distribution trust authorizes only the capabilities/settings in the manifest.
    grant_status = grant_status_for_skill(pathlib.Path(drive_root), skill)
    if not grant_status.get("all_granted", True):
        missing_bits = []
        if grant_status.get("missing_keys"):
            missing_bits.append(f"keys={grant_status.get('missing_keys')}")
        if grant_status.get("missing_permissions"):
            missing_bits.append(f"permissions={grant_status.get('missing_permissions')}")
        return (
            f"skill {skill.name!r} is missing owner grants for "
            f"{', '.join(missing_bits)}. Grant access from the Skills tab."
        )
    requested_core = requested_core_setting_keys(list(skill.manifest.env_from_settings or []))
    granted_set = set(grant_status.get("granted_keys") or [])
    granted_core = [key for key in requested_core if key in granted_set]
    staged_import_root: pathlib.Path | None = None
    module_key = _module_key(skill.name)
    try:
        importlib.invalidate_caches()
        staged_import_root, entry_path = _stage_extension_import_tree(
            skill,
            entry_path=entry_path,
        )
        # Package-style spec preserves relative imports from the staged entry dir.
        spec = importlib.util.spec_from_file_location(
            module_key,
            entry_path,
            submodule_search_locations=[str(entry_path.parent)],
        )
        if spec is None or spec.loader is None:
            return f"skill {skill.name!r}: importlib could not build spec"
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_key] = module
        with isolated_site_dirs_scope(skill.skill_dir, enabled=bool(auto_specs)):
            spec.loader.exec_module(module)
            register = getattr(module, "register", None)
            if not callable(register):
                # Sibling imports may already be in sys.modules; purge the package.
                unload_extension(skill.name)
                return f"skill {skill.name!r} plugin.py does not export a register(api) callable"
            api = PluginAPIImpl(
                _PluginAPIConfig(
                    skill_name=skill.name,
                    permissions=list(skill.manifest.permissions or []),
                    env_allowlist=list(skill.manifest.env_from_settings or []),
                    state_dir=state_dir,
                    settings_reader=settings_reader,
                    storage_provider=storage_provider,
                    granted_keys=granted_core,
                    subscribe_events=list(getattr(skill.manifest, "subscribe_events", []) or []),
                    companion_processes=list(getattr(skill.manifest, "companion_processes", []) or []),
                    skill_dir=skill.skill_dir,
                    source_kind=str(skill.source_kind or skill.source or ""),
                    distribution_owned=bool(skill.distribution_owned),
                    runtime_skill_dir=(staged_import_root / "skill") if staged_import_root is not None else None,
                    dependency_site_dirs_enabled=bool(auto_specs),
                )
            )
            with _lock:
                bundle = _extensions.get(skill.name)
                if bundle is None:
                    bundle = _ExtensionRegistrations()
                    _extensions[skill.name] = bundle
                bundle.content_hash = current_hash
                bundle.skill_dir = str(skill.skill_dir.resolve())
                bundle.import_root = str(staged_import_root) if staged_import_root is not None else None
                bundle.api_instances.append(api)
                _extension_modules[skill.name] = module
                _load_failures.pop(skill.name, None)
            register(api)
            api._close_registration()
    except ExtensionRegistrationError as exc:
        # Registration may be partial; always tear it down.
        unload_extension(skill.name)
        return f"skill {skill.name!r} registration error: {exc}"
    except Exception as exc:
        unload_extension(skill.name)
        log.exception("extension %s failed to load", skill.name)
        return f"skill {skill.name!r} load failure: {type(exc).__name__}: {exc}"
    finally:
        if skill.name not in _extensions and staged_import_root is not None:
            shutil.rmtree(staged_import_root, ignore_errors=True)
    return None


def unload_extension(skill_name: str) -> None:
    lifecycle_lock = _lifecycle_lock_for(skill_name)
    with lifecycle_lock:
        _unload_extension_locked(skill_name)


def _unload_extension_locked(skill_name: str) -> None:
    """Remove one extension's surfaces and purge its package from sys.modules."""
    with _lock:
        bundle = _extensions.pop(skill_name, None)
        _extension_modules.pop(skill_name, None)
        import_root = pathlib.Path(bundle.import_root) if bundle and bundle.import_root else None
        callbacks = list(bundle.unload_callbacks) if bundle else []
        api_instances = list(bundle.api_instances) if bundle else []
        event_subscriptions = list(bundle.event_subscriptions) if bundle else []
        companion_names = list(bundle.companion_names) if bundle else []
        supervised_futures = list(bundle.supervised_futures) if bundle else []
        if bundle:
            _unloading.add(skill_name)
        if bundle:
            for key in bundle.tools:
                _tools.pop(key, None)
    bus = get_global_event_bus()
    for sub_id in event_subscriptions:
        bus.unsubscribe(sub_id)
    for future in supervised_futures:
        try:
            future.cancel()
        except Exception:
            log.debug("Failed to cancel supervised task for %s", skill_name, exc_info=True)
    supervisor = get_global_supervisor()
    if supervisor is not None:
        for raw_name in companion_names:
            name = str(raw_name or "")
            if name and not name.startswith(("task:", "worker-skip:")):
                supervisor.stop(skill_name, name)
    for api in api_instances:
        close = getattr(api, "_close_runtime_access", None)
        if callable(close):
            close()
    try:
        for callback in callbacks:
            _run_unload_callback(skill_name, callback)
        prefix = _module_key(skill_name)
        # Copy keys before mutating sys.modules.
        for mod_name in list(sys.modules.keys()):
            if mod_name == prefix or mod_name.startswith(prefix + "."):
                sys.modules.pop(mod_name, None)
        if import_root is not None:
            shutil.rmtree(import_root, ignore_errors=True)
    finally:
        with _lock:
            _unloading.discard(skill_name)


def reload_all(
    drive_root: pathlib.Path,
    settings_reader: Callable[[], SettingsLike],
    *,
    repo_path: str | None = None,
    workspace_root: pathlib.Path | None = None,
    storage_provider: StorageProvider | None = None,
) -> dict[str, Any]:
    """Refresh all extension liveness and return ``skill: error_or_None``."""
    resolved_workspace_root = pathlib.Path(REPO_DIR) if workspace_root is None and repo_path is None else workspace_root
    skills = discover_skills(drive_root, repo_path=repo_path, workspace_root=resolved_workspace_root)
    skill_names = {s.name for s in skills if s.manifest.is_extension()}
    with _lock:
        loaded_names = set(_extensions.keys())
    for gone in loaded_names - skill_names:
        unload_extension(gone)
    results: dict[str, Any] = {}
    for skill in skills:
        if not skill.manifest.is_extension():
            continue
        state = reconcile_extension(
            skill.name,
            drive_root,
            settings_reader,
            repo_path=repo_path,
            workspace_root=resolved_workspace_root,
            retry_load_error=True,
            preloaded_skill=skill,
            storage_provider=storage_provider,
        )
        results[skill.name] = state.get("load_error") or (None if state.get("desired_live") else state.get("reason"))
    return results


def snapshot() -> dict[str, Any]:
    """Return a read-only snapshot of live extension surfaces."""
    with _lock:
        return {
            "extensions": sorted(_extensions.keys()),
            "tools": sorted(_tools.keys()),
        }


def get_tool(name: str) -> dict[str, Any] | None:
    """Return the registered extension tool, if any."""
    resolved_name = resolve_extension_tool_name(name)
    if resolved_name is None:
        return None
    with _lock:
        return dict(_tools.get(resolved_name) or {}) or None


def registered_tools() -> list[dict[str, Any]]:
    """Return a copy of every registered extension tool record.

    The public read of the tool catalog: ``_tools`` and ``_lock`` stay private to this
    module. Records are copied, so a caller cannot mutate the registry through them.
    """
    with _lock:
        return [dict(tool) for tool in _tools.values()]


__all__ = [
    "PluginAPIImpl",
    "get_tool",
    "is_extension_live",
    "load_extension",
    "reconcile_extension",
    "registered_tools",
    "reload_all",
    "resolve_extension_tool_name",
    "runtime_state_for_skill_name",
    "snapshot",
    "unload_extension",
]
