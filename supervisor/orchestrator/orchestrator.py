"""Task submission orchestrator: classify inbound messages and enqueue work."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from ouroboros.storage.blob.factory import create_storage_provider
from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
from ouroboros.storage.chat_attachments import delete_attachment_metadata, store_legacy_image_base64
from ouroboros.storage.chat_store import append_chat_message, get_chat_message_by_client_id, resolve_conversation_id
from ouroboros.storage.runtime_backend import RuntimeStore
from ouroboros.storage.runtime_models import TaskRequest, is_dialogs_source
from supervisor.orchestrator.envelope import InboundEnvelope
from supervisor.orchestrator.intent import SubmissionMode, TaskIntentKind
from supervisor.queue import TaskQueue
from supervisor.runtime_commands import runtime_slash_command
from supervisor.state import RuntimeStateRepository

log = logging.getLogger(__name__)


def _matches_active_participant(agent: Any, envelope: InboundEnvelope) -> bool:
    active_source = str(getattr(agent, "_current_task_source", "") or "")
    if envelope.source != active_source:
        return False
    if not is_dialogs_source(envelope.source):
        return True
    active_transport = getattr(agent, "_current_transport", {})
    active_uid = str(active_transport.get("target_uid") or "") if isinstance(active_transport, dict) else ""
    inbound_uid = str(envelope.transport.get("target_uid") or envelope.user_id or "")
    return bool(active_uid and inbound_uid and active_uid == inbound_uid)


def _inbound_task_id(envelope: InboundEnvelope) -> str:
    client_message_id = str(envelope.client_message_id or "").strip()
    if not client_message_id:
        return ""
    scope = json.dumps(
        [
            envelope.source,
            str(envelope.chat_id),
            str(envelope.user_id if envelope.user_id is not None else envelope.chat_id),
            str(envelope.sender_session_id or ""),
            client_message_id,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return uuid.uuid5(uuid.NAMESPACE_URL, scope).hex


@dataclass
class SubmitResult:
    intent: TaskIntentKind
    mode: SubmissionMode
    task_id: str = ""
    slash_command: str = ""
    message: str = ""


class SubmissionCommitUnknown(RuntimeError):
    def __init__(self, task_id: str) -> None:
        super().__init__(f"submission commit status is unknown for task {task_id}")
        self.task_id = task_id


class TaskOrchestrator:
    """Routes inbound chat to slash commands, inject, or durable task queue."""

    def __init__(
        self,
        *,
        store: RuntimeStore,
        task_queue: TaskQueue,
        state_repo: RuntimeStateRepository,
        agent_getter: Any,
    ) -> None:
        self.store = store
        self.task_queue = task_queue
        self.state_repo = state_repo
        self._agent_getter = agent_getter

    def classify(self, envelope: InboundEnvelope) -> TaskIntentKind:
        if runtime_slash_command(envelope.effective_text()):
            return TaskIntentKind.SLASH_COMMAND
        return TaskIntentKind.AGENT_CHAT

    def submit(self, envelope: InboundEnvelope) -> SubmitResult:
        intent = self.classify(envelope)
        if intent == TaskIntentKind.SLASH_COMMAND:
            cmd = envelope.effective_text().strip().lower().split()[0]
            return SubmitResult(
                intent=intent,
                mode=SubmissionMode.NONE,
                slash_command=cmd,
            )

        task_id = _inbound_task_id(envelope)
        if task_id and self.store.get_task(task_id) is not None:
            return SubmitResult(intent=intent, mode=SubmissionMode.QUEUE, task_id=task_id)

        if envelope.task_constraint:
            return self._queue_chat(envelope, task_id=task_id)
        if task_id:
            return self._queue_chat(envelope, task_id=task_id)

        agent = self._agent_getter()
        if agent is not None:
            current_chat_id = getattr(agent, "_current_chat_id", None)
            if (
                getattr(agent, "_busy", False)
                and not envelope.attachment_ids
                and not envelope.image_base64
                and _matches_active_participant(agent, envelope)
                and current_chat_id is not None
                and str(current_chat_id) == str(envelope.chat_id)
            ):
                inject_message = getattr(agent, "inject_message", None)
                if callable(inject_message):
                    inject_message(envelope.effective_text(), envelope.image_tuple())
                    return SubmitResult(intent=TaskIntentKind.AGENT_CHAT, mode=SubmissionMode.INJECT)

        return self._queue_chat(envelope, task_id=task_id)

    def _queue_chat(self, envelope: InboundEnvelope, *, task_id: str = "") -> SubmitResult:
        session = self.state_repo.load()
        conversation_id = resolve_conversation_id(
            conversation_id=envelope.conversation_id,
            session_id=str(session.session_id or ""),
            source=envelope.source,
            chat_id=envelope.chat_id,
        )
        attachment_ids = list(envelope.attachment_ids)
        attachments = [dict(value) for value in envelope.attachments]
        legacy_provider = None
        legacy_metadata: dict[str, Any] | None = None
        persisted_legacy_message_id = ""
        if envelope.image_base64:
            legacy_provider = create_storage_provider(data_dir=self.task_queue.drive_root)
            attachment_id = uuid.uuid4().hex
            message_id = envelope.client_message_id or uuid.uuid4().hex
            legacy_metadata = dict(
                store_legacy_image_base64(
                    legacy_provider,
                    attachment_id=attachment_id,
                    image_base64=envelope.image_base64,
                    claimed_mime=envelope.image_mime,
                    source=envelope.source,
                    message_id=message_id,
                )
            )
            try:
                persisted = append_chat_message(
                    self.task_queue.drive_root,
                    actor_type="owner",
                    chat_id=envelope.chat_id,
                    user_id=envelope.user_id if envelope.user_id is not None else envelope.chat_id,
                    text=envelope.effective_text(),
                    session_id=str(session.session_id or ""),
                    source=envelope.source,
                    sender_label=envelope.sender_label,
                    client_message_id=message_id,
                    conversation_id=conversation_id,
                    transport=envelope.transport,
                    extra_metadata={"attachments": [legacy_metadata]},
                )
                if persisted is None:
                    storage_key = str(legacy_metadata.get("storage_key") or "")
                    if storage_key:
                        legacy_provider.delete(SCOPE_UPLOADS, storage_key)
                    delete_attachment_metadata(legacy_provider, attachment_id)
                    existing = get_chat_message_by_client_id(
                        self.task_queue.drive_root,
                        actor_type="owner",
                        source=envelope.source,
                        chat_id=envelope.chat_id,
                        user_id=envelope.user_id if envelope.user_id is not None else envelope.chat_id,
                        client_message_id=message_id,
                        sender_session_id=envelope.sender_session_id,
                    )
                    raw_attachments = (existing.metadata if existing is not None else {}).get("attachments")
                    if not isinstance(raw_attachments, list) or not raw_attachments:
                        raise ValueError("duplicate client_message_id has no persisted image")
                    attachments = [dict(value) for value in raw_attachments if isinstance(value, dict)]
                    attachment_ids = [str(value.get("id")) for value in attachments if value.get("id")]
                    legacy_metadata = None
                else:
                    persisted_legacy_message_id = persisted.message_id
            except Exception:
                if legacy_metadata is not None:
                    storage_key = str(legacy_metadata.get("storage_key") or "")
                    if storage_key:
                        legacy_provider.delete(SCOPE_UPLOADS, storage_key)
                    delete_attachment_metadata(legacy_provider, attachment_id)
                raise
            if legacy_metadata is not None:
                attachment_ids.append(attachment_id)
                attachments.append(legacy_metadata)
        request = TaskRequest(
            id=task_id or uuid.uuid4().hex[:8],
            type="task",
            chat_id=envelope.chat_id,
            text=envelope.effective_text(),
            source=envelope.source,
            conversation_id=conversation_id,
            correlation_id=envelope.correlation_id,
            trace_id=envelope.trace_id,
            task_constraint=dict(envelope.task_constraint),
            image_base64="",
            image_mime="",
            image_caption="",
            attachment_ids=attachment_ids,
            attachments=attachments,
            transport=dict(envelope.transport),
        )
        try:
            record = self.task_queue.enqueue(request)
        except Exception:
            try:
                committed = self.store.get_task(request.id)
            except Exception as exc:
                raise SubmissionCommitUnknown(request.id) from exc
            if committed is not None:
                return SubmitResult(
                    intent=TaskIntentKind.AGENT_CHAT,
                    mode=SubmissionMode.QUEUE,
                    task_id=committed.task_id,
                )
            if (
                persisted_legacy_message_id
                and legacy_metadata is not None
                and legacy_provider is not None
                and self.store.delete_message(persisted_legacy_message_id)
            ):
                storage_key = str(legacy_metadata.get("storage_key") or "")
                if storage_key:
                    legacy_provider.delete(SCOPE_UPLOADS, storage_key)
                delete_attachment_metadata(legacy_provider, str(legacy_metadata.get("id") or ""))
            raise
        return SubmitResult(
            intent=TaskIntentKind.AGENT_CHAT,
            mode=SubmissionMode.QUEUE,
            task_id=record.task_id,
        )


__all__ = ["SubmissionCommitUnknown", "SubmitResult", "TaskOrchestrator"]
