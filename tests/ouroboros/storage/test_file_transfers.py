import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, nullcontext
from io import BytesIO
from threading import Barrier, Event

import pytest
from sqlalchemy import event, text

from infra.async_engine import create_async_runtime_engine
from ouroboros.storage.attachment_validation import AttachmentError
from ouroboros.storage.blob.factory import get_storage_provider
from ouroboros.storage.chat_attachments import store_attachment_fileobj
from ouroboros.storage.file_catalog import FileTransferPending
from ouroboros.storage.file_transfers import TransferLimits, get_transfer_limits
from ouroboros.storage.runtime_persistence import runtime_persistence
from ouroboros.tools.attachments import get_tools
from ouroboros.tools.service.messaging import _send_file
from tests.helpers.files import stored_file
from tests.helpers.outbound import RecordingOutbound
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE, make_tool_ctx, scope_with


@parametrize_from_dict(
    {
        "extract": {"operation": "extract", "valid": True},
        "invalid_extract": {"operation": "extract", "valid": False},
        "validate_upload": {"operation": "upload", "valid": True},
        "invalid_upload": {"operation": "upload", "valid": False},
        "send": {"operation": "send", "valid": True},
        "invalid_send": {"operation": "send", "valid": False},
    }
)
def test_document_parsing_leaves_transfer_capacity_available(tmp_path, monkeypatch, caplog, operation, valid):
    monkeypatch.setenv("BROWSER_INGRESS_ENABLED", "false")
    monkeypatch.setenv("OUROBOROS_FILE_OWNER_TRANSFERS", "1")
    data = b'{"ok": true}' if valid else b"{"
    metadata = stored_file(data=data, filename="report.json", mime="application/json")
    ctx = make_tool_ctx(
        repo_dir=tmp_path,
        drive_root=tmp_path,
        attachments=[metadata],
        task_workspace=tmp_path,
        outbound=RecordingOutbound(),
    )
    replica = TransferLimits(runtime_persistence.current.engine, 8, 1, process_id="other-replica", lease_seconds=300)
    entered, release = Event(), Event()

    class ParserAdmission(logging.Handler):
        def emit(self, record):
            message = record.getMessage()
            if message.startswith("event=attachment_parser_admission ") and "outcome=acquired" in message:
                entered.set()
                assert release.wait(10)

    def process():
        if operation == "send":
            return _send_file(ctx, metadata["id"])
        if operation == "extract":
            return get_tools()[0].handler(ctx, metadata["id"])
        with BytesIO(data) as source:
            return store_attachment_fileobj(
                TEST_SCOPE,
                get_storage_provider(),
                attachment_id="validated-upload",
                filename="report.json",
                mime="application/json",
                size=len(data),
                fileobj=source,
                source="browser",
                status="ready",
                validate_content=True,
            )

    logger = logging.getLogger("ouroboros.storage.attachment_validation")
    observer = ParserAdmission()
    logger.addHandler(observer)
    try:
        with caplog.at_level(logging.INFO, logger=logger.name), ThreadPoolExecutor(1) as workers:
            pending = workers.submit(process)
            try:
                assert entered.wait(10)
                with replica.slot(TEST_SCOPE):
                    pass
            finally:
                release.set()
            if operation == "upload" and not valid:
                with pytest.raises(AttachmentError) as error:
                    pending.result(timeout=10)
                assert error.value.code == "invalid_attachment"
            else:
                result = pending.result(timeout=10)
                if operation == "upload":
                    assert result["status"] == "ready"
                elif operation == "send":
                    assert ("OK:" if valid else "INVALID_ATTACHMENT") in result
                    assert len(ctx.outbound.files) == int(valid)
                else:
                    assert ("ok" if valid else "INVALID_ATTACHMENT") in result
    finally:
        release.set()
        logger.removeHandler(observer)
    with replica.slot(TEST_SCOPE):
        pass


def test_replicas_share_owner_admission():
    engine = runtime_persistence.initialize().engine
    replicas = [
        TransferLimits(engine, 8, 1, process_id=process, lease_seconds=300) for process in ("replica-a", "replica-b")
    ]
    start, finish = Barrier(2), Barrier(2)

    def transfer(limits):
        start.wait(timeout=10)
        try:
            with limits.slot(TEST_SCOPE):
                finish.wait(timeout=10)
                return True
        except FileTransferPending:
            finish.wait(timeout=10)
            return False

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert list(workers.map(transfer, replicas)).count(True) == 1
    with replicas[0].slot(TEST_SCOPE):
        with replicas[1].slot(scope_with(TEST_SCOPE, owner_id="another-owner")):
            pass


def test_multiuser_producer_preserves_shared_browser_outgoing_reservation(monkeypatch):
    monkeypatch.setenv("OUROBOROS_MULTIUSER", "true")
    monkeypatch.setenv("BROWSER_INGRESS_ENABLED", "false")
    engine = runtime_persistence.initialize().engine
    browser = TransferLimits(engine, 8, 2, process_id="browser-replica", lease_seconds=300, reserve_outgoing=True)
    with browser.slot(TEST_SCOPE), pytest.raises(FileTransferPending), get_transfer_limits().slot(TEST_SCOPE):
        pytest.fail("a producer consumed the shared owner's outgoing reservation")


def test_expired_holder_cannot_release_its_replacement():
    engine = runtime_persistence.initialize().engine
    first = TransferLimits(engine, 8, 1, process_id="replica-a", lease_seconds=300)
    second = TransferLimits(engine, 8, 1, process_id="replica-b", lease_seconds=300)
    with ExitStack() as abandoned:
        abandoned.enter_context(first.slot(TEST_SCOPE))
        with engine.begin() as conn:
            conn.execute(text("UPDATE file_transfer_leases SET expires_at=clock_timestamp()-interval '1 second'"))
        with second.slot(TEST_SCOPE):
            abandoned.close()
            with pytest.raises(FileTransferPending):
                with first.slot(TEST_SCOPE):
                    pytest.fail("the late release removed the replacement lease")
    with first.slot(TEST_SCOPE):
        pass


def test_outgoing_reservation_stays_inside_shared_owner_and_process_limits():
    engine = runtime_persistence.initialize().engine
    first = TransferLimits(engine, 2, 2, process_id="replica-a", lease_seconds=300, reserve_outgoing=True)
    second = TransferLimits(engine, 2, 2, process_id="replica-b", lease_seconds=300, reserve_outgoing=True)
    other_owner = scope_with(TEST_SCOPE, owner_id="another-owner")
    with first.slot(TEST_SCOPE):
        with pytest.raises(FileTransferPending):
            with second.slot(TEST_SCOPE):
                pytest.fail("ordinary traffic consumed the owner's outgoing reservation")
        with pytest.raises(FileTransferPending):
            with first.slot(other_owner):
                pytest.fail("ordinary traffic consumed the process's outgoing reservation")
        with first.slot(TEST_SCOPE, outgoing=True):
            with pytest.raises(FileTransferPending):
                with second.slot(TEST_SCOPE, outgoing=True):
                    pytest.fail("outgoing traffic exceeded the shared owner limit")
            with pytest.raises(FileTransferPending):
                with first.slot(other_owner, outgoing=True):
                    pytest.fail("outgoing traffic exceeded the process limit")


@parametrize_from_dict({"waiting": {"waiting": True}, "admitted": {"waiting": False}})
def test_cancelled_async_transfer_releases_its_owner_lease(waiting):
    store = runtime_persistence.initialize()
    first = TransferLimits(store.engine, 8, 1, process_id="replica-a", lease_seconds=300)
    second = TransferLimits(store.engine, 8, 1, process_id="replica-b", lease_seconds=300)

    async def scenario():
        engine = create_async_runtime_engine(store.db_urls)
        entered = asyncio.Event()

        def admission_checked(conn, cursor, statement, parameters, context, executemany):
            if waiting and "count(" in statement and "file_transfer_leases" in statement:
                entered.set()

        async def transfer():
            async with first.async_slot(TEST_SCOPE, engine, wait_seconds=5):
                entered.set()
                await asyncio.Event().wait()

        event.listen(engine.sync_engine, "after_cursor_execute", admission_checked)
        try:
            with second.slot(TEST_SCOPE) if waiting else nullcontext():
                task = asyncio.create_task(transfer())
                async with asyncio.timeout(5):
                    while not entered.is_set():
                        if task.done():
                            await task
                        await asyncio.sleep(0)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
            async with second.async_slot(TEST_SCOPE, engine):
                pass
        finally:
            event.remove(engine.sync_engine, "after_cursor_execute", admission_checked)
            await engine.dispose()

    asyncio.run(scenario())
