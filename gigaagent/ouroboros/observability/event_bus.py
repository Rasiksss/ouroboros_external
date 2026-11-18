"""Typed in-process event bus for reviewed extension subscriptions."""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

CHAT_OUTBOUND = "chat.outbound"
CHAT_TYPING = "chat.typing"
CHAT_PHOTO = "chat.photo"
SKILL_LIFECYCLE = "skill.lifecycle"
VALID_TOPICS = frozenset({CHAT_OUTBOUND, CHAT_TYPING, CHAT_PHOTO, SKILL_LIFECYCLE})


@dataclass
class EventSubscription:
    id: str
    skill_name: str
    topic: str
    handler: Callable[[dict[str, Any]], Any]


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscriptions: dict[str, EventSubscription] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        self._loop = loop

    def subscribe(self, skill_name: str, topic: str, handler: Callable[[dict[str, Any]], Any]) -> str:
        if topic not in VALID_TOPICS:
            raise ValueError(f"unsupported event topic: {topic}")
        sub_id = uuid.uuid4().hex
        with self._lock:
            self._subscriptions[sub_id] = EventSubscription(
                id=sub_id,
                skill_name=str(skill_name or ""),
                topic=topic,
                handler=handler,
            )
        return sub_id

    def unsubscribe(self, sub_id: str) -> None:
        with self._lock:
            self._subscriptions.pop(str(sub_id or ""), None)

    def unsubscribe_skill(self, skill_name: str) -> None:
        with self._lock:
            for sub_id, sub in list(self._subscriptions.items()):
                if sub.skill_name == skill_name:
                    self._subscriptions.pop(sub_id, None)

    def publish(self, topic: str, data: dict[str, Any]) -> None:
        if topic not in VALID_TOPICS:
            raise ValueError(f"unsupported event topic: {topic}")
        with self._lock:
            subscribers = [sub for sub in self._subscriptions.values() if sub.topic == topic]
        payload = dict(data or {})
        payload.setdefault("topic", topic)
        for sub in subscribers:
            try:
                result = sub.handler(payload)
                if inspect.iscoroutine(result):
                    if self._loop is not None and self._loop.is_running():
                        asyncio.run_coroutine_threadsafe(result, self._loop)
                    else:
                        log.debug("Dropping async event handler without running event loop for %s", topic)
                elif inspect.isawaitable(result):
                    log.debug("Dropping non-coroutine awaitable handler for %s", topic)
            except Exception:
                log.debug("Event subscriber %s failed for topic %s", sub.id, topic, exc_info=True)

    def snapshot(self) -> dict[str, dict[str, str]]:
        with self._lock:
            return {
                sub_id: {"skill_name": sub.skill_name, "topic": sub.topic}
                for sub_id, sub in self._subscriptions.items()
            }


_GLOBAL_BUS: EventBus | None = None


def init_global_event_bus() -> EventBus:
    global _GLOBAL_BUS
    _GLOBAL_BUS = EventBus()
    return _GLOBAL_BUS


def get_global_event_bus() -> EventBus:
    global _GLOBAL_BUS
    if _GLOBAL_BUS is None:
        _GLOBAL_BUS = EventBus()
    return _GLOBAL_BUS


def publish_event(topic: str, data: dict[str, Any]) -> None:
    get_global_event_bus().publish(topic, data)


__all__ = [
    "CHAT_OUTBOUND",
    "CHAT_PHOTO",
    "CHAT_TYPING",
    "SKILL_LIFECYCLE",
    "VALID_TOPICS",
    "EventBus",
    "get_global_event_bus",
    "init_global_event_bus",
    "publish_event",
]
