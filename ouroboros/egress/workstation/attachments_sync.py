"""Copy this turn's stored attachments into the WorkStation inbound directory."""

from __future__ import annotations

import logging
import pathlib
from typing import TYPE_CHECKING

from new_core.identity import OwnerScope
from ouroboros.contracts.attachments import attachment_status
from ouroboros.egress.workstation.paths import INBOUND_SUBDIR
from ouroboros.storage.blob.factory import get_storage_provider
from ouroboros.storage.blob.protocol import StorageProvider
from ouroboros.storage.chat_attachments import get_attachment, read_attachment_bytes

if TYPE_CHECKING:
    from new_core.tools.context import ToolContext
    from ouroboros.egress.workstation.handle import WorkstationHandle

log = logging.getLogger(__name__)


def sync_turn_attachments(handle: WorkstationHandle, ctx: ToolContext) -> None:
    """Upload this turn's attachments; log individual failures and continue the turn."""
    if handle.config.access_level == "none":
        # No workspace is delivered to the session at all (spec §8.4) — there is
        # nowhere to put an inbound attachment either.
        return
    attachment_ids = [str(item["id"]) for item in (ctx.attachments or []) if isinstance(item, dict) and item.get("id")]
    if not attachment_ids:
        return

    provider = get_storage_provider()
    for attachment_id in attachment_ids:
        try:
            _sync_one(ctx.scope, handle, provider, attachment_id)
        except Exception:
            log.warning("WorkStation attachment sync failed for %s", attachment_id, exc_info=True)


def _sync_one(
    owner_scope: OwnerScope, handle: WorkstationHandle, provider: StorageProvider, attachment_id: str
) -> None:
    # The run's own scope: an attachment another owner stored answers as an absent one,
    # which is what keeps this sync from carrying a stranger's bytes into the session.
    metadata = get_attachment(owner_scope, attachment_id)
    if metadata is None or attachment_status(metadata) != "ready":
        log.warning("WorkStation attachment sync: %s is unavailable; skipped", attachment_id)
        return

    size = int(metadata.get("size") or 0)
    limit = handle.config.attachment_max_bytes
    if size > limit:
        log.warning(
            "WorkStation attachment sync: %s is %d bytes, over the %d-byte limit; skipped (fatal for this upload)",
            attachment_id,
            size,
            limit,
        )
        return

    mime = str(metadata.get("mime") or "application/octet-stream")
    data = read_attachment_bytes(owner_scope, provider, metadata["storage_key"], mime=mime, max_bytes=limit)
    if data is None:
        log.warning("WorkStation attachment sync: %s metadata exists but bytes are missing; skipped", attachment_id)
        return

    filename = pathlib.PurePosixPath(str(metadata.get("filename") or "")).name or attachment_id
    target = f"{INBOUND_SUBDIR}/{filename}"
    handle.files.write_file(target, data, mime=mime)


__all__ = ["sync_turn_attachments"]
