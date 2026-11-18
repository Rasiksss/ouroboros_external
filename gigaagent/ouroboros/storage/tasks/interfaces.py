from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from typing import Any

from new_core.identity import OwnerScope
from ouroboros.storage.runtime_models import (
    TaskRecord,
    TaskRequest,
    TaskStatus,
    TaskStopState,
)


class ITaskRepository(ABC):
    """Task execution lifecycle and cancellation."""

    @abstractmethod
    def read_task_stop_state(self, task_id: str) -> TaskStopState: ...

    @property
    @abstractmethod
    def transaction(self) -> AbstractContextManager[None]: ...

    @abstractmethod
    def create_task(self, request: TaskRequest, *, status: TaskStatus = "queued") -> TaskRecord: ...

    @abstractmethod
    def upsert_task(
        self,
        task: TaskRecord,
        *,
        execution_task_id: str | None = None,
        claim_token: str = "",
        expected_status: TaskStatus | None = None,
    ) -> TaskRecord: ...

    @abstractmethod
    def claim_next_task(
        self,
        worker_id: int = 0,
        *,
        runtime_id: str,
        include_task_types: Iterable[str] | None = None,
        exclude_task_types: Iterable[str] | None = None,
        max_running_root_jobs_per_owner: int = 3,
        claim_guard: Callable[[TaskRecord], AbstractContextManager[bool]] | None = None,
    ) -> TaskRecord | None: ...

    @abstractmethod
    def request_task_stop(self, task_id: str, *, scope: OwnerScope) -> bool: ...

    @abstractmethod
    def claim_soft_timeout_notice(self, task_id: str) -> bool: ...

    @abstractmethod
    def heartbeat_task(self, task_id: str, *, phase: str = "", claim_token: str = "") -> TaskRecord | None: ...

    @abstractmethod
    def complete_task(
        self, task_id: str, *, result: str = "", result_ref: str = "", **fields: Any
    ) -> TaskRecord | None: ...

    @abstractmethod
    def fail_task(
        self,
        task_id: str,
        *,
        result: str = "",
        error_code: str = "",
        status: TaskStatus = "failed",
        **fields: Any,
    ) -> TaskRecord | None: ...

    @abstractmethod
    def cancel_task(self, task_id: str, *, result: str = "Task cancelled.") -> TaskRecord | None: ...

    @abstractmethod
    def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        result: str | None = None,
        result_ref: str | None = None,
        error_code: str | None = None,
        metadata: dict[str, Any] | None = None,
        **fields: Any,
    ) -> TaskRecord | None: ...

    @abstractmethod
    def list_stale_leases(self, *, cutoff_iso: str, limit: int = 100) -> list[TaskRecord]: ...

    @abstractmethod
    def transition_running(
        self,
        task_id: str,
        *,
        kind: str,
        result: str,
        attempt: int,
        successor: TaskRequest | None = None,
        runtime_id: str = "",
        claim_token: str = "",
        cutoff_iso: str = "",
        hard_timeout_sec: float = 1800,
    ) -> tuple[TaskRecord, bool] | None: ...

    @abstractmethod
    def get_task(self, task_id: str) -> TaskRecord | None: ...

    @abstractmethod
    def is_task_attempt_active(self, task_id: str, *, claim_token: str = "") -> bool: ...

    @abstractmethod
    def finish_detached_attempt(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        result: str = "",
        error_code: str = "",
        claim_token: str = "",
        pending_child_ids: tuple[str, ...] = (),
        result_ref: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> TaskRecord | None: ...

    @abstractmethod
    def get_current_job(self, job_id: str, *, scope: OwnerScope) -> TaskRecord | None: ...

    @abstractmethod
    def get_job_or_task_statuses(self, ids: Iterable[str], *, scope: OwnerScope) -> dict[str, TaskStatus]: ...

    @abstractmethod
    def list_current_jobs(self, *, scope: OwnerScope, limit: int, active_only: bool = False) -> list[TaskRecord]: ...

    @abstractmethod
    def stop_jobs(self, *, scope: OwnerScope, job_id: str | None = None) -> list[TaskRecord]: ...

    @abstractmethod
    def list_tasks(
        self,
        *,
        statuses: Iterable[str] | None = None,
        limit: int | None = 500,
        activity_first: bool = False,
        scope: OwnerScope | None = None,
        runtime_id: str | None = None,
        chat_only: bool = False,
    ) -> list[TaskRecord]: ...

    @abstractmethod
    def admission_transaction(self, owner_id: str) -> AbstractContextManager[None]: ...

    @abstractmethod
    def get_owner_job(self, job_id: str, *, owner_id: str) -> TaskRecord | None: ...

    @abstractmethod
    def count_active_root_jobs(self, *, owner_id: str) -> int: ...

    @abstractmethod
    def insert_task(self, task: TaskRecord) -> None: ...

    @abstractmethod
    def list_terminal_memory(self, *, limit: int = 50) -> list[TaskRecord]: ...

    @abstractmethod
    def finish_terminal_memory(self, task_id: str, *, filed: bool) -> bool: ...
