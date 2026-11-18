"""Register Dialogs input in AIFA before it reaches the agent inbox."""

from __future__ import annotations

import json
import logging
import pathlib
import tempfile
from contextlib import ExitStack
from typing import Any
from urllib.parse import urlsplit

import httpx

from new_core.identity import OwnerScope
from ouroboros.observability.aef_tracing import with_agent_header, with_trace_header
from ouroboros.storage.attachment_validation import attachment_max_bytes

log = logging.getLogger(__name__)


def register_turn(settings: Any, scope: OwnerScope, source_message_id: str, message: str,
                  attachments: list, storage: Any) -> str:
    if "distribution:aifa-engine" not in settings.OUROBOROS_TRUSTED_EXECUTABLE_SKILL_IDS:
        return ""
    url = str(settings.AIFA_ENGINE_URL or "").strip()
    if urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).hostname:
        raise RuntimeError("AIFA_ENGINE_URL must be an HTTP service URL")
    token = settings.AIFA_ENGINE_TOKEN.get_secret_value()
    if not token or not source_message_id:
        raise RuntimeError("AIFA registration requires AIFA_ENGINE_TOKEN and source_message_id")
    # Non-report attachments still reach the agent, but are not AIFA documents.
    reports = [a for a in attachments if pathlib.PurePosixPath(a["filename"]).suffix.lower() in {".pdf", ".txt", ".md"}]
    registration = {"owner_id": scope.owner_id, "agent_id": scope.agent_id, "chat_id": scope.chat_id,
                        "source_message_id": source_message_id, "message": message,
                        "attachment_ids": [a["id"] for a in reports]}
    headers = with_agent_header(with_trace_header({"Authorization": f"Bearer {token}"}, generate=True))
    with tempfile.TemporaryDirectory(prefix="dialogs-aifa-") as directory, ExitStack() as stack:
        files = []
        for index, attachment in enumerate(reports):
            if any(str(attachment.get(k, "")) != str(v) for k, v in {
                "owner_id": scope.owner_id, "agent_id": scope.agent_id,
                "chat_id": scope.chat_id, "surface_id": "dialogs",
            }.items()):
                raise RuntimeError("AIFA attachment does not belong to the Dialogs scope")
            path = pathlib.Path(directory) / str(index)
            storage.copy_to_path(scope, "uploads", attachment["storage_key"], path,
                                 max_bytes=attachment_max_bytes(attachment["mime"]))
            files.append(("files", (attachment["filename"], stack.enter_context(path.open("rb")), attachment["mime"])))
        # A text-only follow-up also uses multipart, just without file parts.
        parts = [("registration", (None, json.dumps(registration), "application/json")), *files]
        log.info("event=aifa_dialogs_registration_start files=%d", len(files))
        try:
            with httpx.Client(timeout=60.0, trust_env=False) as client:
                response = client.post(f"{url.rstrip('/')}/internal/dialogs/v1/turns", files=parts, headers=headers)
                response.raise_for_status()
                run_id = response.json()["run_id"]
                if not isinstance(run_id, str) or not run_id:
                    raise ValueError("AIFA registration returned no run_id")
        except Exception as error:
            log.error("event=aifa_dialogs_registration_failed error_type=%s status=%s",
                      type(error).__name__, getattr(getattr(error, "response", None), "status_code", None))
            raise
    log.info("event=aifa_dialogs_registered run_id=%s files=%d", run_id, len(files))
    return run_id
