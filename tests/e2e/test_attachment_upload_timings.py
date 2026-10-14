from __future__ import annotations

import logging

from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.server_client import patched_server_client

_UPLOAD_OUTCOMES = {
    "valid JSON": {"valid": True},
    "invalid JSON": {"valid": False},
}


@parametrize_from_dict(_UPLOAD_OUTCOMES)
def test_upload_diagnostics_distinguish_validation_and_storage_without_document_data(
    tmp_path, monkeypatch, caplog, valid
):
    caplog.set_level(logging.INFO, logger="transport.browser.files")
    contents = b'{"private":"document contents"}' if valid else b'{"private": broken}'

    with patched_server_client(monkeypatch, tmp_path, browser_enabled=True, reset_storage=True) as client:
        logger = logging.getLogger("transport.browser.files")
        logger.addHandler(caplog.handler)
        try:
            response = client.post(
                "/api/chat/upload", files={"file": ("private-filename.json", contents, "application/json")}
            )
        finally:
            logger.removeHandler(caplog.handler)

    assert response.status_code == (200 if valid else 422)
    message = next(
        record.getMessage() for record in caplog.records if "event=attachment_upload_finished" in record.getMessage()
    )
    fields = dict(field.split("=", 1) for field in message.split())
    assert float(fields["copy_ms"]) >= 0
    assert float(fields["total_ms"]) >= float(fields["copy_ms"])
    if valid:
        assert fields["outcome"] == "ok"
        assert fields["phase"] == "storage"
        assert float(fields["validation_ms"]) >= 0
        assert float(fields["storage_ms"]) >= 0
    else:
        assert fields["outcome"] == "invalid_attachment"
        assert fields["phase"] == "validation"
        assert fields["validation_ms"] == fields["storage_ms"] == "None"
    assert "private" not in message
