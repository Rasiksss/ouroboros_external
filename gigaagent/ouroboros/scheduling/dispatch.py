"""Dispatch of due slots; delivery arrives as two callables from the composition root.

Why every taken slot must write an outcome, and what `permanent` separates: `.rules/scheduling.md`.
"""

from __future__ import annotations

import logging
import time as time_module
from collections.abc import Callable
from typing import Any, Protocol

from config_registry.chat_registry.interfaces import ChatRegistryStore
from new_core.identity import OwnerScope

from .core import ScheduleDefinition, ScheduleValidationError, next_occurrence, parse_iso_timestamp, utc_iso
from .service import SchedulerError, require_delivery_chat
from .store import (
    ClaimedSlot,
    ConcurrentUpdateError,
    DispatchStore,
    ScheduleCorruptionError,
    ScheduleNotFoundError,
    StorageError,
    StoredSchedule,
)

log = logging.getLogger(__name__)

#: A claim this old with no outcome is not retried — the delivery may already have happened.
_STALE_CLAIM_SECONDS = 300
#: One slot's failure must not abandon the batch; every one of these ends a slot, not the pass.
_SLOT_ERRORS = (ScheduleCorruptionError, ScheduleValidationError, SchedulerError, StorageError)

#: Well inside `_STALE_CLAIM_SECONDS`: the claim only has to outlive the batch, not each slot.
_HEARTBEAT_SECONDS = 60.0
RUN_RETENTION_SECONDS = 30 * 24 * 3600
SWEEP_INTERVAL_SECONDS = 6 * 3600


class SendReminder(Protocol):
    """Deliver reminder text; a raise means the publish failed."""

    def __call__(self, text: str, *, scope: OwnerScope, transport: dict[str, Any]) -> None: ...


class SubmitTask(Protocol):
    """Enqueue a scheduled agent task under the stored scope."""

    def __call__(self, text: str, *, scope: OwnerScope, client_message_id: str, transport: dict[str, Any]) -> None: ...


def _delivery_transport(delivery: Any) -> dict[str, Any]:
    """Delivery hints from the row; a malformed one degrades to none rather than killing the schedule."""
    hints = delivery.get("transport") if isinstance(delivery, dict) else None
    return dict(hints) if isinstance(hints, dict) else {}


def _claimed_at_or_none(value: Any) -> float | None:
    try:
        return parse_iso_timestamp(value)
    except ScheduleValidationError:
        return None


def _delivery_scope(delivery: Any) -> OwnerScope:
    """The row's own scope; no fallback to a configured owner."""
    payload = delivery.get("scope") if isinstance(delivery, dict) else None
    try:
        return OwnerScope.from_dict(payload)
    except ValueError as exc:
        raise SchedulerError("schedule carries no usable delivery scope") from exc


class ScheduleDispatcher:
    def __init__(
        self,
        store: DispatchStore,
        *,
        chats: ChatRegistryStore,
        send_message: SendReminder,
        submit_task: SubmitTask,
        batch_limit: int,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        self.store = store
        self._chats = chats
        self._send_message = send_message
        self._submit_task = submit_task
        self._batch_limit = max(1, int(batch_limit))
        self._should_stop = should_stop
        self._next_heartbeat_at = 0.0
        self._next_sweep_at = 0.0

    def _tick(self) -> int:
        """Claim this instance's share of due slots, then deliver them; returns how many went out."""
        now_ts = time_module.time()
        batch = self.store.claim_due_slots(
            utc_iso(now_ts),
            limit=self._batch_limit,
            stale_before_iso=utc_iso(now_ts - _STALE_CLAIM_SECONDS),
        )
        # The claim just stamped every slot in this batch, so nothing needs restamping yet.
        self._next_heartbeat_at = now_ts + _HEARTBEAT_SECONDS
        started = 0
        dispatched = 0
        try:
            for schedule_id in batch.undecodable:
                if self._should_stop():
                    return dispatched
                log.error("Parking an undecodable schedule id=%s", schedule_id)
                self._park(schedule_id, now_ts=now_ts)
            for taken in batch.lost:
                if self._should_stop():
                    return dispatched
                try:
                    self._absorb_lost_claim(taken.schedule, taken.slot_id, now_ts=now_ts)
                except _SLOT_ERRORS as exc:
                    self._report(taken, exc)
            for index, taken in enumerate(batch.claimed):
                if self._should_stop():
                    return dispatched
                started = index + 1
                # A pass runs for as long as its batch takes, so the next occurrence and every
                # timestamp below it are measured here rather than at the scan that opened the pass.
                slot_ts = time_module.time()
                self._heartbeat(batch.claimed[index:], now_ts=slot_ts)
                try:
                    if self._deliver_claimed(taken, now_ts=slot_ts):
                        dispatched += 1
                except _SLOT_ERRORS as exc:
                    self._report(taken, exc)
        finally:
            # Whatever this pass claimed and never started, on every way out of it.
            self._release(batch.claimed[started:])
        return dispatched

    def _heartbeat(self, owed: list[ClaimedSlot], *, now_ts: float) -> None:
        """Keep the claims this pass still owes alive; restamping any wider is the bug the rule names."""
        if now_ts < self._next_heartbeat_at:
            return
        self._next_heartbeat_at = now_ts + _HEARTBEAT_SECONDS
        try:
            self.store.refresh_claims([taken.key for taken in owed], now_iso=utc_iso(now_ts))
        except StorageError:
            log.warning("Could not refresh %d live claim(s)", len(owed), exc_info=True)

    def _release(self, remaining: list[ClaimedSlot]) -> None:
        """Un-claim what a stopping pass will not reach; a peer then delivers it on its own pass."""
        try:
            self.store.release_claims([taken.key for taken in remaining])
        except StorageError:
            log.warning("Could not release %d unstarted claim(s)", len(remaining), exc_info=True)

    def _report(self, taken: ClaimedSlot, exc: Exception) -> None:
        """One slot's failure is its own; the rest of the batch still runs."""
        schedule_id = taken.schedule.schedule_id
        if isinstance(exc, ScheduleCorruptionError):
            log.error("Parking an undecodable schedule id=%s", schedule_id, exc_info=exc)
            self._park(schedule_id, now_ts=time_module.time())
            return
        log.warning("Schedule dispatch failed id=%s: %s", schedule_id, exc)

    def _park(self, schedule_id: str, *, now_ts: float) -> None:
        try:
            self.store.park_undecodable(schedule_id, now_iso=utc_iso(now_ts))
        except StorageError:
            log.warning("Could not park schedule id=%s", schedule_id, exc_info=True)

    def _sweep_runs(self) -> int:
        now = time_module.time()
        return self.store.sweep_runs(
            now_iso=utc_iso(now),
            stale_before_iso=utc_iso(now - _STALE_CLAIM_SECONDS),
            cutoff_iso=utc_iso(now - RUN_RETENTION_SECONDS),
            limit=self._batch_limit,
        )

    def run_pass(self) -> None:
        """One worker pass; the sweep keeps its own deadline so a failing tick cannot starve it."""
        try:
            self._tick()
        except Exception:
            log.warning("Schedule dispatch pass failed", exc_info=True)
        now = time_module.time()
        if now < self._next_sweep_at:
            return
        self._next_sweep_at = now + SWEEP_INTERVAL_SECONDS
        try:
            self._sweep_runs()
        except Exception:
            log.warning("Schedule receipt sweep failed", exc_info=True)

    def _require_live_conversation(self, scope: OwnerScope) -> None:
        try:
            record = self._chats.get_chat(scope)
        except Exception as exc:
            raise StorageError("the conversation registry could not be read") from exc
        if record is not None and not record.is_active:
            raise SchedulerError("schedule delivers into a conversation the owner retired")

    def _deliver_claimed(self, taken: ClaimedSlot, *, now_ts: float) -> bool:
        """Act on a slot this instance holds. The claim is already committed; nothing here is locked."""
        schedule_id, slot_id = taken.key
        # A claim exists only for a row the due scan selected, so this is a real timestamp;
        # an empty one matches no stored value and makes `_persist_dispatch` stand down.
        slot_at = taken.schedule.value.get("next_run_at") or ""

        if not self.store.holds_claim(schedule_id, slot_id):
            # A peer ended this slot while this instance worked through the batch ahead of it.
            return False

        current = self.store.get_for_dispatch(schedule_id)
        if current is None or current.value.get("state") != "active" or current.value.get("next_run_at") != slot_at:
            self.store.update_receipt(schedule_id, slot_id, status="aborted", finished_at=utc_iso(time_module.time()))
            return False

        try:
            # All three raise for a property of the row, so every future occurrence would fail identically.
            delivery_scope = _delivery_scope(current.value.get("delivery"))
            require_delivery_chat(delivery_scope)
            self._require_live_conversation(delivery_scope)
        except SchedulerError as exc:
            return self._fail(current, slot_id, slot_at, now_ts, exc, permanent=True)
        try:
            transport = _delivery_transport(current.value.get("delivery"))
            if str(current.value.get("execution_kind") or "agent_task") == "reminder":
                self._send_message(current.value["what"], scope=delivery_scope, transport=transport)
            else:
                run_id = f"{schedule_id}:{slot_id}"
                prompt = (
                    f"Scheduled run {run_id}. Execute this self-contained task. "
                    "Do not create or modify schedules unless the task explicitly asks for it.\n\n"
                    f"{current.value['what']}"
                )
                # Deterministic per slot, so a repeated injection deduplicates at the inbox.
                self._submit_task(
                    prompt,
                    scope=delivery_scope,
                    client_message_id=f"schedule:{schedule_id}:{slot_id}",
                    transport=transport,
                )
        except Exception as exc:
            return self._fail(current, slot_id, slot_at, now_ts, exc, permanent=False)

        self.store.update_receipt(schedule_id, slot_id, status="dispatched", finished_at=utc_iso(time_module.time()))
        self._persist_dispatch(current, slot_at, now_ts, success=True, error="")
        return True

    def _fail(
        self, current: StoredSchedule, slot_id: str, slot_at: str, now_ts: float, exc: Exception, *, permanent: bool
    ) -> bool:
        """Record a failed slot on the receipt first: a peer reading it before the schedule decides with it."""
        error = f"{type(exc).__name__}: {exc}"
        self.store.update_receipt(
            current.schedule_id,
            slot_id,
            status="needs_attention",
            finished_at=utc_iso(time_module.time()),
            error=error,
            permanent=permanent,
        )
        self._persist_dispatch(current, slot_at, now_ts, success=False, error=error, permanent=permanent)
        return False

    def _absorb_lost_claim(self, stored: StoredSchedule, slot_id: str, *, now_ts: float) -> None:
        """Take the winner's outcome for a slot this instance did not get; branches are exhaustive."""
        schedule_id = stored.schedule_id
        slot_at = stored.value.get("next_run_at") or ""
        existing = self.store.get_receipt(schedule_id, slot_id)
        if existing is None:
            # A stopping peer handed the slot back before this pass read it; nothing ran, so the
            # row is due again and the next pass claims it. Ending it here would kill it unfired.
            return
        if existing.status == "dispatched":
            self._persist_dispatch(stored, slot_at, now_ts, success=True, error="")
        elif existing.status == "needs_attention":
            self._persist_dispatch(
                stored,
                slot_at,
                now_ts,
                success=False,
                error=existing.error or "dispatch requires attention",
                permanent=existing.permanent,
            )
        elif existing.status == "aborted":
            # Recording success would complete a schedule the owner never heard from.
            return
        elif existing.status != "claimed":
            self._end_unknown_outcome(stored, slot_at, now_ts=now_ts)
        elif (claimed_at := _claimed_at_or_none(existing.claimed_at)) is None:
            # A claim whose timestamp will not parse can never age out of the stale window.
            self._end_unknown_outcome(stored, slot_at, now_ts=now_ts)
        elif claimed_at - now_ts > _STALE_CLAIM_SECONDS:
            # Nor can one stamped further ahead than any live claim could be: the row is not
            # hidden, and every later pass would read it as fresh and leave it with no outcome.
            # Ordinary skew between replicas stays under this and is left alone as a live claim.
            self._end_unknown_outcome(stored, slot_at, now_ts=now_ts)
        elif now_ts - claimed_at >= _STALE_CLAIM_SECONDS:
            error = "dispatch outcome is unknown after a stale slot claim"
            self.store.update_receipt(
                schedule_id,
                slot_id,
                status="needs_attention",
                finished_at=utc_iso(now_ts),
                error=error,
                permanent=existing.permanent,
            )
            self._persist_dispatch(stored, slot_at, now_ts, success=False, error=error)

    def _end_unknown_outcome(self, stored: StoredSchedule, slot_at: str, *, now_ts: float) -> None:
        """The slot was taken and its receipt says nothing usable; the owner has to look."""
        self._persist_dispatch(
            stored,
            slot_at,
            now_ts,
            success=False,
            error="slot receipt is unreadable; dispatch outcome is unknown",
            permanent=True,
        )

    def _persist_dispatch(
        self,
        stored: StoredSchedule,
        slot_at: str,
        now_ts: float,
        *,
        success: bool,
        error: str,
        permanent: bool = False,
    ) -> None:
        """Record what became of one slot, and decide whether the schedule survives it."""
        schedule_id = stored.schedule_id
        for attempt in range(3):
            if attempt:
                current = self.store.get_for_dispatch(schedule_id)
                if current is None:
                    return
                stored = current
            value = stored.value.copy()
            if value.get("next_run_at") != slot_at:
                return
            value["last_dispatch"] = {
                "slot_at": slot_at,
                "status": "dispatched" if success else "needs_attention",
                "recorded_at": utc_iso(now_ts),
                "error": error,
            }
            recurring = value.get("recurrence") != "once"
            if value.get("state") == "active":
                if not success and (permanent or not recurring):
                    value["state"] = "needs_attention"
                    value["next_run_at"] = None
                elif not success:
                    # Skip to the next occurrence; the receipt keeps the failure, so this is no retry.
                    value["next_run_at"] = self._next_run_or_none(value, now_ts=now_ts)
                    if value["next_run_at"] is None:
                        value["state"] = "needs_attention"
                elif not recurring:
                    value["state"] = "completed"
                    value["next_run_at"] = None
                else:
                    value["next_run_at"] = self._next_run_or_none(value, now_ts=now_ts)
                    if value["next_run_at"] is None:
                        # The delivery happened; ending beats freezing at a slot the tick re-selects.
                        value["state"] = "needs_attention"
            value["updated_at"] = utc_iso(now_ts)
            try:
                self.store.replace_for_dispatch(value, expected_token=stored.token)
                return
            except ConcurrentUpdateError:
                continue
            except ScheduleNotFoundError:
                return
        raise SchedulerError("could not persist dispatch result after concurrent updates")

    @staticmethod
    def _next_run_or_none(value: ScheduleDefinition, *, now_ts: float) -> str | None:
        """A stored definition can stop resolving; a raise here would abandon the slot with no outcome."""
        try:
            next_run = next_occurrence(value, after_ts=now_ts)
        except ScheduleValidationError as exc:
            value["last_dispatch"]["error"] = f"could not schedule the next run: {exc}"
            return None
        return utc_iso(next_run) if next_run is not None else None


__all__ = [
    "RUN_RETENTION_SECONDS",
    "SWEEP_INTERVAL_SECONDS",
    "ScheduleDispatcher",
    "SendReminder",
    "SubmitTask",
]
