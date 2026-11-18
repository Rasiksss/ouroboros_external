"""In-process per-session locking primitives."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.utils.exceptions.common import InvalidStateException


@dataclass(slots=True)
class _OperationState:
    active: int = 0
    exclusive: bool = False


class SessionLockRegistry:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def is_busy(self, session_id: str) -> bool:
        lock = self._locks.get(session_id)
        return bool(lock and lock.locked())

    @contextlib.asynccontextmanager
    async def acquire(self, session_id: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            raise InvalidStateException("Сессия занята другой операцией", details={"sessionId": session_id})
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()
            self._cleanup_lock(session_id, lock)

    @contextlib.asynccontextmanager
    async def serialize(self, session_id: str) -> AsyncIterator[None]:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()
            self._cleanup_lock(session_id, lock)

    def _cleanup_lock(self, session_id: str, lock: asyncio.Lock) -> None:
        waiters = getattr(lock, "_waiters", None)
        if not lock.locked() and not waiters:
            self._locks.pop(session_id, None)


class SessionOperationRegistry:
    def __init__(self) -> None:
        self._states: dict[str, _OperationState] = {}
        self._guard = asyncio.Lock()

    def is_busy(self, session_id: str) -> bool:
        state = self._states.get(session_id)
        return bool(state and (state.active > 0 or state.exclusive))

    @contextlib.asynccontextmanager
    async def track(self, session_id: str) -> AsyncIterator[None]:
        async with self._guard:
            state = self._states.setdefault(session_id, _OperationState())
            if state.exclusive:
                raise InvalidStateException("Сессия занята другой операцией", details={"sessionId": session_id})
            state.active += 1
        try:
            yield
        finally:
            async with self._guard:
                # No `return` in `finally`: it would swallow whatever the caller raised
                # inside the context manager. A missing state just means nothing to clean.
                state = self._states.get(session_id)
                if state is not None:
                    state.active -= 1
                    self._cleanup_state(session_id, state)

    @contextlib.asynccontextmanager
    async def exclusive(self, session_id: str) -> AsyncIterator[None]:
        async with self._guard:
            state = self._states.setdefault(session_id, _OperationState())
            if state.active > 0 or state.exclusive:
                raise InvalidStateException("Сессия занята другой операцией", details={"sessionId": session_id})
            state.exclusive = True
        try:
            yield
        finally:
            async with self._guard:
                # Same as `track`: returning from `finally` would silence the caller's
                # exception, so the missing-state case only skips cleanup.
                state = self._states.get(session_id)
                if state is not None:
                    state.exclusive = False
                    self._cleanup_state(session_id, state)

    def _cleanup_state(self, session_id: str, state: _OperationState) -> None:
        if state.active == 0 and not state.exclusive:
            self._states.pop(session_id, None)


session_exec_operations = SessionOperationRegistry()
session_create_locks = SessionLockRegistry()
session_file_locks = SessionLockRegistry()
