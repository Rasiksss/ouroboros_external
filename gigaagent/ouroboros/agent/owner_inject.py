"""Per-task owner-message mailboxes for running worker tasks."""

import json
import logging
import pathlib
import threading
import uuid

from new_core.identity import OwnerScope
from ouroboros.storage.blob.drive import get_drive_storage
from ouroboros.utils import read_text, utc_now_iso, write_text

log = logging.getLogger(__name__)

_MAILBOX_DIR = "memory/owner_mailbox"
_MAILBOX_LOCK = threading.Lock()


def _mailbox_path(drive_root: pathlib.Path, task_id: str) -> pathlib.Path:
    return drive_root / _MAILBOX_DIR / f"{task_id}.jsonl"


def write_owner_message(
    owner_scope: OwnerScope,
    drive_root: pathlib.Path,
    text: str,
    task_id: str,
    msg_id: str | None = None,
) -> None:
    """Write an owner message to a specific task's mailbox."""
    path = _mailbox_path(drive_root, task_id)
    entry = json.dumps(
        {
            "msg_id": msg_id or uuid.uuid4().hex,
            "ts": utc_now_iso(),
            "text": text,
        },
        ensure_ascii=False,
    )
    try:
        with _MAILBOX_LOCK:
            existing = ""
            if get_drive_storage(drive_root).path_exists(owner_scope, path):
                existing = read_text(owner_scope, path)
                if existing and not existing.endswith("\n"):
                    existing += "\n"
            write_text(owner_scope, path, existing + entry + "\n")
    except Exception:
        log.debug("Failed to write owner message for task %s", task_id, exc_info=True)
        raise


def drain_owner_messages(
    owner_scope: OwnerScope,
    drive_root: pathlib.Path,
    task_id: str,
    seen_ids: set | None = None,
) -> list[str]:
    """Read unseen message texts for one task, updating seen_ids for dedup."""
    path = _mailbox_path(drive_root, task_id)
    if not get_drive_storage(drive_root).path_exists(owner_scope, path):
        return []
    if seen_ids is None:
        seen_ids = set()
    try:
        content = read_text(owner_scope, path).strip()
        if not content:
            return []
        messages = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                mid = entry.get("msg_id", "")
                if mid and mid in seen_ids:
                    continue
                if mid:
                    seen_ids.add(mid)
                text = entry.get("text", "")
                if text:
                    messages.append(text)
            except Exception:
                log.debug("Malformed mailbox line for task %s", task_id, exc_info=True)
        return messages
    except Exception:
        log.debug("Failed to read mailbox for task %s", task_id, exc_info=True)
        return []


def cleanup_task_mailbox(owner_scope: OwnerScope, drive_root: pathlib.Path, task_id: str) -> None:
    """Remove a task's mailbox file after task completes."""
    path = _mailbox_path(drive_root, task_id)
    try:
        drive = get_drive_storage(drive_root)
        rel = drive.rel_from_path(path)
        if rel and drive.path_exists(owner_scope, path):
            drive.delete_rel(owner_scope, rel)
        elif path.is_file():
            path.unlink()
    except Exception:
        log.debug("Failed to cleanup mailbox for task %s", task_id, exc_info=True)
