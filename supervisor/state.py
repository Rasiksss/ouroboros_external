"""Supervisor persistent state and LLM usage accounting (owner state in runtime DB)."""

from __future__ import annotations

import contextlib
import logging
import pathlib
import time
import uuid
from collections.abc import Iterator
from typing import Any

from new_core.identity import OwnerScope, ServiceJob
from ouroboros.contracts.chat_commands import CONFIRMATION_ID_HINT_LEN
from ouroboros.observability.interfaces.audit_events import IAuditEventRepository
from ouroboros.storage.owner_state import default_state_dict, ensure_state_defaults
from ouroboros.storage.runtime_models import OwnerRuntimeState, RuntimeSession, TaskSource
from ouroboros.storage.tool_confirmation.interfaces import IToolConfirmationRepository
from supervisor.interfaces.cleanup_checkpoint import CLEANUP_CHECKPOINT_KEY
from supervisor.interfaces.owner_state import IOwnerStateRepository, OwnerStateDocument
from supervisor.interfaces.runtime_sessions import IRuntimeSessionRepository
from supervisor.interfaces.usage import IUsageRepository

__all__ = [
    "OwnerRuntimeState",
    "RuntimeSession",
    "RuntimeStateRepository",
    "TaskSource",
    "configured_owner_state_scope",
    "get_state_repository",
    "init",
    "init_state",
    "init_state_for_repo",
    "load_state",
    "model_breakdown",
    "per_task_usage_summary",
    "rotate_chat_log_if_needed",
    "save_state",
    "status_text",
    "update_usage_from_llm",
]
from ouroboros.storage.runtime_persistence import runtime_persistence

log = logging.getLogger(__name__)


class RuntimeStateRepository:
    """DB-backed owner runtime state with session sync into runtime metadata store."""

    def __init__(
        self,
        drive_root: pathlib.Path,
        *,
        owner_state_repository: IOwnerStateRepository,
        scope: OwnerScope,
        usage_repository: IUsageRepository,
        audit_event_repository: IAuditEventRepository,
    ) -> None:
        self.drive_root = pathlib.Path(drive_root)
        self._owner_state_repository = owner_state_repository
        #: Whose state this repository reads and writes. Not a tenant id: that named the
        #: deployment, so one person's session and spend were everybody's.
        self.scope = scope
        self.usage_repository = usage_repository
        self.audit_event_repository = audit_event_repository

    def for_scope(self, scope: OwnerScope) -> RuntimeStateRepository:
        """The same store, addressed to another owner."""
        return RuntimeStateRepository(
            self.drive_root,
            owner_state_repository=self._owner_state_repository,
            scope=scope,
            usage_repository=self.usage_repository,
            audit_event_repository=self.audit_event_repository,
        )

    def load(self) -> OwnerRuntimeState:
        document = self._owner_state_repository.load(self.scope)
        raw = document.payload if document is not None else None
        if raw is None:
            st = default_state_dict()
            self._owner_state_repository.save(OwnerStateDocument(st), self.scope)
            return OwnerRuntimeState.model_validate(st)
        normalized = ensure_state_defaults(dict(raw))
        if normalized != {key: value for key, value in raw.items() if key != CLEANUP_CHECKPOINT_KEY}:
            self._owner_state_repository.save(OwnerStateDocument(normalized), self.scope)
        state = OwnerRuntimeState.model_validate(normalized)
        self._refresh_usage_from_store(state)
        return state

    def session_id(self) -> str:
        """Read the session id without refreshing usage counters."""
        document = self._owner_state_repository.load(self.scope)
        raw = document.payload if document is not None else None
        stored = str((raw or {}).get("session_id") or "")
        return stored or str(self.load().session_id or "")

    def save(self, state: OwnerRuntimeState) -> None:
        payload = ensure_state_defaults(state.model_dump(mode="json", exclude_none=False))
        self._owner_state_repository.save(OwnerStateDocument(payload), self.scope)

    @contextlib.contextmanager
    def mutate(self) -> Iterator[OwnerRuntimeState]:
        """Read one owner's state, let the caller edit it, and write it back.

        The fields written here are last-writer-wins on the row, and say so rather than
        pretending otherwise: a context manager cannot be replayed, so it cannot sit inside
        the store's compare-and-set retry. The counters are not among them — those are a
        projection of ``llm_usage`` recomputed on every read, so two instances recording an
        owner's spend cannot lose each other's. No lock stands here either: one guarded
        writers inside one process and said nothing to a second, and a lock beside these
        writes is where the next read-modify-write would be added.
        """
        state = self.load()
        yield state
        self.save(state)

    def sync_runtime_session(
        self, session_repository: IRuntimeSessionRepository, *, source: TaskSource = "unknown"
    ) -> None:
        state = self.load()
        if not state.session_id:
            state.session_id = uuid.uuid4().hex
            self.save(state)
        owner_ref = str(state.owner_id or state.owner_chat_id or "")
        session_repository.create_or_update_session(
            RuntimeSession(
                session_id=state.session_id,
                source=source,
                owner_external_id=owner_ref,
                status="active",
            )
        )

    def _refresh_usage_from_store(self, state: OwnerRuntimeState) -> None:
        try:
            stats = self.usage_repository.sum_usage_stats(
                session_id=str(state.session_id or "") or None, scope=self.scope
            )
            state.spent_calls = int(stats.get("spent_calls") or 0)
            state.spent_tokens_prompt = int(stats.get("spent_tokens_prompt") or 0)
            state.spent_tokens_completion = int(stats.get("spent_tokens_completion") or 0)
            state.spent_tokens_cached = int(stats.get("spent_tokens_cached") or 0)
        except Exception:
            log.debug("Failed to refresh usage counters from llm_usage", exc_info=True)


def configured_owner_state_scope() -> OwnerScope:
    """The instance's configured owner, for a caller that holds no owner of its own.

    Spelled out as a widening rather than defaulted inside the repository: a reader with no
    owner in hand has to say which owner it means, and the row it reaches is one owner's,
    not the deployment's.
    """
    from ouroboros.contracts.owner_scope import service_identity

    return service_identity(job=ServiceJob.OWNER_STATE).as_scope()


def init(
    drive_root: pathlib.Path,
    *,
    owner_state_repository: IOwnerStateRepository,
    usage_repository: IUsageRepository,
    audit_event_repository: IAuditEventRepository,
) -> RuntimeStateRepository:
    """Create a state repository for tests; production uses build_supervisor_runtime."""
    return RuntimeStateRepository(
        pathlib.Path(drive_root),
        owner_state_repository=owner_state_repository,
        scope=configured_owner_state_scope(),
        usage_repository=usage_repository,
        audit_event_repository=audit_event_repository,
    )


def get_state_repository(
    drive_root: pathlib.Path | None = None,
    *,
    owner_state_repository: IOwnerStateRepository | None = None,
    usage_repository: IUsageRepository | None = None,
    audit_event_repository: IAuditEventRepository | None = None,
) -> RuntimeStateRepository:
    if drive_root is not None:
        if usage_repository is None or owner_state_repository is None or audit_event_repository is None:
            raise ValueError("a standalone state repository requires owner state, usage and audit repositories")
        return RuntimeStateRepository(
            pathlib.Path(drive_root),
            owner_state_repository=owner_state_repository,
            scope=configured_owner_state_scope(),
            usage_repository=usage_repository,
            audit_event_repository=audit_event_repository,
        )
    from supervisor.runtime_context import try_get_runtime

    runtime = try_get_runtime()
    if runtime is not None:
        return runtime.state_repo
    raise RuntimeError("RuntimeStateRepository is not available; pass drive_root or initialize supervisor runtime")


def load_state() -> OwnerRuntimeState:
    return get_state_repository().load()


def current_session_id() -> str:
    return get_state_repository().session_id()


def save_state(state: OwnerRuntimeState) -> None:
    get_state_repository().save(state)


def init_state_for_repo(repo: RuntimeStateRepository) -> OwnerRuntimeState:
    """Ensure owner state row exists with a session id."""
    state = repo.load()
    if not state.session_id:
        state.session_id = uuid.uuid4().hex
        repo.save(state)
    return state


def init_state() -> OwnerRuntimeState:
    return init_state_for_repo(get_state_repository())


def update_usage_from_llm(usage: dict[str, Any], *, owner_scope: OwnerScope | None) -> None:
    """Refresh LLM call/token counters from durable usage stats, for one owner.

    ``owner_scope`` is required and nullable rather than defaulted: a caller that holds no
    owner says so, and one that does cannot fill the configured owner's counters by
    omission. The aggregate is summed over that owner's rows for the same reason.
    """

    def _to_int(v: Any, default: int = 0) -> int:
        try:
            return int(v)
        except Exception:
            log.debug("Failed to convert value to int: %r", v, exc_info=True)
            return default

    repo = get_state_repository()
    if owner_scope is not None:
        repo = repo.for_scope(owner_scope)
    rounds = _to_int(usage.get("rounds") if isinstance(usage, dict) else 0, default=1)
    with repo.mutate() as state:
        try:
            stats = repo.usage_repository.sum_usage_stats(
                session_id=str(state.session_id or "") or None, scope=repo.scope
            )
            state.spent_calls = int(stats.get("spent_calls") or 0)
            state.spent_tokens_prompt = _to_int(stats.get("spent_tokens_prompt") or 0)
            state.spent_tokens_completion = _to_int(stats.get("spent_tokens_completion") or 0)
            state.spent_tokens_cached = _to_int(stats.get("spent_tokens_cached") or 0)
        except Exception:
            state.spent_calls = int(state.spent_calls or 0) + rounds
            state.spent_tokens_prompt = _to_int(state.spent_tokens_prompt or 0) + _to_int(
                usage.get("prompt_tokens") if isinstance(usage, dict) else 0
            )
            state.spent_tokens_completion = _to_int(state.spent_tokens_completion or 0) + _to_int(
                usage.get("completion_tokens") if isinstance(usage, dict) else 0
            )
            state.spent_tokens_cached = _to_int(state.spent_tokens_cached or 0) + _to_int(
                usage.get("cached_tokens") if isinstance(usage, dict) else 0
            )


#: How many ``llm_usage`` rows the model table reads. Bounded because this runs on the
#: supervisor's own tick thread, which also drains the inbox, enforces timeouts and reclaims
#: leases: a million-row scan there stalls every other owner's ingress for its duration.
_MODEL_BREAKDOWN_ROW_LIMIT = 50_000


def model_breakdown(*, owner_scope: OwnerScope | None = None) -> dict[str, dict[str, float]]:
    """Aggregate llm_usage calls/tokens by model from runtime events.

    ``owner_scope`` narrows the read to one owner's rows. It is optional because a
    deployment-wide total is a legitimate operator question; every caller that answers *an
    owner* passes it, because the alternative is telling one person what everybody else
    spent.
    """
    breakdown: dict[str, dict[str, float]] = {}
    try:
        from ouroboros.observability.event_record import list_runtime_events

        events = list_runtime_events(
            audit_event_repository=get_state_repository().audit_event_repository,
            event_type="llm_usage",
            limit=_MODEL_BREAKDOWN_ROW_LIMIT,
            scope=owner_scope,
        )
        for event in events:
            try:
                model = event.get("model") or "unknown"
                prompt_tokens = int(event.get("prompt_tokens", 0) or 0)
                completion_tokens = int(event.get("completion_tokens", 0) or 0)
                cached_tokens = int(event.get("cached_tokens", 0) or 0)
            except (ValueError, TypeError, AttributeError):
                continue

            if model not in breakdown:
                breakdown[model] = {
                    "calls": 0.0,
                    "prompt_tokens": 0.0,
                    "completion_tokens": 0.0,
                    "cached_tokens": 0.0,
                }

            breakdown[model]["calls"] += 1
            breakdown[model]["prompt_tokens"] += prompt_tokens
            breakdown[model]["completion_tokens"] += completion_tokens
            breakdown[model]["cached_tokens"] += cached_tokens
    except Exception:
        log.warning("Failed to calculate model breakdown", exc_info=True)

    return breakdown


def per_task_usage_summary(max_tasks: int = 10, *, owner_scope: OwnerScope) -> list[dict[str, Any]]:
    """Return recent per-task token usage from one owner's runtime events."""
    repo = get_state_repository()

    tasks: dict[str, dict[str, Any]] = {}
    try:
        from ouroboros.observability.event_record import list_runtime_events

        for event in list_runtime_events(
            audit_event_repository=repo.audit_event_repository,
            event_type="llm_usage",
            limit=5000,
            scope=owner_scope,
        ):
            try:
                tid = event.get("task_id") or "unknown"
                prompt_tokens = int(event.get("prompt_tokens", 0) or 0)
                completion_tokens = int(event.get("completion_tokens", 0) or 0)
            except (ValueError, TypeError, AttributeError):
                continue
            if tid not in tasks:
                tasks[tid] = {
                    "task_id": tid,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "rounds": 0,
                    "model": event.get("model", ""),
                }
            tasks[tid]["prompt_tokens"] += prompt_tokens
            tasks[tid]["completion_tokens"] += completion_tokens
            tasks[tid]["rounds"] += 1
    except Exception:
        log.warning("Failed to calculate per-task usage summary", exc_info=True)

    sorted_tasks = sorted(
        tasks.values(),
        key=lambda x: int(x.get("prompt_tokens", 0)) + int(x.get("completion_tokens", 0)),
        reverse=True,
    )
    return sorted_tasks[:max_tasks]


def status_text(
    soft_timeout_sec: int,
    hard_timeout_sec: int,
    *,
    tool_confirmation_reader: IToolConfirmationRepository,
    scope: OwnerScope,
) -> str:
    from supervisor.queue import parse_iso_to_ts

    records = runtime_persistence.current.tasks_repo.list_tasks(
        statuses=["queued", "scheduled", "interrupted", "running"], scope=scope, chat_only=True, limit=None
    )
    pending = [row for row in records if row.status != "running"]
    running = [row for row in records if row.status == "running"]
    state = get_state_repository().for_scope(scope).load()
    now = time.time()
    lines = []
    lines.append(f"owner_id: {state.owner_id}")
    lines.append(f"session_id: {state.session_id}")
    lines.append(f"pending: {len(pending)}")
    lines.append(f"running: {len(running)}")
    if pending:
        preview = [f"{row.task_id}:{row.task_type}:pr{row.priority}:a{row.attempt}" for row in pending[:10]]
        lines.append("pending_queue: " + ", ".join(preview))
    if running:
        lines.append("running_ids: " + ", ".join(row.task_id for row in running[:10]))
    open_request = tool_confirmation_reader.read_open_tool_confirmation(scope)
    if open_request is not None:
        hint = open_request.confirmation_id[:CONFIRMATION_ID_HINT_LEN]
        lines.append(f"awaiting_confirmation: {open_request.tool_name} [{hint}] task={open_request.task_id}")
    busy = [f"{row.worker_id}:{row.task_id}" for row in running]
    if busy:
        lines.append("busy: " + ", ".join(busy))
    if running:
        lines.append("running_details:")
        for row in running[:10]:
            started = parse_iso_to_ts(str(row.metadata.get("run_started_at") or row.started_at)) or 0.0
            hb = parse_iso_to_ts(row.heartbeat_at) or 0.0
            runtime_sec = int(max(0.0, now - started)) if started > 0 else 0
            hb_lag_sec = int(max(0.0, now - hb)) if hb > 0 else -1
            lines.append(
                f"  - {row.task_id}:type={row.task_type} pr={row.priority} "
                f"attempt={row.attempt} runtime={runtime_sec}s hb_lag={hb_lag_sec}s"
            )
    lines.append(f"agent_total_spent_calls: {state.spent_calls}")
    lines.append(
        f"agent_total_prompt_tokens: {state.spent_tokens_prompt}, completion_tokens: {state.spent_tokens_completion}, "
        f"cached_tokens: {state.spent_tokens_cached}"
    )

    # Narrowed like every other read here: the rows carry their owner now, and a
    # deployment-wide table would tell one owner which models everybody else is spending on.
    models = model_breakdown(owner_scope=scope)
    if models:
        sorted_models = sorted(models.items(), key=lambda x: x[1]["calls"], reverse=True)
        lines.append("agent_total_model_breakdown:")
        for model_name, stats in sorted_models:
            if stats["calls"] > 0:
                calls = int(stats["calls"])
                pt = int(stats["prompt_tokens"])
                ct = int(stats["completion_tokens"])
                lines.append(f"  {model_name}: {calls} calls, {pt:,}p/{ct:,}c tok")

    lines.append(f"agent_last_owner_message_at: {state.last_owner_message_at or '-'}")
    lines.append(f"timeouts: soft={soft_timeout_sec}s, hard={hard_timeout_sec}s")
    return "\n".join(lines)


def rotate_chat_log_if_needed(drive_root: pathlib.Path, max_bytes: int = 800_000) -> None:
    """Legacy no-op: owner chat lives in the runtime database."""
    del drive_root, max_bytes
