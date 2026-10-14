"""In-process transport for extension skills acting as user and as agent.

Producer-only: a skill submits messages through the inbox and publishes answers
through the outbox, and reads nothing back. That makes it the cheapest client of
the seam and the first one migrated — it proves the shape without a delivery
channel of its own.

Like every module here it imports the seam and shared infrastructure, never the
supervisor: the gateway arrives through the constructor.
"""

from __future__ import annotations

import logging
import pathlib
import threading
import time
import uuid
from collections import defaultdict, deque
from typing import Any

from background_memory.infra.events import announce_chat_message_persisted
from background_memory.interfaces import IMemoryEventPublisher
from new_core.identity import OwnerScope
from new_core.skills.models import SkillId
from new_core.skills.permissions import ISkillPermissionChecker
from ouroboros.contracts.chat_commands import confirmation_answer, is_slash_command
from ouroboros.contracts.chat_id_policy import SKILL_BRIDGE_CHAT_ID_MAX, SKILL_BRIDGE_CHAT_ID_MIN
from ouroboros.contracts.owner_scope import (
    ServiceJob,
    SurfaceId,
    configured_agent_id,
    configured_owner_id,
    service_identity,
)
from ouroboros.storage.chat_store import append_chat_message
from ouroboros.storage.settings.store import load_settings
from ouroboros.utils import atomic_write_json, read_json_dict, utc_now_iso
from transport.inbound_limits import oversized_message_refusal
from transport.interfaces import (
    DELIVERY_SOURCES,
    IMessageGateway,
    InboundEnvelope,
    OutboundMessage,
)

log = logging.getLogger(__name__)

TRANSPORT_NAME = "skill-bridge"


class SkillBridgeError(Exception):
    """Raised when a skill cannot use inject_chat."""


def _synthetic_scope_for(delivery_source: str, chat_id: int) -> OwnerScope:
    """A synthetic scope for one extension message: this instance, acting for nobody.

    An extension runs on behalf of the instance, not of the person whose message triggered
    it, and nothing in the plugin API carries an author. So the ownership pair is this
    instance's configuration — which names a real person only while the instance has one
    owner, and names nobody once owners are resolved per person. That is what makes it
    synthetic, and it is said here rather than left to be inferred from
    ``configured_owner_id()`` appearing below.

    Only the surface and the chat are decided per message. A skill injects on behalf of a
    delivery transport, so the surface is that transport's, and the bridge's own is what is
    left when a skill speaks for itself. The source reaches this as a skill argument, so it
    is coerced against the closed dictionary here rather than being trusted to have been
    checked upstream.
    """
    source = str(delivery_source or "").strip().lower()
    surface = SurfaceId.coerce(source) if source else SurfaceId(TRANSPORT_NAME)
    if surface is None:
        raise SkillBridgeError(f"unsupported delivery source {delivery_source!r}")
    return OwnerScope(
        owner_id=configured_owner_id(),
        agent_id=configured_agent_id(),
        surface_id=surface,
        chat_id=str(chat_id),
    )


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
    """Permission-checked bridge from extension skills onto the seam."""

    def __init__(
        self,
        data_dir: pathlib.Path,
        *,
        gateway: IMessageGateway,
        session_id: str,
        memory_events: IMemoryEventPublisher,
        permission_checker: ISkillPermissionChecker,
    ):
        self.data_dir = pathlib.Path(data_dir)
        self.gateway = gateway
        self._session_id = str(session_id or "")
        self._memory_events = memory_events
        self._permission_checker = permission_checker
        self.rate_limiter = _RateLimiter()
        self._inflight: dict[str, int] = defaultdict(int)
        self._inflight_lock = threading.Lock()
        self._counter_lock = threading.Lock()

    @property
    def skills_state_dir(self) -> pathlib.Path:
        return self.data_dir / "state" / "skills"

    @staticmethod
    def _parse_skill_id(value: str) -> SkillId:
        try:
            return SkillId.parse(value)
        except ValueError as exc:
            raise SkillBridgeError(f"skill id {value!r} is not canonical") from exc

    def require_permission(self, skill_id: SkillId, permission: str) -> None:
        refusal = self._permission_checker.refusal(skill_id, permission)
        if refusal is not None:
            raise SkillBridgeError(refusal)

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
        skill_id = self._parse_skill_id(skill_name)
        self.require_permission(skill_id, "inject_chat")
        runtime_key = skill_id.runtime_key
        counter_path = self.skills_state_dir / runtime_key / "chat_id_counter.json"
        with self._counter_lock:
            data = read_json_dict(service_identity(job=ServiceJob.SKILL_STATE).as_scope(), counter_path) or {}
            # Skills walk down from the top of their own window; A2A derives below
            # its floor, so neither allocator can reach the other's ids.
            next_id = int(data.get("next_chat_id") or SKILL_BRIDGE_CHAT_ID_MAX)
            chat_id = next_id
            if chat_id < SKILL_BRIDGE_CHAT_ID_MIN:
                chat_id = SKILL_BRIDGE_CHAT_ID_MAX
            atomic_write_json(
                service_identity(job=ServiceJob.SKILL_STATE).as_scope(),
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
        """Submit a message from an extension skill as if a user had sent it."""
        skill_id = self._parse_skill_id(skill_name)
        self.require_permission(skill_id, "inject_chat")
        clean_text = str(text or "").strip()
        caption_text = str(image_caption or "").strip()
        refusal = oversized_message_refusal(
            max((clean_text, caption_text), key=len), load_settings().OUROBOROS_MAX_INBOUND_MESSAGE_CHARS
        )
        if refusal:
            raise SkillBridgeError(refusal)
        if is_slash_command(clean_text) or is_slash_command(caption_text):
            raise SkillBridgeError("slash commands are reserved for direct owner input")
        # The synthetic scope is the owner's downstream, so an injected "да" self-authorizes.
        if confirmation_answer(clean_text) is not None or confirmation_answer(caption_text) is not None:
            raise SkillBridgeError("confirmation replies are reserved for direct owner input")
        if not self.rate_limiter.allow(f"{skill_name}:inject"):
            raise SkillBridgeError("rate limit exceeded")
        if not self._enter_inflight(skill_name):
            raise SkillBridgeError("too many in-flight inject requests")
        try:
            outbound_source = str(delivery_source or "").strip().lower()
            if outbound_source and outbound_source not in DELIVERY_SOURCES:
                raise SkillBridgeError(f"unsupported delivery source {outbound_source!r}")
            image = str(image_base64 or "").strip()
            if not clean_text and caption_text:
                clean_text = caption_text
            if not clean_text and not image:
                return
            self.gateway.inbox.submit(
                InboundEnvelope(
                    # The delivery source decides who answers the skill's message;
                    # without one the conversation belongs to the bridge itself.
                    scope=_synthetic_scope_for(outbound_source, int(chat_id)),
                    user_id=int(user_id),
                    text=clean_text,
                    sender_label=str(sender_label or skill_name),
                    # Minted here, once per injection: the seam redelivers an
                    # envelope a consumer took and never settled, and an envelope
                    # without this id takes a fresh task id on the second pass and
                    # runs the same injection twice.
                    client_message_id=f"skill:{uuid.uuid4().hex}",
                    transport=dict(transport or {}),
                    image_base64=image,
                    image_mime=str(image_mime or ""),
                    image_caption=caption_text,
                )
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
        """Publish extension output as an answer bound for the user."""
        skill_id = self._parse_skill_id(skill_name)
        self.require_permission(skill_id, "send_message")
        # Refused at the door rather than published: on an instance resolving owners per
        # person the synthetic owner below matches no connection, so the message would
        # reach nobody. The extension author sees an unsupported capability instead of a
        # message that vanishes.
        from transport.browser.identity import browser_identity

        if not browser_identity.resolver.delivers_unattributed_output:
            raise SkillBridgeError(
                "extension messages are unsupported while owners are resolved per person: "
                "the plugin API carries no author to attribute them to"
            )
        clean_text = str(text or "").strip()
        if not clean_text:
            raise SkillBridgeError("outbound message must be non-empty")
        if int(chat_id) <= 0:
            raise SkillBridgeError("outbound message requires a valid chat id")
        if not self.rate_limiter.allow(f"{skill_name}:send"):
            raise SkillBridgeError("rate limit exceeded")
        # Checked, never defaulted: the reading transport decides delivery from
        # the source, so guessing one sends the answer to the wrong channel or to
        # none at all — and the answer is written to history either way.
        delivery_source = str(source or "").strip().lower()
        if delivery_source not in DELIVERY_SOURCES:
            raise SkillBridgeError(f"unsupported delivery source {source!r}")
        # Minted once, here at the bridge's own entry point, and carried into the write:
        # the answer's surface is decided by the delivery source it was checked against.
        scope = _synthetic_scope_for(delivery_source, int(chat_id))
        self._record_answer(
            scope=scope, chat_id=int(chat_id), text=clean_text, source=delivery_source, task_id=str(task_id or "")
        )
        self.gateway.outbox.publish(
            OutboundMessage(
                scope=scope,
                source=delivery_source,
                text=clean_text,
                task_id=str(task_id or ""),
            )
        )

    def _record_answer(self, *, scope: OwnerScope, chat_id: int, text: str, source: str, task_id: str) -> None:
        """Put the skill's answer in the history the user reads.

        The supervisor persists what it produces; a skill's answer is produced
        here, so it is persisted here — before publishing, so a failed publish
        cannot leave the user with a message they never see in history.

        Written like any other agent turn: with the running session, which is
        what puts it in the conversation the user is having rather than one keyed
        by chat id, and followed by the persisted event background memory
        consolidates from.
        """
        try:
            persisted = append_chat_message(
                self.data_dir,
                # A skill's answer belongs to the transport that delivers it, the same
                # surface the inject path mints for a skill speaking as the user.
                scope=scope,
                actor_type="agent",
                chat_id=chat_id,
                user_id=chat_id,
                text=text,
                session_id=self._session_id,
                source=source,
                task_id=task_id,
            )
        except Exception:
            log.warning("Failed to persist a skill answer for chat %s", chat_id, exc_info=True)
            return
        if persisted is None:
            return
        announce_chat_message_persisted(self._memory_events, scope, str(persisted.message_id or ""))


__all__ = [
    "TRANSPORT_NAME",
    "SkillBridge",
    "SkillBridgeError",
]
