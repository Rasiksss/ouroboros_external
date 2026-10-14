"""Task submission orchestrator: classify inbound messages and enqueue work."""

from __future__ import annotations

import json
import logging
import pathlib
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import TypeAdapter

from new_core.identity import OwnerScope
from ouroboros.contracts.chat_commands import (
    TYPED_ANSWER_SURFACES,
    ConfirmationAnswer,
    answered_slash_command,
    confirmation_answer,
    is_confirm_command,
    retired_slash_command,
    runtime_command_parts,
)
from ouroboros.contracts.tool_confirmation import CONFIRMATION_TRANSPORT_KEY
from ouroboros.observability import aef_tracing
from ouroboros.storage import runtime_backend
from ouroboros.storage.blob.factory import get_storage_provider
from ouroboros.storage.chat_attachments import store_legacy_image_base64
from ouroboros.storage.chat_store import append_chat_message, get_chat_message_by_client_id
from ouroboros.storage.conversations.interfaces import IConversationRepository
from ouroboros.storage.create_root_task.use_case import CreateRootTaskUseCase
from ouroboros.storage.file_catalog import FileError, FileFailure
from ouroboros.storage.runtime_backend import DurableJobsDisabledError
from ouroboros.storage.runtime_models import (
    ConversationMessage,
    TaskRecord,
    TaskRequest,
    TaskSource,
    normalize_task_source,
    same_task_request,
)
from ouroboros.storage.settings.store import load_settings
from ouroboros.storage.task_results import TERMINAL_STATUSES
from ouroboros.storage.tasks.interfaces import ITaskRepository
from ouroboros.storage.tool_confirmation.interfaces import IToolConfirmationRepository
from supervisor.orchestrator.intent import SubmissionMode, TaskIntentKind
from supervisor.queue import TaskQueue
from supervisor.state import RuntimeStateRepository
from transport.interfaces import InboundEnvelope

log = logging.getLogger(__name__)
_INBOUND_ADAPTER = TypeAdapter(InboundEnvelope)


def _asked_confirmation_id(message: ConversationMessage) -> str:
    request = (message.meta.get("transport") or {}).get(CONFIRMATION_TRANSPORT_KEY) or {}
    return str(request.get("confirmation_id") or "")


def _source(envelope: InboundEnvelope) -> TaskSource:
    """The scope's surface; the store's column wants its own closed set."""
    return normalize_task_source(envelope.scope.surface_id)


def _inbound_task_id(envelope: InboundEnvelope) -> str:
    """Stable admission identity across every scope and sender axis."""
    client_message_id = str(envelope.client_message_id or "").strip()
    if not client_message_id:
        return ""
    material = json.dumps(
        [
            envelope.scope.owner_id,
            envelope.scope.agent_id,
            envelope.scope.surface_id,
            envelope.scope.chat_id,
            str(envelope.user_id if envelope.user_id is not None else envelope.chat_id),
            str(envelope.sender_session_id or ""),
            client_message_id,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return uuid.uuid5(uuid.NAMESPACE_URL, material).hex


def stored_owner_message(drive_root: pathlib.Path, envelope: InboundEnvelope) -> ConversationMessage | None:
    return get_chat_message_by_client_id(
        drive_root,
        scope=envelope.scope,
        actor_type="owner",
        source=_source(envelope),
        chat_id=envelope.chat_id,
        user_id=envelope.user_id if envelope.user_id is not None else envelope.chat_id,
        client_message_id=envelope.client_message_id,
        sender_session_id=envelope.sender_session_id,
    )


def _snapshot_context(drive_root: pathlib.Path, envelope: InboundEnvelope, request: TaskRequest) -> None:
    message = stored_owner_message(drive_root, envelope)
    request.through_message_id = str(message.message_id if message else "")


def legacy_image_envelope(record: ConversationMessage) -> InboundEnvelope:
    attachments = record.attachments
    return _INBOUND_ADAPTER.validate_python(
        {
            **record.metadata,
            "scope": OwnerScope.from_dict(
                {
                    "owner_id": record.owner_id,
                    "agent_id": record.agent_id,
                    "surface_id": record.source,
                    "chat_id": record.chat_id,
                }
            ),
            "text": record.content,
            "client_message_id": record.source_message_id,
            "attachment_ids": [item["id"] for item in attachments],
        }
    )


@dataclass
class SubmitResult:
    intent: TaskIntentKind
    mode: SubmissionMode
    task_id: str = ""
    slash_command: str = ""
    answer: ConfirmationAnswer | None = None
    message: str = ""
    reply_id: str = ""
    trace_id: str = ""
    transport: dict[str, Any] = field(default_factory=dict)
    replay_result: bool = False


class SubmissionCommitUnknown(RuntimeError):
    def __init__(self, task_id: str) -> None:
        super().__init__(f"submission commit status is unknown for task {task_id}")
        self.task_id = task_id


def _conflicting_submission(envelope: InboundEnvelope, task_id: str) -> SubmitResult:
    reply_key = f"{task_id}:{envelope.correlation_id}" if envelope.source == "a2a" else task_id
    return SubmitResult(
        TaskIntentKind.AGENT_CHAT,
        SubmissionMode.NONE,
        task_id=task_id,
        message="Не удалось обработать сообщение: конфликт идентификатора.",  # noqa: RUF001
        reply_id=f"durable-job-conflict:{envelope.scope.owner_id}:{reply_key}",
        trace_id=envelope.trace_id,
        transport=dict(envelope.transport),
    )


class TaskOrchestrator:
    """Routes inbound chat to slash commands or the durable task queue.

    The queue is the only way in. Nothing here holds a reference to a run in flight, asks
    one whether it is busy, or adds a message to one that has already started — a message
    added to a run that is already ending is dropped without a record, while a queued one
    survives the process that was going to handle it. That is also what makes the decision
    the same on every instance: a reference cannot cross a process boundary.
    """

    def __init__(
        self,
        *,
        tasks_repo: ITaskRepository,
        create_root_task: CreateRootTaskUseCase,
        conversations_repo: IConversationRepository,
        tool_confirmation_reader: IToolConfirmationRepository,
        task_queue: TaskQueue,
        state_repo: RuntimeStateRepository,
    ) -> None:
        self.tasks_repo = tasks_repo
        self.create_root_task = create_root_task
        self.conversations_repo = conversations_repo
        self.tool_confirmation_reader = tool_confirmation_reader
        self.task_queue = task_queue
        self.state_repo = state_repo

    def classify(self, envelope: InboundEnvelope) -> tuple[TaskIntentKind, ConfirmationAnswer | None]:
        """The intent and, for a reply, its answer: ``None`` there is a `/confirm` that decides nothing."""
        if envelope.confirmation_decision:
            return TaskIntentKind.CONFIRMATION_REPLY, ConfirmationAnswer(
                envelope.confirmation_decision, envelope.confirmation_id
            )
        text = envelope.effective_text()
        # The reply path stores no attachment, and a photo captioned "ок 2024" parses as an answer.
        carries_a_file = bool(envelope.image_base64 or envelope.attachment_ids or envelope.attachments)
        typed = envelope.scope.surface_id in TYPED_ANSWER_SURFACES and not carries_a_file
        answer = confirmation_answer(text) if typed else None
        # Only the reply path can tell the owner the request they named is already gone.
        if answer is not None and answer.confirmation_id:
            return TaskIntentKind.CONFIRMATION_REPLY, answer
        if not carries_a_file and is_confirm_command(text):
            return TaskIntentKind.CONFIRMATION_REPLY, None
        if answered_slash_command(text):
            return TaskIntentKind.SLASH_COMMAND, None
        if answer is not None:
            answered = self._answered_confirmation_id(envelope)
            if answered:
                return TaskIntentKind.CONFIRMATION_REPLY, ConfirmationAnswer(answer.decision, answered)
        return TaskIntentKind.AGENT_CHAT, None

    def _answered_confirmation_id(self, envelope: InboundEnvelope) -> str:
        try:
            open_request = self.tool_confirmation_reader.read_open_tool_confirmation(envelope.scope)
            if open_request is None:
                return ""
            reply = stored_owner_message(self.task_queue.drive_root, envelope)
            if reply is None:
                return ""
            # Counted back from the reply's own row: a newer row says nothing about what this word answers.
            reply_and_before = self.conversations_repo.list_chat_history_messages(
                scope=envelope.scope, through_message_id=reply.message_id, limit=2
            )
        except runtime_backend.STORE_FAILURES:
            # An unreadable store is not a decision: the message goes to the agent.
            log.warning("Failed to check for an open confirmation request", exc_info=True)
            return ""
        if len(reply_and_before) < 2 or _asked_confirmation_id(reply_and_before[1]) != open_request.confirmation_id:
            return ""
        return open_request.confirmation_id

    def submit(self, envelope: InboundEnvelope) -> SubmitResult:
        task_id = _inbound_task_id(envelope)
        existing = self.tasks_repo.get_task(task_id) if task_id else None
        if existing is not None and existing.error_code == "inbound_failed":
            return self._submission_result(existing)
        intent, answer = self.classify(envelope)
        if intent == TaskIntentKind.CONFIRMATION_REPLY:
            return SubmitResult(intent=intent, mode=SubmissionMode.NONE, answer=answer)
        if intent == TaskIntentKind.SLASH_COMMAND:
            text = envelope.effective_text().strip()
            command, remainder = runtime_command_parts(text)
            owned = " ".join(filter(None, (command, remainder)))
            return SubmitResult(
                intent=intent,
                mode=SubmissionMode.NONE,
                # A retired command travels as its bare token: the refusal path logs what it got.
                slash_command=owned or retired_slash_command(text),
            )

        return self._queue_chat(envelope, task_id=task_id)

    def submit_detached(self, envelope: InboundEnvelope, text: str) -> SubmitResult:
        task_text = str(text or "").strip()
        if len(task_text) >= 2 and task_text[0] == task_text[-1] and task_text[0] in "\"'":
            task_text = task_text[1:-1].strip()
        if not task_text:
            return self.resolve_failed_inbound(envelope, rejection="После команды /new укажите задачу.")
        return self._queue_chat(replace(envelope, text=task_text, detached=True), task_id=_inbound_task_id(envelope))

    def resolve_failed_inbound(
        self,
        envelope: InboundEnvelope,
        *,
        rejection: str = "Не удалось обработать сообщение. Отправьте его ещё раз.",  # noqa: RUF001
    ) -> SubmitResult:
        task_id = _inbound_task_id(envelope)
        if not task_id:
            raise ValueError("inbound failure requires a stable message identity")
        record = self.create_root_task.execute(
            TaskRequest(
                id=task_id,
                job_id=task_id,
                scope=envelope.scope.to_dict(),
                source=_source(envelope),
                text=envelope.effective_text(),
                trace_id=envelope.trace_id,
                transport=dict(envelope.transport),
            ),
            enabled=False,
            max_active=0,
            rejection=rejection,
        )
        return self._submission_result(record, replay=record.error_code != "inbound_failed")

    def _queue_chat(self, envelope: InboundEnvelope, *, task_id: str = "") -> SubmitResult:
        source = _source(envelope)
        attachment_ids = list(envelope.attachment_ids)
        attachments = [value.copy() for value in envelope.attachments]
        if envelope.image_base64:
            session = self.state_repo.load()
            provider = get_storage_provider()
            attachment_id = uuid.uuid5(uuid.NAMESPACE_URL, "legacy-image:" + (task_id or uuid.uuid4().hex)).hex
            message_id = envelope.client_message_id or uuid.uuid4().hex
            persisted = stored_owner_message(self.task_queue.drive_root, envelope)
            if persisted is not None:
                if not persisted.metadata.get("attachments"):
                    return _conflicting_submission(envelope, task_id)
                saved = legacy_image_envelope(persisted)
                if (
                    saved.text != envelope.effective_text()
                    or saved.detached != envelope.detached
                    or len(saved.attachment_ids) != len(attachment_ids) + 1
                    or saved.attachment_ids[:-1] != attachment_ids
                ):
                    return _conflicting_submission(envelope, task_id)
                attachment_id = saved.attachment_ids[-1]
            try:
                metadata = store_legacy_image_base64(
                    envelope.scope,
                    provider,
                    attachment_id=attachment_id,
                    image_base64=envelope.image_base64,
                    claimed_mime=envelope.image_mime,
                    source=source,
                )
            except FileError as exc:
                if exc.kind is not FileFailure.CONFLICT:
                    raise
                return _conflicting_submission(envelope, task_id)
            attachment_ids.append(metadata["id"])
            persisted = (
                persisted
                or append_chat_message(
                    self.task_queue.drive_root,
                    scope=envelope.scope,
                    actor_type="owner",
                    chat_id=envelope.chat_id,
                    user_id=envelope.user_id if envelope.user_id is not None else envelope.chat_id,
                    text=envelope.effective_text(),
                    session_id=str(session.session_id or ""),
                    source=source,
                    sender_label=envelope.sender_label,
                    sender_session_id=envelope.sender_session_id,
                    client_message_id=message_id,
                    transport=envelope.transport,
                    extra_metadata={
                        "attachments": [{"id": identity} for identity in attachment_ids],
                        "inbound_processed": False,
                        "legacy_image": True,
                        "detached": envelope.detached,
                        "trace_id": envelope.trace_id,
                        "correlation_id": envelope.correlation_id,
                        "suppress_acceptance_notice": envelope.suppress_acceptance_notice,
                    },
                )
                or stored_owner_message(self.task_queue.drive_root, envelope)
            )
            if persisted is None:
                raise RuntimeError("legacy image message was not persisted")
            saved = legacy_image_envelope(persisted)
            if (
                saved.text != envelope.effective_text()
                or saved.attachment_ids != attachment_ids
                or saved.detached != envelope.detached
            ):
                return _conflicting_submission(envelope, task_id)
            attachments = saved.attachments
            envelope = saved
        request = TaskRequest(
            id=task_id or uuid.uuid4().hex[:8],
            job_id=task_id or "",
            detached=envelope.detached,
            type="task",
            chat_id=envelope.scope.chat_id,
            text=envelope.effective_text(),
            source=source,
            scope=envelope.scope.to_dict(),
            correlation_id=envelope.correlation_id,
            # Ensured here, on the one inbound path, rather than in each
            # transport: a transport that forwarded an upstream request carries
            # its trace, and one that did not still gets a trace of its own.
            trace_id=aef_tracing.ensure_trace_id(envelope.trace_id or aef_tracing.current_trace_id()),
            image_base64="",
            image_mime="",
            image_caption="",
            attachment_ids=attachment_ids,
            attachments=attachments,
            transport=dict(envelope.transport),
        )
        settings = load_settings()
        record = None
        if request.job_id:
            try:
                record = (
                    self.tasks_repo.get_current_job(request.job_id, scope=envelope.scope)
                    if source == "a2a" and not request.detached
                    else self.tasks_repo.get_task(request.id)
                )
            except runtime_backend.STORE_FAILURES as exc:
                raise SubmissionCommitUnknown(request.id) from exc
        if record is None:
            try:
                if settings.OUROBOROS_DURABLE_JOBS_ENABLED:
                    _snapshot_context(self.task_queue.drive_root, envelope, request)
                record = self.create_root_task.execute(
                    request,
                    enabled=settings.OUROBOROS_DURABLE_JOBS_ENABLED,
                    max_active=settings.OUROBOROS_MAX_ACTIVE_ROOT_JOBS_PER_OWNER,
                )
                if record.status == "queued":
                    self.task_queue.announce_claimable()
            except runtime_backend.MessageAnchorMissingError:
                return self.resolve_failed_inbound(
                    envelope,
                    rejection="Не удалось запустить фоновую задачу: исходное сообщение не сохранено.",  # noqa: RUF001
                )
            except DurableJobsDisabledError:
                return self.resolve_failed_inbound(envelope, rejection="Фоновые задачи отключены.")
            except Exception:
                try:
                    record = (
                        self.tasks_repo.get_current_job(request.job_id, scope=envelope.scope)
                        if source == "a2a" and not request.detached
                        else self.tasks_repo.get_task(request.id)
                    )
                except Exception as exc:
                    raise SubmissionCommitUnknown(request.id) from exc
                if record is None and (request.detached or settings.OUROBOROS_DURABLE_JOBS_ENABLED):
                    raise SubmissionCommitUnknown(request.id)
                if record is None:
                    raise
        if record.error_code != "inbound_failed" and not same_task_request(record, request):
            return _conflicting_submission(envelope, request.id)
        return self._submission_result(record, acknowledge=not envelope.suppress_acceptance_notice)

    def _submission_result(self, record: TaskRecord, *, acknowledge: bool = True, replay: bool = True) -> SubmitResult:
        failed = record.error_code == "inbound_failed"
        refused = record.status == "rejected_duplicate"
        replay = replay and record.source == "a2a" and not record.detached and record.status in TERMINAL_STATUSES
        return SubmitResult(
            intent=TaskIntentKind.AGENT_CHAT,
            mode=SubmissionMode.QUEUE,
            task_id=record.job_id or record.task_id,
            message=(
                record.result
                if failed
                else "Достигнут общий лимит активных задач. Дождитесь завершения или остановите одну из задач."
                if refused and not record.detached
                else "Не удалось запустить фоновую задачу: достигнут лимит активных задач."  # noqa: RUF001
                if refused
                else (record.result or "(no response)")
                if replay
                else "Фоновая задача принята. Результат появится здесь после завершения."
                if record.detached and acknowledge
                else ""
            ),
            reply_id=f"durable-job-{'terminal' if refused or failed else 'admission'}:{record.owner_id}:{record.job_id}",
            trace_id=str(record.input.get("trace_id") or ""),
            transport=dict(record.input.get("transport") or {}),
            replay_result=replay,
        )


__all__ = ["SubmissionCommitUnknown", "SubmitResult", "TaskOrchestrator"]
