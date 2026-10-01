"""Schedule lifecycle and dispatch orchestration."""

from __future__ import annotations

import copy
import time as time_module
import uuid
from collections.abc import Callable
from typing import Any

from .scheduler_core import (
    ScheduleValidationError,
    activate_schedule,
    definition_fingerprint,
    display_time,
    next_occurrence,
    parse_iso_timestamp,
    resume_schedule,
    schedule_summary,
    utc_iso,
    validate_definition,
)
from .scheduler_storage import ConcurrentUpdateError, ScheduleStore, StorageError, StoredSchedule

_EDITABLE_STATES = {"pending", "needs_attention"}
_MOVABLE_STATES = {"pending", "active", "paused", "needs_attention"}
_INTERNAL_SOURCES = {"a2a", "internal", "system"}
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


class SchedulerService:
    def __init__(
        self,
        store: ScheduleStore,
        api: Any,
        *,
        timezone_name: str,
        now_fn: Callable[[], float] = time_module.time,
    ) -> None:
        self.store = store
        self.api = api
        self.timezone_name = str(timezone_name or "").strip() or "Europe/Moscow"
        self.now_fn = now_fn

    @property
    def backend_name(self) -> str:
        return str(getattr(self.store, "backend_name", "unknown"))

    def require_interactive(self, ctx: Any) -> None:
        self._current_delivery(ctx)

    def _current_delivery(self, ctx: Any) -> dict[str, Any]:
        source = str(getattr(ctx, "current_task_source", "") or "").strip().lower()
        if source in _INTERNAL_SOURCES or source.startswith("skill:"):
            raise SchedulerError("this operation requires an interactive user chat")
        raw_chat_id = getattr(ctx, "current_chat_id", None)
        try:
            chat_id = int(raw_chat_id)
        except (TypeError, ValueError) as exc:
            raise SchedulerError("current chat has no numeric delivery id") from exc
        if chat_id <= 0:
            raise SchedulerError("this operation requires an interactive user chat")
        return {"chat_id": chat_id, "source": source or "unknown"}

    @staticmethod
    def _require_revision(stored: StoredSchedule, revision: int) -> None:
        if int(stored.value.get("revision") or 0) != int(revision or 0):
            raise SchedulerError(
                f"schedule revision changed; list schedules and retry with revision={stored.value.get('revision')}"
            )

    def _get(self, schedule_id: str) -> StoredSchedule:
        stored = self.store.get_schedule(str(schedule_id or "").strip())
        if stored is None:
            raise SchedulerError(f"schedule {schedule_id!r} not found")
        return stored

    def _replace(self, stored: StoredSchedule, value: dict[str, Any]) -> StoredSchedule:
        try:
            return self.store.replace_schedule(value, expected_token=stored.token)
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
        recurrence: str,
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
        recurrence: str,
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
        recurrence: str,
        time: str | None = None,
        weekday: str | None = None,
        interval_minutes: int | None = None,
        at: str | None = None,
        in_minutes: int | None = None,
        in_hours: int | None = None,
        in_days: int | None = None,
    ) -> dict[str, Any]:
        now_ts = self.now_fn()
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
                "timezone": self.timezone_name,
                "created_at": utc_iso(now_ts),
                "updated_at": utc_iso(now_ts),
                "activated_at": None,
                "next_run_at": None,
                "delivery": delivery,
            },
            now_ts=now_ts,
            require_future_once=True,
        )
        schedule.update(
            {
                "id": schedule.get("id") or str(uuid.uuid4()),
                "revision": 1,
                "state": "pending",
                "created_at": utc_iso(now_ts),
                "updated_at": utc_iso(now_ts),
                "activated_at": None,
                "next_run_at": None,
                "delivery": delivery,
            }
        )
        if execution_kind == "reminder" and schedule["recurrence"] == "once":
            schedule = activate_schedule(schedule, now_ts=now_ts)
        schedule["fingerprint"] = definition_fingerprint(schedule)
        for existing in self.store.list_schedules():
            value = existing.value
            if (
                value.get("state") in {"pending", "active", "paused"}
                and definition_fingerprint(value) == schedule["fingerprint"]
            ):
                result = (
                    self._pending_response(value, now_ts=now_ts) if value["state"] == "pending" else self._public(value)
                )
                result.update({"status": "ok", "duplicate": True, "needs_confirmation": value["state"] == "pending"})
                return result
        stored = self.store.create_schedule(schedule)
        if stored.value["state"] == "pending":
            result = self._pending_response(stored.value, now_ts=now_ts)
        else:
            result = self._public(stored.value)
        result.update({"status": "ok", "needs_confirmation": stored.value["state"] == "pending"})
        return result

    def edit_schedule(
        self,
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
        stored = self._get(schedule_id)
        self._require_revision(stored, revision)
        if stored.value.get("state") not in _EDITABLE_STATES:
            raise SchedulerError("only pending or needs-attention schedules can be edited")
        now_ts = self.now_fn()
        value = copy.deepcopy(stored.value)
        relative_at = self._relative_at(
            now_ts=now_ts,
            at=at,
            in_minutes=in_minutes,
            in_hours=in_hours,
            in_days=in_days,
        )
        changes = {
            "what": what,
            "recurrence": recurrence,
            "time": time,
            "weekday": weekday,
            "interval_minutes": interval_minutes,
            "at": at,
        }
        for key, change in changes.items():
            if change is not None:
                value[key] = change
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
        updated = self._replace(stored, value)
        result = self._pending_response(updated.value, now_ts=now_ts)
        result.update({"status": "ok", "needs_confirmation": True})
        return result

    def confirm_schedule(self, schedule_id: str, *, revision: int) -> dict[str, Any]:
        stored = self._get(schedule_id)
        self._require_revision(stored, revision)
        if stored.value.get("state") not in _EDITABLE_STATES:
            raise SchedulerError("only pending or needs-attention schedules can be confirmed")
        active = activate_schedule(stored.value, now_ts=self.now_fn())
        updated = self._replace(stored, active)
        result = self._public(updated.value)
        result["status"] = "ok"
        return result

    def list_schedules(self) -> dict[str, Any]:
        groups: dict[str, list[dict[str, Any]]] = {
            "active": [],
            "pending": [],
            "paused": [],
            "needs_attention": [],
            "completed": [],
            "cancelled": [],
        }
        for stored in self.store.list_schedules():
            value = self._public(stored.value)
            state = str(value.get("state") or "needs_attention")
            groups.setdefault(state, []).append(value)
        return {"status": "ok", "backend": self.backend_name, **groups}

    def cancel_schedule(self, schedule_id: str, *, revision: int) -> dict[str, Any]:
        stored = self._get(schedule_id)
        self._require_revision(stored, revision)
        if stored.value.get("state") == "cancelled":
            return {"status": "ok", **self._public(stored.value), "note": "already cancelled"}
        value = dict(stored.value)
        value.update({"state": "cancelled", "next_run_at": None, "updated_at": utc_iso(self.now_fn())})
        updated = self._replace(stored, value)
        return {"status": "ok", **self._public(updated.value)}

    def pause_schedule(self, schedule_id: str, *, revision: int) -> dict[str, Any]:
        stored = self._get(schedule_id)
        self._require_revision(stored, revision)
        if stored.value.get("state") != "active":
            raise SchedulerError("only an active schedule can be paused")
        value = dict(stored.value)
        value.update({"state": "paused", "next_run_at": None, "updated_at": utc_iso(self.now_fn())})
        updated = self._replace(stored, value)
        return {"status": "ok", **self._public(updated.value)}

    def resume_schedule(self, schedule_id: str, *, revision: int) -> dict[str, Any]:
        stored = self._get(schedule_id)
        self._require_revision(stored, revision)
        if stored.value.get("state") != "paused":
            raise SchedulerError("only a paused schedule can be resumed")
        value = resume_schedule(stored.value, now_ts=self.now_fn())
        updated = self._replace(stored, value)
        return {"status": "ok", **self._public(updated.value)}

    def move_schedule_here(
        self,
        ctx: Any,
        *,
        schedule_id: str,
        revision: int,
    ) -> dict[str, Any]:
        delivery = self._current_delivery(ctx)
        stored = self._get(schedule_id)
        self._require_revision(stored, revision)
        if stored.value.get("state") not in _MOVABLE_STATES:
            raise SchedulerError("only a live or pending schedule can be moved")
        value = dict(stored.value)
        value.update({"delivery": delivery, "updated_at": utc_iso(self.now_fn())})
        value["fingerprint"] = definition_fingerprint(value)
        updated = self._replace(stored, value)
        return {"status": "ok", **self._public(updated.value)}

    def tick(self) -> int:
        now_ts = self.now_fn()
        dispatched = 0
        for candidate in self.store.list_schedules():
            value = candidate.value
            if value.get("state") != "active" or not value.get("next_run_at"):
                continue
            try:
                if parse_iso_timestamp(value["next_run_at"]) > now_ts:
                    continue
                if self._dispatch_due(value["id"], now_ts=now_ts):
                    dispatched += 1
            except (ScheduleValidationError, SchedulerError, StorageError) as exc:
                self.api.log("warning", f"tasks-scheduler: failed {value.get('id')}: {exc}")
        return dispatched

    def _dispatch_due(self, schedule_id: str, *, now_ts: float) -> bool:
        stored = self._get(schedule_id)
        value = stored.value
        if value.get("state") != "active" or not value.get("next_run_at"):
            return False
        slot_ts = parse_iso_timestamp(value["next_run_at"])
        if slot_ts > now_ts:
            return False
        slot_id = str(int(slot_ts))
        receipt = {
            "schedule_id": schedule_id,
            "slot_id": slot_id,
            "schedule_revision": value.get("revision"),
            "status": "claimed",
            "claimed_at": utc_iso(now_ts),
        }
        if not self.store.claim_slot(schedule_id, slot_id, receipt):
            existing = self.store.get_receipt(schedule_id, slot_id) or {}
            if existing.get("status") == "dispatched":
                self._persist_dispatch(schedule_id, value["next_run_at"], now_ts, success=True, error="")
            elif existing.get("status") == "needs_attention":
                self._persist_dispatch(
                    schedule_id,
                    value["next_run_at"],
                    now_ts,
                    success=False,
                    error=str(existing.get("error") or "dispatch requires attention"),
                )
            elif existing.get("status") == "claimed":
                claimed_at = parse_iso_timestamp(existing.get("claimed_at"))
                if now_ts - claimed_at >= 300:
                    error = "dispatch outcome is unknown after a stale slot claim"
                    existing.update(
                        {
                            "status": "needs_attention",
                            "finished_at": utc_iso(now_ts),
                            "error": error,
                        }
                    )
                    self.store.update_receipt(schedule_id, slot_id, existing)
                    self._persist_dispatch(
                        schedule_id,
                        value["next_run_at"],
                        now_ts,
                        success=False,
                        error=error,
                    )
            return False

        current = self._get(schedule_id).value
        if current.get("state") != "active" or current.get("next_run_at") != value.get("next_run_at"):
            receipt.update({"status": "aborted", "finished_at": utc_iso(self.now_fn())})
            self.store.update_receipt(schedule_id, slot_id, receipt)
            return False

        delivery = current.get("delivery") if isinstance(current.get("delivery"), dict) else {}
        chat_id = int(delivery.get("chat_id") or 0)
        if chat_id <= 0:
            raise SchedulerError("schedule has no valid delivery chat")
        run_id = f"{schedule_id}:{slot_id}"
        try:
            if str(current.get("execution_kind") or "agent_task") == "reminder":
                self.api.send_message(
                    current["what"],
                    chat_id=chat_id,
                    source=str(delivery.get("source") or "dialogs"),
                    task_id=run_id,
                )
            else:
                prompt = (
                    f"Scheduled run {run_id}. Execute this self-contained task. "
                    "Do not create or modify schedules unless the task explicitly asks for it.\n\n"
                    f"{current['what']}"
                )
                self.api.inject_chat(
                    prompt,
                    chat_id=chat_id,
                    user_id=chat_id,
                    sender_label="Task Scheduler",
                    delivery_source=str(delivery.get("source") or ""),
                )
        except Exception as exc:
            receipt.update(
                {
                    "status": "needs_attention",
                    "finished_at": utc_iso(self.now_fn()),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            self.store.update_receipt(schedule_id, slot_id, receipt)
            self._persist_dispatch(schedule_id, value["next_run_at"], now_ts, success=False, error=receipt["error"])
            return False

        receipt.update({"status": "dispatched", "finished_at": utc_iso(self.now_fn())})
        self.store.update_receipt(schedule_id, slot_id, receipt)
        self._persist_dispatch(schedule_id, value["next_run_at"], now_ts, success=True, error="")
        return True

    def _persist_dispatch(
        self,
        schedule_id: str,
        slot_at: str,
        now_ts: float,
        *,
        success: bool,
        error: str,
    ) -> None:
        for _attempt in range(3):
            stored = self._get(schedule_id)
            value = dict(stored.value)
            if value.get("next_run_at") != slot_at:
                return
            value["last_dispatch"] = {
                "slot_at": slot_at,
                "status": "dispatched" if success else "needs_attention",
                "recorded_at": utc_iso(now_ts),
                "error": error,
            }
            if value.get("state") == "active":
                if value.get("recurrence") == "once":
                    value["state"] = "completed" if success else "needs_attention"
                    value["next_run_at"] = None
                else:
                    next_run = next_occurrence(value, after_ts=now_ts)
                    value["next_run_at"] = utc_iso(next_run) if next_run is not None else None
            value["updated_at"] = utc_iso(now_ts)
            try:
                self.store.replace_schedule(value, expected_token=stored.token)
                return
            except ConcurrentUpdateError:
                continue
        raise SchedulerError("could not persist dispatch result after concurrent updates")

    @staticmethod
    def _public(schedule: dict[str, Any]) -> dict[str, Any]:
        value = copy.deepcopy(schedule)
        value["execution_kind"] = str(value.get("execution_kind") or "agent_task")
        value.pop("fingerprint", None)
        value["summary"] = schedule_summary(value)
        if any(marker in str(value.get("what") or "").lower() for marker in _CONTEXT_MARKERS):
            value["warning"] = "what may depend on the current conversation; scheduled runs have no chat history"
        if value.get("next_run_at"):
            value["next_run"] = display_time(value["next_run_at"], str(value.get("timezone") or "Europe/Moscow"))
        return value

    def _pending_response(self, schedule: dict[str, Any], *, now_ts: float) -> dict[str, Any]:
        result = self._public(schedule)
        preview = activate_schedule(schedule, now_ts=now_ts)
        result["preview_next_run"] = display_time(
            preview["next_run_at"],
            str(schedule.get("timezone") or "Europe/Moscow"),
        )
        return result


__all__ = ["SchedulerError", "SchedulerService"]
