import base64
from dataclasses import replace

import pytest
from sqlalchemy import text

from ouroboros.storage.file_catalog import FileTransferPending
from ouroboros.storage.runtime_persistence import runtime_persistence
from supervisor.orchestrator.intent import SubmissionMode
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import TEST_SCOPE
from transport.interfaces import InboundEnvelope


@parametrize_from_dict(
    {
        "different_bytes_conflict": {"busy": False},
        "live_upload_retries": {"busy": True},
    }
)
def test_legacy_image_retry_distinguishes_conflict_from_live_upload(supervisor, busy):
    envelope = InboundEnvelope(
        scope=TEST_SCOPE,
        text="Inspect the image",
        client_message_id="image-retry",
        image_base64=base64.b64encode(b"\x89PNG\r\n\x1a\noriginal").decode(),
        image_mime="image/png",
    )
    orchestrator = supervisor.runtime.orchestrator
    admitted = orchestrator.submit(envelope)
    store = runtime_persistence.current
    record = store.tasks_repo.get_task(admitted.task_id)
    assert record.status == "queued"
    if busy:
        file_id = record.input["attachment_ids"][0]
        with store.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE files SET status='staging', claim_until='2099-01-01T00:00:00+00:00', "
                    "expires_at='2099-01-01T00:00:00+00:00' WHERE id=:id"
                ),
                {"id": file_id},
            )
        with pytest.raises(FileTransferPending):
            orchestrator.submit(envelope)
    else:
        refused = orchestrator.submit(
            replace(
                envelope,
                image_base64=base64.b64encode(b"\x89PNG\r\n\x1a\nchanged").decode(),
            )
        )
        assert refused.mode is SubmissionMode.NONE
        assert refused.reply_id.startswith("durable-job-conflict:")
    assert store.tasks_repo.get_task(admitted.task_id).status == "queued"
