"""WorkStation session get-or-create algorithm (spec §5) and the process-wide registry.

Not a lifespan-initialized singleton like ``a2a_manager``/``mcp_manager``
(``ouroboros/egress/a2a/manager.py``): those exist to hold a long-lived
async connection the lifespan loop owns. This manager's client is
synchronous with no connection to open ahead of time — the registry starts
empty and the first ``get_or_create`` call builds the client lazily.
"""

from __future__ import annotations

import logging
import threading
import time

from ouroboros.egress.workstation.client import (
    SessionStatus,
    WorkstationApiError,
    WorkstationClient,
    WorkstationConfig,
)
from ouroboros.egress.workstation.handle import WorkstationHandle
from ouroboros.egress.workstation.seeding import reconcile_workspace, seed_workspace
from ouroboros.egress.workstation.session import SessionRecord, session_id_for

log = logging.getLogger(__name__)


class WorkstationManager:
    def __init__(self) -> None:
        self._records_lock = threading.Lock()
        self._records: dict[str, SessionRecord] = {}
        self._client_lock = threading.Lock()
        self._client: WorkstationClient | None = None
        self._client_config: WorkstationConfig | None = None
        #: One lock per scope key so concurrent turns for *different*
        #: conversations never block on each other's session setup; the
        #: dict itself grows with the number of distinct conversations ever
        #: seen this process, same lifetime as `_records` (spec §11.3's
        #: retention policy is deferred, so nothing evicts either yet).
        self._scope_locks_guard = threading.Lock()
        self._scope_locks: dict[str, threading.Lock] = {}

    def _client_for(self, config: WorkstationConfig) -> WorkstationClient:
        with self._client_lock:
            if self._client is None or self._client_config != config:
                stale = self._client
                self._client = WorkstationClient(config)
                self._client_config = config
                if stale is not None:
                    stale.close()
            return self._client

    def _lock_for_scope(self, scope_key: str) -> threading.Lock:
        with self._scope_locks_guard:
            lock = self._scope_locks.get(scope_key)
            if lock is None:
                lock = threading.Lock()
                self._scope_locks[scope_key] = lock
            return lock

    def _forget(self, scope_key: str) -> None:
        with self._records_lock:
            self._records.pop(scope_key, None)

    def get_or_create(
        self, *, scope_key: str, config: WorkstationConfig, timeout_sec: float | None = None
    ) -> WorkstationHandle:
        """Spec §5's algorithm, literally.

        Reuse the remembered {session_id, token} for ``scope_key`` when the
        service reports it ``ready``; forget and recreate on "not found" or
        any other non-ready status; abort without creating anything on any
        other error (a transient outage must not be masked by a spare new
        session). ``timeout_sec`` bounds only this session-setup call, not
        the exec/file calls made through the returned handle.
        """
        client = self._client_for(config)
        with self._lock_for_scope(scope_key):
            with self._records_lock:
                record = self._records.get(scope_key)

            if record is not None:
                try:
                    state = client.get_session(record.session_id, record.token, timeout_sec=timeout_sec)
                except WorkstationApiError as exc:
                    if not exc.is_not_found:
                        raise
                    self._forget(scope_key)
                else:
                    if state.status == SessionStatus.READY:
                        record.quotas = state.quotas
                        record.touch()
                        return WorkstationHandle(client=client, config=config, record=record)
                    self._forget(scope_key)

            session_id = session_id_for(config.session_strategy, scope_key)
            result = client.create_session(session_id, timeout_sec=timeout_sec)
            now = time.monotonic()
            new_record = SessionRecord(
                session_id=result.session_id,
                token=result.token,
                created_at=now,
                last_used_at=now,
                quotas=result.quotas,
            )
            with self._records_lock:
                self._records[scope_key] = new_record

            handle = WorkstationHandle(client=client, config=config, record=new_record)
            if result.created:
                if result.restored:
                    reconcile_workspace(handle)
                else:
                    seed_workspace(handle)
            return handle

    def reset_for_tests(self) -> None:
        with self._records_lock:
            self._records.clear()
        with self._client_lock:
            stale = self._client
            self._client = None
            self._client_config = None
        if stale is not None:
            stale.close()
        with self._scope_locks_guard:
            self._scope_locks.clear()


workstation_manager = WorkstationManager()

__all__ = ["WorkstationManager", "workstation_manager"]
