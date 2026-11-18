"""Create a root task with an immutable context snapshot."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.engine import Connection

from infra.sql_transaction import SqlTransactionManager
from new_core.identity import OwnerScope
from ouroboros.storage.conversations.interfaces import IConversationRepository
from ouroboros.storage.core_memory.scratchpad.domain.render import render_blocks
from ouroboros.storage.core_memory.scratchpad.infra.sql import SqlScratchpad
from ouroboros.storage.execution_memory.interfaces import IExecutionMemoryRepository
from ouroboros.storage.runtime_backend import (
    IMMUTABLE_CONTEXT_SEED_MAX_CHARS,
    DurableJobsDisabledError,
    MessageAnchorMissingError,
    TaskIdentityConflictError,
)
from ouroboros.storage.runtime_models import TaskRecord, TaskRequest, TaskStatus, same_task_request
from ouroboros.storage.tasks.interfaces import ITaskRepository
from ouroboros.storage.tasks.records import _task_from_request


class CreateRootTaskUseCase:
    def __init__(
        self,
        *,
        tasks_repo: ITaskRepository,
        conversations_repo: IConversationRepository,
        memory_repo: IExecutionMemoryRepository,
        scratchpad_repo: SqlScratchpad,
        transaction_manager: SqlTransactionManager,
        episode_limit: Callable[[], int],
    ) -> None:
        self.tasks_repo = tasks_repo
        self.conversations_repo = conversations_repo
        self.memory_repo = memory_repo
        self.scratchpad_repo = scratchpad_repo
        self.transaction_manager = transaction_manager
        self._episode_limit = episode_limit

    def _capture_context(self, conn: Connection, request: TaskRequest, scope: OwnerScope) -> None:
        if not request.through_message_id:
            raise MessageAnchorMissingError("missing persisted submission")
        history = self.conversations_repo.list_chat_history_messages(
            scope=scope,
            through_message_id=request.through_message_id,
            limit=1000,
        )
        request.history_message_ids = [row.message_id for row in history]
        request.task_snapshot = [
            row.model_copy(
                update={
                    "input": {key: row.input[key] for key in ("text", "description") if key in row.input},
                    "metadata": {
                        key: row.metadata[key]
                        for key in ("text", "description", "total_rounds", "trace_summary")
                        if key in row.metadata
                    },
                }
            )
            for row in self.tasks_repo.list_tasks(scope=scope, limit=100, activity_first=True)
        ]
        request.committed_summary_snapshot = ""
        summary = self.conversations_repo.get_latest_context_summary(scope)
        if summary and summary.source_offsets_ref:
            try:
                valid = self.conversations_repo.list_chat_history_messages(
                    scope=scope,
                    after_message_id=summary.source_offsets_ref
                    if summary.source_offsets_ref != request.through_message_id
                    else None,
                    through_message_id=request.through_message_id,
                    limit=1,
                )
                if valid:
                    request.committed_summary_snapshot = summary.summary_ref[:IMMUTABLE_CONTEXT_SEED_MAX_CHARS]
            except MessageAnchorMissingError:
                pass  # A summary beyond the originating submission cannot be inherited.
        if request.detached:
            blocks = self.scratchpad_repo.list_active(scope, conn=conn)
            request.inherited_context_seed = "\n\n".join(
                filter(
                    None,
                    (
                        render_blocks(blocks),
                        request.context,
                    ),
                )
            )[-IMMUTABLE_CONTEXT_SEED_MAX_CHARS:]
            request.context = ""  # Explicit context is already inside the bounded immutable seed.
            request.recent_task_ids = sorted(
                {str(message.meta["task_id"]) for message in history if message.meta.get("task_id")}
            )
            request.episode_snapshot = {}
            episodes = self.memory_repo.list_task_episodes(scope=scope, limit=self._episode_limit())
            statuses = self.tasks_repo.get_job_or_task_statuses(
                (episode.task_id for episode in episodes if episode.task_id), scope=scope
            )
            for episode in episodes:
                if episode.task_id and statuses.get(episode.task_id) in (None, "completed"):
                    request.episode_snapshot[episode.episode_id] = episode.snapshot_version()
        request.context_captured = True

    def execute(self, request: TaskRequest, *, enabled: bool, max_active: int, rejection: str = "") -> TaskRecord:
        """Create a queued or rejected root task, or return its saved identity on replay."""
        if request.type != "task" or request.parent_task_id:
            raise ValueError("root admission accepts only parentless task records")
        scope = OwnerScope.from_dict(request.scope)
        with self.tasks_repo.admission_transaction(scope.owner_id), self.transaction_manager.connection() as conn:
            stored = self.tasks_repo.get_owner_job(request.job_id, owner_id=scope.owner_id)
            if stored is not None:
                if stored.agent_id != scope.agent_id or (
                    not rejection and stored.error_code != "inbound_failed" and not same_task_request(stored, request)
                ):
                    raise TaskIdentityConflictError(request.job_id)
                return stored
            if rejection:
                task = _task_from_request(request.model_copy(update={"detached": False}), status="failed")
                task.error_code, task.result = "inbound_failed", rejection
                self.tasks_repo.insert_task(task)
                return task
            if request.detached and not enabled:
                raise DurableJobsDisabledError(request.job_id)
            status: TaskStatus = "queued"
            if request.detached:
                active = self.tasks_repo.count_active_root_jobs(owner_id=scope.owner_id)
                if int(active or 0) >= int(max_active):
                    status = "rejected_duplicate"
            if request.detached or request.through_message_id:
                self._capture_context(conn, request, scope)
            task = _task_from_request(request, status=status)
            if status == "rejected_duplicate":
                task.error_code = "owner_active_job_limit"
                task.result = "Active job limit reached."
            self.tasks_repo.insert_task(task)
        return task
