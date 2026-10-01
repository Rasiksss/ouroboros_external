"""In-process skill ingress: enqueue owner messages without HTTP host service."""

from __future__ import annotations

import logging
import pathlib
import re
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any

from ouroboros.contracts.chat_id_policy import SKILL_BRIDGE_CHAT_ID_MAX, SKILL_BRIDGE_CHAT_ID_MIN
from ouroboros.skills.loader import (
    find_skill,
    grant_status_for_skill,
)
from ouroboros.utils import atomic_write_json, read_json_dict, utc_now_iso

log = logging.getLogger(__name__)

AUTH_TOKEN_FILENAME = "auth_token.json"
_SLASH_COMMAND_RE = re.compile(r"^\s*/[A-Za-z]")


class SkillBridgeError(Exception):
    """Raised when a skill cannot use inject_chat."""


class _RateLimiter:
    def __init__(self, limit: int = 60, window_sec: float = 60.0):
        self.limit = limit
        self.window_sec = window_sec
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window_sec:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


class SkillBridge:
    """Permission-checked bridge from extension skills into the message bus."""

    def __init__(
        self,
        data_dir: pathlib.Path,
        *,
        bridge_getter: Callable[[], Any] | None = None,
    ):
        self.data_dir = pathlib.Path(data_dir)
        self.bridge_getter = bridge_getter or self._default_bridge
        self.rate_limiter = _RateLimiter()
        self._inflight: dict[str, int] = defaultdict(int)
        self._inflight_lock = threading.Lock()
        self._counter_lock = threading.Lock()

    def _default_bridge(self) -> Any:
        from supervisor.message_bus import try_get_bridge

        bridge = try_get_bridge()
        if bridge is None:
            raise SkillBridgeError("message bridge is not initialized")
        return bridge

    @property
    def skills_state_dir(self) -> pathlib.Path:
        return self.data_dir / "state" / "skills"

    def require_permission(self, skill_name: str, permission: str) -> None:
        loaded = find_skill(self.data_dir, skill_name)
        if loaded is None:
            raise SkillBridgeError(f"skill {skill_name!r} is not installed")
        if loaded.load_error:
            raise SkillBridgeError(f"skill {skill_name!r} failed to load: {loaded.load_error}")
        if not loaded.enabled:
            raise SkillBridgeError(f"skill {skill_name!r} is disabled")
        status = grant_status_for_skill(self.data_dir, loaded)
        granted = set(status.get("granted_permissions") or [])
        if permission not in granted:
            raise SkillBridgeError(f"skill {skill_name!r} lacks grant {permission!r}")

    def _enter_inflight(self, skill_name: str, limit: int = 5) -> bool:
        with self._inflight_lock:
            current = self._inflight[skill_name]
            if current >= limit:
                return False
            self._inflight[skill_name] = current + 1
            return True

    def _leave_inflight(self, skill_name: str) -> None:
        with self._inflight_lock:
            self._inflight[skill_name] = max(0, self._inflight[skill_name] - 1)

    def allocate_internal_chat_id(self, skill_name: str, range_name: str = "a2a") -> int:
        # ``"a2a"`` is the range's published name and stays one; the ids come from the skill
        # bridge's own window, which A2A derivation is kept below.
        if range_name != "a2a":
            raise ValueError("unsupported internal chat id range")
        self.require_permission(skill_name, "inject_chat")
        counter_path = self.skills_state_dir / skill_name / "chat_id_counter.json"
        with self._counter_lock:
            data = read_json_dict(counter_path) or {}
            next_id = int(data.get("next_chat_id") or SKILL_BRIDGE_CHAT_ID_MAX)
            chat_id = next_id
            if chat_id < SKILL_BRIDGE_CHAT_ID_MIN:
                chat_id = SKILL_BRIDGE_CHAT_ID_MAX
            atomic_write_json(
                counter_path,
                {
                    "range_name": range_name,
                    "last_chat_id": chat_id,
                    "next_chat_id": chat_id - 1,
                    "updated_at": utc_now_iso(),
                },
            )
            return chat_id

    def enqueue_skill_message(
        self,
        skill_name: str,
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
        """Enqueue a message from an extension skill into the supervisor inbox."""
        self.require_permission(skill_name, "inject_chat")
        clean_text = str(text or "").strip()
        caption_text = str(image_caption or "").strip()
        if _SLASH_COMMAND_RE.match(clean_text) or _SLASH_COMMAND_RE.match(caption_text):
            raise SkillBridgeError("slash commands are reserved for direct owner input")
        if not self.rate_limiter.allow(f"{skill_name}:inject"):
            raise SkillBridgeError("rate limit exceeded")
        if not self._enter_inflight(skill_name):
            raise SkillBridgeError("too many in-flight inject requests")
        try:
            bridge = self.bridge_getter()
            outbound_source = str(delivery_source or "").strip().lower()
            if outbound_source and outbound_source not in {"browser", "dialogs", "sberchat", "web"}:
                raise SkillBridgeError(f"unsupported delivery source {outbound_source!r}")
            bridge.enqueue_local_message(
                clean_text,
                chat_id=chat_id,
                user_id=user_id,
                source=outbound_source or f"skill:{skill_name}",
                sender_label=str(sender_label or skill_name),
                image_base64=str(image_base64 or ""),
                image_mime=str(image_mime or ""),
                image_caption=caption_text,
                transport=dict(transport or {}),
            )
        finally:
            self._leave_inflight(skill_name)

    def send_skill_message(
        self,
        skill_name: str,
        text: str,
        *,
        chat_id: int,
        source: str,
        task_id: str = "",
    ) -> None:
        """Deliver extension output through the canonical outbound flow."""
        self.require_permission(skill_name, "send_message")
        clean_text = str(text or "").strip()
        if not clean_text:
            raise SkillBridgeError("outbound message must be non-empty")
        if int(chat_id) <= 0:
            raise SkillBridgeError("outbound message requires a valid chat id")
        if not self.rate_limiter.allow(f"{skill_name}:send"):
            raise SkillBridgeError("rate limit exceeded")
        from supervisor.message_bus import send_outbound_message

        send_outbound_message(
            int(chat_id),
            clean_text,
            source=str(source or "dialogs"),
            task_id=str(task_id or ""),
        )


_global_bridge: SkillBridge | None = None
_global_lock = threading.Lock()


def get_skill_bridge(data_dir: pathlib.Path | None = None) -> SkillBridge:
    global _global_bridge
    with _global_lock:
        if _global_bridge is None:
            if data_dir is None:
                from ouroboros.app.config import DATA_DIR

                data_dir = DATA_DIR
            _global_bridge = SkillBridge(pathlib.Path(data_dir))
        return _global_bridge


def reset_skill_bridge_for_tests() -> None:
    global _global_bridge
    with _global_lock:
        _global_bridge = None


__all__ = [
    "AUTH_TOKEN_FILENAME",
    "SkillBridge",
    "SkillBridgeError",
    "enqueue_skill_message",
    "get_skill_bridge",
    "reset_skill_bridge_for_tests",
]


def enqueue_skill_message(skill_name: str, text: str, **kwargs: Any) -> None:
    get_skill_bridge().enqueue_skill_message(skill_name, text, **kwargs)
