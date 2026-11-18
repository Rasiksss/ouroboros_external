from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta

import anyio
from sqlalchemy import and_, delete, func, insert, or_, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from new_core.identity import OwnerScope
from ouroboros.storage.engine_setup import database_now
from ouroboros.storage.file_catalog import FileFailure, FileTransferPending, file_claim_seconds
from ouroboros.storage.file_models import FileTransferLease
from ouroboros.storage.settings.schema import AppSettings
from ouroboros.utils import finish_file_io

log = logging.getLogger(__name__)

_EXPIRED_LEASE_BATCH = 1000
_LEASE_SQL_TIMEOUT_SEC = 5
_ADMISSION_RETRY_INTERVAL_SEC = 0.25


class TransferLimits:
    def __init__(
        self,
        engine: Engine,
        capacity: int,
        per_owner: int,
        *,
        process_id: str,
        lease_seconds: float,
        reserve_outgoing: bool = False,
    ) -> None:
        self.engine, self.process_id, self.lease_seconds = engine, process_id, lease_seconds
        self.capacity, self.per_owner = capacity, per_owner
        self.reserve_outgoing = reserve_outgoing
        self._clients = threading.BoundedSemaphore(capacity)

    @contextmanager
    def client_slot(self) -> Iterator[None]:
        if not self._clients.acquire(blocking=False):
            raise FileTransferPending("process file request capacity exhausted", FileFailure.PROCESS_CAPACITY)
        try:
            yield
        finally:
            self._clients.release()

    def _acquire(self, conn: Connection, scope: OwnerScope, token: str, outgoing: bool) -> None:
        conn.execute(
            text("SELECT set_config('lock_timeout', :timeout, true), set_config('statement_timeout', :timeout, true)"),
            {"timeout": f"{_LEASE_SQL_TIMEOUT_SEC}s"},
        )
        for namespace, key in (("file-transfer-process", self.process_id), ("file-transfer-owner", scope.owner_id)):
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:namespace), hashtext(:key))"),
                {"namespace": namespace, "key": key},
            )
        now = database_now(conn)
        conn.execute(
            delete(FileTransferLease).where(
                FileTransferLease.token.in_(
                    select(FileTransferLease.token)
                    .where(FileTransferLease.expires_at <= now)
                    .limit(_EXPIRED_LEASE_BATCH)
                    .with_for_update(skip_locked=True)
                )
            )
        )
        owner = and_(FileTransferLease.owner_id == scope.owner_id, FileTransferLease.agent_id == scope.agent_id)
        process = FileTransferLease.process_id == self.process_id
        owner_count, owner_outgoing, process_count, process_outgoing = conn.execute(
            select(
                func.count().filter(owner),
                func.count().filter(owner, FileTransferLease.outgoing),
                func.count().filter(process),
                func.count().filter(process, FileTransferLease.outgoing),
            ).where(or_(owner, process), FileTransferLease.expires_at > now)
        ).one()
        owner_reserve = int(self.reserve_outgoing and not outgoing and not owner_outgoing)
        process_reserve = int(self.reserve_outgoing and not outgoing and not process_outgoing)
        if owner_count >= self.per_owner - owner_reserve:
            raise FileTransferPending("owner transfer capacity exhausted", FileFailure.OWNER_CAPACITY)
        if process_count >= self.capacity - process_reserve or (outgoing and process_outgoing):
            raise FileTransferPending("process transfer capacity exhausted", FileFailure.PROCESS_CAPACITY)
        conn.execute(
            insert(FileTransferLease).values(
                token=token,
                owner_id=scope.owner_id,
                agent_id=scope.agent_id,
                process_id=self.process_id,
                outgoing=outgoing,
                expires_at=now + timedelta(seconds=self.lease_seconds),
            )
        )

    @contextmanager
    def slot(self, scope: OwnerScope, *, outgoing: bool = False) -> Iterator[None]:
        token = uuid.uuid4().hex
        with self.engine.begin() as conn:
            self._acquire(conn, scope, token, outgoing)
        try:
            yield
        finally:
            try:
                with self.engine.begin() as conn:
                    conn.execute(delete(FileTransferLease).where(FileTransferLease.token == token))
            except SQLAlchemyError:
                log.warning("File-transfer release deferred to lease expiry", exc_info=True)

    @asynccontextmanager
    async def async_slot(
        self, scope: OwnerScope, engine: AsyncEngine, *, outgoing: bool = False, wait_seconds: float = 0
    ) -> AsyncIterator[None]:
        token = uuid.uuid4().hex
        deadline = asyncio.get_running_loop().time() + wait_seconds
        try:
            while True:
                try:
                    with anyio.CancelScope(shield=True):
                        await finish_file_io(self._acquire_async(engine, scope, token, outgoing))
                    break
                except FileTransferPending as error:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0 or error.kind not in {FileFailure.OWNER_CAPACITY, FileFailure.PROCESS_CAPACITY}:
                        raise
                    await asyncio.sleep(min(_ADMISSION_RETRY_INTERVAL_SEC, remaining))
            yield
        finally:
            with anyio.CancelScope(shield=True):
                await finish_file_io(self._release_async(engine, token))

    async def _acquire_async(self, engine: AsyncEngine, scope: OwnerScope, token: str, outgoing: bool) -> None:
        async with asyncio.timeout(_LEASE_SQL_TIMEOUT_SEC), engine.begin() as conn:
            await conn.run_sync(self._acquire, scope, token, outgoing)

    async def _release_async(self, engine: AsyncEngine, token: str) -> None:
        try:
            async with asyncio.timeout(_LEASE_SQL_TIMEOUT_SEC), engine.begin() as conn:
                await conn.execute(delete(FileTransferLease).where(FileTransferLease.token == token))
        except (SQLAlchemyError, TimeoutError):
            log.warning("File-transfer release deferred to lease expiry", exc_info=True)


_limits: TransferLimits | None = None


def initialize_transfer_limits(engine: Engine, settings: AppSettings, *, reserve_outgoing: bool = False) -> None:
    global _limits
    _limits = TransferLimits(
        engine,
        settings.OUROBOROS_FILE_TRANSFERS,
        settings.OUROBOROS_FILE_OWNER_TRANSFERS,
        process_id=f"{socket.gethostname()}:{os.getpid()}",
        lease_seconds=file_claim_seconds(settings.OUROBOROS_FILE_IO_TIMEOUT_SEC),
        reserve_outgoing=reserve_outgoing or settings.BROWSER_INGRESS_ENABLED or settings.OUROBOROS_MULTIUSER,
    )


def get_transfer_limits() -> TransferLimits:
    if _limits is None:
        raise RuntimeError("file transfer limits must be composed before use")
    return _limits
