"""Schedule lifecycle the tools drive; every read and write is narrowed to the caller's scope.

The scope travels into the store, which decides ownership in the statement: `.rules/scheduling.md`.
"""

from __future__ import annotations

import copy
import time as time_module
import uuid
from collections.abc import Callable
from typing import Any

from new_core.identity import OwnerScope

from .core import (
    ScheduleDefinition,
    ScheduleValidationError,
    activate_schedule,
    definition_fingerprint,
    display_time,
    resume_schedule,
    schedule_summary,
    utc_iso,
    validate_definition,
)
from .store import ConcurrentUpdateError, ScheduleStore, StoredSchedule

_EDITABLE_STATES = {"pending", "needs_attention"}
_MOVABLE_STATES = {"pending", "active", "paused", "needs_attention"}
_LISTED_STATES = ("active", "pending", "paused", "needs_attention", "completed", "cancelled")
_TERMINAL_STATES = {"completed", "cancelled"}
#: One page of a listing that reaches the model's context, and the ceiling a caller may ask for.
_PAGE = 15
_PAGE_MAX = 25
_INTERNAL_SURFACES = {"a2a", "skill-bridge"}
_CONTEXT_MARKERS = (
    "этот проект",
    "эта задача",
    "как обсуж",
    "как договор",
    "как выше",
    "там же",
    "this project",
    "that project",
    "as discussed",
)


class SchedulerError(RuntimeError):
    """A schedule lifecycle operation cannot be completed."""


def _delivery_hints(ctx: Any) -> dict[str, Any]:
    """`target_uid` only: a reply anchor to a message from weeks ago is not a reply."""
    transport = getattr(ctx, "transport", None)
    if not isinstance(transport, dict):
        return {}
    uid = transport.get("target_uid")
    return {"target_uid": uid} if uid not in (None, "") else {}


def require_delivery_chat(scope: OwnerScope) -> None:
    """Raise unless the scope names a deliverable chat; string peer ids are valid (`routed_chat_id`)."""
    from ouroboros.contracts.chat_id_policy import is_internal_chat_id

    chat_id = str(scope.chat_id or "").strip()
    if not chat_id or is_internal_chat_id(chat_id):
        raise SchedulerError("schedule has no usable delivery chat")


def _owner_timezone(scope: OwnerScope) -> str:
    from config_registry.user_settings.reader import user_settings_factory

    return user_settings_factory.settings().OUROBOROS_TIMEZONE(scope)


class SchedulerService:
    def __init__(
        self,
        store: ScheduleStore,
        *,
        timezone_for: Callable[[OwnerScope], str] = _owner_timezone,
    ) -> None:
        self.store = store
        self.timezone_for = timezone_for

    @property
    def backend_name(self) -> str:
        return str(self.store.backend_name)

    @staticmethod
    def require_interactive(scope: OwnerScope) -> None:
        if scope.surface_id in _INTERNAL_SURFACES:
            raise SchedulerError("this operation requires an interactive user chat")
        require_delivery_chat(scope)

    @staticmethod
    def _scope(ctx: Any) -> OwnerScope:
        return ctx.scope

    def _timezone(self, ctx: Any) -> str:
        return str(self.timezone_for(self._scope(ctx)) or "").strip() or "Europe/Moscow"

    def _current_delivery(self, ctx: Any) -> dict[str, Any]:
        """The whole scope, serialized: a schedule outlives the process that created it."""
        scope = self._scope(ctx)
        self.require_interactive(scope)
        hints = _delivery_hints(ctx)
        # Part of the fingerprint on purpose: two participants of one group asking for the
        # same thing are two schedules, each addressed to its own asker.
        return {"scope": scope.to_dict(), "transport": hints} if hints else {"scope": scope.to_dict()}

    @staticmethod
    def _require_revision(stored: StoredSchedule, revision: int) -> None:
        if int(stored.value.get("revision") or 0) != int(revision or 0):
            raise SchedulerError(
                f"schedule revision changed; list schedules and retry with revision={stored.value.get('revision')}"
            )

    def _get(self, ctx: Any, schedule_id: str) -> StoredSchedule:
        stored = self.store.get_schedule(str(schedule_id or "").strip(), scope=self._scope(ctx))
        if stored is None:
            raise SchedulerError(f"schedule {schedule_id!r} not found")
        return stored

    def _replace(self, ctx: Any, stored: StoredSchedule, value: ScheduleDefinition) -> StoredSchedule:
        try:
            return self.store.replace_schedule(value, expected_token=stored.token, scope=self._scope(ctx))
        except ConcurrentUpdateError as exc:
            raise SchedulerError("schedule changed concurrently; list schedules and retry") from exc

    @staticmethod
    def _relative_at(
        *,
        now_ts: float,
        at: str | None,
        in_minutes: int | None,
        in_hours: int | None,
        in_days: int | None,
    ) -> str | None:
        values = (in_minutes, in_hours, in_days)
        if not any(value is not None for value in values):
            return None
        if sum(value is not None for value in values) > 1:
            raise ScheduleValidationError("provide only one relative delay field")
        if str(at or "").strip():
            raise ScheduleValidationError("provide either at or a relative delay, not both")
        total_minutes = 0
        for value, multiplier, field in (
            (in_minutes, 1, "in_minutes"),
            (in_hours, 60, "in_hours"),
            (in_days, 1440, "in_days"),
        ):
            if value is None:
                continue
            try:
                amount = int(value)
            except (TypeError, ValueError) as exc:
                raise ScheduleValidationError(f"{field} must be an integer") from exc
            if amount <= 0:
                raise ScheduleValidationError(f"{field} must be positive")
            total_minutes += amount * multiplier
        return utc_iso(now_ts + total_minutes * 60)

    def create_reminder(
        self,
        ctx: Any,
        *,
        message: str,
        recurrence: str = "",
        **schedule_fields: Any,
    ) -> dict[str, Any]:
        return self._create_schedule(
            ctx,
            execution_kind="reminder",
            what=message,
            recurrence=recurrence,
            **schedule_fields,
        )

    def create_scheduled_task(
        self,
        ctx: Any,
        *,
        instruction: str,
        recurrence: str = "",
        **schedule_fields: Any,
    ) -> dict[str, Any]:
        return self._create_schedule(
            ctx,
            execution_kind="agent_task",
            what=instruction,
            recurrence=recurrence,
            **schedule_fields,
        )

    def _create_schedule(
        self,
        ctx: Any,
        *,
        execution_kind: str,
        what: str,
        recurrence: str = "",
        time: str | None = None,
        weekday: str | None = None,
        interval_minutes: int | None = None,
        at: str | None = None,
        in_minutes: int | None = None,
        in_hours: int | None = None,
        in_days: int | None = None,
    ) -> dict[str, Any]:
        now_ts = time_module.time()
        delivery = self._current_delivery(ctx)
        relative_at = self._relative_at(
            now_ts=now_ts,
            at=at,
            in_minutes=in_minutes,
            in_hours=in_hours,
            in_days=in_days,
        )
        if relative_at is not None:
            recurrence = "once"
            at = relative_at
        elif str(at or "").strip() and not str(recurrence or "").strip():
            # Inferred only when the caller said nothing: "напомни завтра в 9" needs no
            # recurrence, but a stated one is the caller's, and overriding it here turned a
            # daily reminder into a one-off that also skipped the confirm gate.
            recurrence = "once"
        schedule = validate_definition(
            {
                "id": str(uuid.uuid4()),
                "revision": 1,
                "state": "pending",
                "execution_kind": execution_kind,
                "what": what,
                "recurrence": recurrence,
                "time": time,
                "weekday": weekday,
                "interval_minutes": interval_minutes,
                "at": at,
                "timezone": self._timezone(ctx),
                "created_at": utc_iso(now_ts),
                "updated_at": utc_iso(now_ts),
                "activated_at": None,
                "next_run_at": None,
                "delivery": delivery,
            },
            now_ts=now_ts,
            require_future_once=True,
        )
        if execution_kind == "reminder" and schedule["recurrence"] == "once":
            schedule = activate_schedule(schedule, now_ts=now_ts)
        schedule["fingerprint"] = definition_fingerprint(schedule)
        scope = self._scope(ctx)
        # Open rows sort first, so a limit of their count reads exactly them and nothing terminal.
        counts = self.store.count_schedules_by_state(scope=scope)
        # Open rows sort first; the margin covers rows a concurrent create added since the count.
        open_rows = sum(n for state, n in counts.items() if state not in _TERMINAL_STATES)
        for existing in self.store.list_schedules(scope=scope, limit=open_rows + _PAGE) if open_rows else ():
            value = existing.value
            if (
                value.get("state") in {"pending", "active", "paused"}
                and value.get("fingerprint") == schedule["fingerprint"]
            ):
                result = (
                    self._pending_response(value, now_ts=now_ts) if value["state"] == "pending" else self._public(value)
                )
                result.update({"status": "ok", "duplicate": True, "needs_confirmation": value["state"] == "pending"})
                return result
        stored = self.store.create_schedule(schedule, scope=scope)
        if stored.value["state"] == "pending":
            result = self._pending_response(stored.value, now_ts=now_ts)
        else:
            result = self._public(stored.value)
        result.update({"status": "ok", "needs_confirmation": stored.value["state"] == "pending"})
        return result

    def edit_schedule(
        self,
        ctx: Any,
        schedule_id: str,
        *,
        revision: int,
        what: str | None = None,
        recurrence: str | None = None,
        time: str | None = None,
        weekday: str | None = None,
        interval_minutes: int | None = None,
        at: str | None = None,
        in_minutes: int | None = None,
        in_hours: int | None = None,
        in_days: int | None = None,
    ) -> dict[str, Any]:
        self.require_interactive(self._scope(ctx))
        stored = self._get(ctx, schedule_id)
        self._require_revision(stored, revision)
        if stored.value.get("state") not in _EDITABLE_STATES:
            raise SchedulerError("only pending or needs-attention schedules can be edited")
        now_ts = time_module.time()
        value = copy.deepcopy(stored.value)
        relative_at = self._relative_at(
            now_ts=now_ts,
            at=at,
            in_minutes=in_minutes,
            in_hours=in_hours,
            in_days=in_days,
        )
        # Spelled out rather than looped: an edit only replaces the fields the caller named.
        if what is not None:
            value["what"] = what
        if recurrence is not None:
            value["recurrence"] = recurrence
        if time is not None:
            value["time"] = time
        if weekday is not None:
            value["weekday"] = weekday
        if interval_minutes is not None:
            value["interval_minutes"] = interval_minutes
        if at is not None:
            value["at"] = at
        if relative_at is not None:
            value["recurrence"] = "once"
            value["at"] = relative_at
        elif at is not None:
            value["recurrence"] = "once"
        value["state"] = "pending"
        value["activated_at"] = None
        value["next_run_at"] = None
        value["updated_at"] = utc_iso(now_ts)
        value = validate_definition(value, now_ts=now_ts, require_future_once=True)
        value["fingerprint"] = definition_fingerprint(value)
        updated = self._replace(ctx, stored, value)
        result = self._pending_response(updated.value, now_ts=now_ts)
        result.update({"status": "ok", "needs_confirmation": True})
        return result

    def confirm_schedule(self, ctx: Any, schedule_id: str, *, revision: int) -> dict[str, Any]:
        self.require_interactive(self._scope(ctx))
        stored = self._get(ctx, schedule_id)
        self._require_revision(stored, revision)
        if stored.value.get("state") not in _EDITABLE_STATES:
            raise SchedulerError("only pending or needs-attention schedules can be confirmed")
        active = activate_schedule(stored.value, now_ts=time_module.time())
        updated = self._replace(ctx, stored, active)
        result = self._public(updated.value)
        result["status"] = "ok"
        return result

    def list_schedules(
        self, scope: OwnerScope, *, state: str = "", cursor: int = 0, limit: int = _PAGE
    ) -> dict[str, Any]:
        """One page in one order; `next_cursor` keeps every row reachable, since a caller
        needs the revision from here to cancel, edit, pause or resume anything."""
        self.require_interactive(scope)
        wanted = str(state or "").strip()
        counts = self.store.count_schedules_by_state(scope=scope)
        total = counts.get(wanted, 0) if wanted else sum(counts.values())

        start = max(0, int(cursor or 0))
        size = max(1, min(_PAGE_MAX, int(limit or _PAGE)))
        page = [
            self._public(stored.value)
            for stored in self.store.list_schedules(scope=scope, state=wanted, offset=start, limit=size)
        ]

        answer: dict[str, Any] = {
            "status": "ok",
            "backend": self.backend_name,
            "counts": {name: counts.get(name, 0) for name in _LISTED_STATES},
            "total": total,
            "schedules": page,
        }
        # The cursor is an offset into the ordering, so it advances by the rows the store read,
        # not by the ones it could decode: a short page from a skipped row must not rewind it.
        if start + size < total:
            answer["next_cursor"] = start + size
        return answer

    def cancel_schedule(self, ctx: Any, schedule_id: str, *, revision: int) -> dict[str, Any]:
        self.require_interactive(self._scope(ctx))
        stored = self._get(ctx, schedule_id)
        self._require_revision(stored, revision)
        if stored.value.get("state") == "cancelled":
            return {"status": "ok", **self._public(stored.value), "note": "already cancelled"}
        value = stored.value.copy()
        value.update({"state": "cancelled", "next_run_at": None, "updated_at": utc_iso(time_module.time())})
        updated = self._replace(ctx, stored, value)
        return {"status": "ok", **self._public(updated.value)}

    def delete_schedule(self, scope: OwnerScope, schedule_id: str, *, revision: int) -> None:
        self.require_interactive(scope)
        self.store.delete_schedule(schedule_id, revision=revision, scope=scope)

    def pause_schedule(self, ctx: Any, schedule_id: str, *, revision: int) -> dict[str, Any]:
        self.require_interactive(self._scope(ctx))
        stored = self._get(ctx, schedule_id)
        self._require_revision(stored, revision)
        if stored.value.get("state") != "active":
            raise SchedulerError("only an active schedule can be paused")
        value = stored.value.copy()
        value.update({"state": "paused", "next_run_at": None, "updated_at": utc_iso(time_module.time())})
        updated = self._replace(ctx, stored, value)
        return {"status": "ok", **self._public(updated.value)}

    def resume_schedule(self, ctx: Any, schedule_id: str, *, revision: int) -> dict[str, Any]:
        self.require_interactive(self._scope(ctx))
        stored = self._get(ctx, schedule_id)
        self._require_revision(stored, revision)
        if stored.value.get("state") != "paused":
            raise SchedulerError("only a paused schedule can be resumed")
        value = resume_schedule(stored.value, now_ts=time_module.time())
        updated = self._replace(ctx, stored, value)
        return {"status": "ok", **self._public(updated.value)}

    def move_schedule_here(
        self,
        ctx: Any,
        *,
        schedule_id: str,
        revision: int,
    ) -> dict[str, Any]:
        delivery = self._current_delivery(ctx)
        stored = self._get(ctx, schedule_id)
        self._require_revision(stored, revision)
        if stored.value.get("state") not in _MOVABLE_STATES:
            raise SchedulerError("only a live or pending schedule can be moved")
        value = stored.value.copy()
        value.update({"delivery": delivery, "updated_at": utc_iso(time_module.time())})
        value["fingerprint"] = definition_fingerprint(value)
        updated = self._replace(ctx, stored, value)
        return {"status": "ok", **self._public(updated.value)}

    @staticmethod
    def _public(schedule: ScheduleDefinition) -> dict[str, Any]:
        """The answer, which is a different shape from the row: it gains a summary and a
        rendered time, and loses the fingerprint and the owner pair."""
        value: dict[str, Any] = copy.deepcopy(dict(schedule))
        value["execution_kind"] = str(schedule.get("execution_kind") or "agent_task")
        value.pop("fingerprint", None)
        # No ownership pair: this dict reaches the model's context and tool-result logs.
        delivery = schedule.get("delivery")
        scope = delivery.get("scope") if isinstance(delivery, dict) else None
        if isinstance(scope, dict):
            value["delivery"] = {"scope": {key: scope[key] for key in ("surface_id", "chat_id") if key in scope}}
        value["summary"] = schedule_summary(schedule)
        if any(marker in str(value.get("what") or "").lower() for marker in _CONTEXT_MARKERS):
            value["warning"] = (
                "what points at the current conversation; the run happens later, so say what it means outright"
            )
        if value.get("next_run_at"):
            value["next_run"] = display_time(value["next_run_at"], str(value.get("timezone") or "Europe/Moscow"))
        return value

    def _pending_response(self, schedule: ScheduleDefinition, *, now_ts: float) -> dict[str, Any]:
        result = self._public(schedule)
        preview = activate_schedule(schedule, now_ts=now_ts)
        result["preview_next_run"] = display_time(
            preview["next_run_at"],
            str(schedule.get("timezone") or "Europe/Moscow"),
        )
        return result


__all__ = ["SchedulerError", "SchedulerService"]
