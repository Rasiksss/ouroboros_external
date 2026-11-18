"""Schedule rows in the runtime database: revision CAS, slot claims, the due query.

Which methods carry the owner predicate and which belong to the dispatcher: `.rules/scheduling.md`.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, cast

from cryptography.exceptions import InvalidTag
from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError

from new_core.identity import OwnerScope
from ouroboros.storage.message_field_crypto import FieldKeyUnavailable, decrypt_field, encrypt_field

from .core import ScheduleDefinition, ScheduleValidationError, parse_iso_timestamp, utc_iso

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine

log = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SLOT_RE = re.compile(r"^[0-9]{1,20}$")

_OWNED = "owner_id=:owner_id AND agent_id=:agent_id AND chat_id=:chat_id"

_LISTING_LIMIT = 15

#: A live peer's claim hides the row: without this every replica re-selects the batch the first
#: one is still delivering, loses every claim, and takes no new work of its own. The upper bound
#: matters as much as the lower: `claimed_at` is text, and a value that is not a timestamp sorts
#: above every horizon, which would hide its row from every replica for good.
_UNCLAIMED = (
    "NOT EXISTS (SELECT 1 FROM schedule_runs r WHERE r.schedule_id = s.schedule_id "
    "AND r.status = 'claimed' AND r.claimed_at > :stale_before AND r.claimed_at <= :now)"
)
_DUE_WHERE = ["s.state='active'", "s.next_run_at != ''", "s.next_run_at <= :now", _UNCLAIMED]
_DUE_TAIL = " ORDER BY s.next_run_at LIMIT :limit"

_CLAIM_SLOT = """
INSERT INTO schedule_runs (
    schedule_id, slot_id, owner_id, agent_id, schedule_revision,
    status, claimed_at, finished_at, error, permanent
)
-- Owner axes from the row itself, so a claim cannot disagree with it.
SELECT schedule_id, :slot_id, owner_id, agent_id, :schedule_revision,
       'claimed', :claimed_at, '', '', 0
FROM schedules WHERE schedule_id=:schedule_id
ON CONFLICT(schedule_id, slot_id) DO NOTHING
"""


class StorageError(RuntimeError):
    """Base storage failure."""


class ScheduleCorruptionError(StorageError):
    """A stored row could not be decoded; its content is unusable, not merely unavailable."""


class ScheduleUnreadableError(StorageError):
    """This process cannot read the row; the row itself may be perfectly good on a peer."""


class ConcurrentUpdateError(StorageError):
    """The caller attempted to replace an outdated schedule revision."""


class ScheduleNotFoundError(StorageError):
    """No definition is accessible under the caller's scope."""


class ScheduleAlreadyExistsError(StorageError):
    """A schedule id already exists."""


class SlotKey(NamedTuple):
    """A receipt's whole primary key; named, because both halves are strings."""

    schedule_id: str
    slot_id: str


@dataclass(frozen=True)
class Receipt:
    """One taken slot's row: the outcome a peer reads when it loses the claim."""

    schedule_id: str
    slot_id: str
    schedule_revision: int
    status: str
    claimed_at: str
    finished_at: str
    error: str
    permanent: bool


@dataclass(frozen=True)
class StoredSchedule:
    """A decrypted row: its id from the column, the definition, and the revision CAS token.

    `value` stays open on purpose — `core.py` normalizes and fingerprints it by dynamic key.
    """

    schedule_id: str
    value: ScheduleDefinition
    token: str


@dataclass(frozen=True)
class ClaimedSlot:
    """One due schedule as it was read under the claim, and the slot the claim was for."""

    schedule: StoredSchedule
    slot_id: str

    @property
    def key(self) -> SlotKey:
        return SlotKey(self.schedule.schedule_id, self.slot_id)


@dataclass(frozen=True)
class DueBatch:
    """What one pass took: slots this instance holds, slots a peer holds, rows that will not read."""

    claimed: list[ClaimedSlot]
    lost: list[ClaimedSlot]
    undecodable: list[str]


class ScheduleStore(Protocol):
    """Everything a tool reaches. Every method narrows to the caller's scope; see `DispatchStore`."""

    backend_name: str

    def create_schedule(self, schedule: ScheduleDefinition, *, scope: OwnerScope) -> StoredSchedule: ...

    def get_schedule(self, schedule_id: str, *, scope: OwnerScope) -> StoredSchedule | None: ...

    def list_schedules(
        self, *, scope: OwnerScope, state: str = "", offset: int = 0, limit: int = _LISTING_LIMIT
    ) -> list[StoredSchedule]: ...

    def count_schedules_by_state(self, *, scope: OwnerScope) -> dict[str, int]: ...

    def replace_schedule(
        self, schedule: ScheduleDefinition, *, expected_token: str, scope: OwnerScope
    ) -> StoredSchedule: ...

    def delete_schedule(self, schedule_id: str, *, revision: int, scope: OwnerScope) -> None: ...


class DispatchStore(Protocol):
    """Everything the tick reaches. No scope: a tick has no caller, so it owns every owner's rows."""

    def claim_due_slots(self, now_iso: str, *, limit: int, stale_before_iso: str) -> DueBatch: ...

    def refresh_claims(self, slots: Sequence[SlotKey], *, now_iso: str) -> None: ...

    def holds_claim(self, schedule_id: str, slot_id: str) -> bool: ...

    def release_claims(self, slots: Sequence[SlotKey]) -> int: ...

    def get_for_dispatch(self, schedule_id: str) -> StoredSchedule | None: ...

    def replace_for_dispatch(self, schedule: ScheduleDefinition, *, expected_token: str) -> StoredSchedule: ...

    def get_receipt(self, schedule_id: str, slot_id: str) -> Receipt | None: ...

    def update_receipt(
        self, schedule_id: str, slot_id: str, *, status: str, finished_at: str, error: str = "", permanent: bool = False
    ) -> None: ...

    def park_undecodable(self, schedule_id: str, *, now_iso: str) -> None: ...

    def sweep_runs(self, *, now_iso: str, stale_before_iso: str, cutoff_iso: str, limit: int) -> int: ...


def _validate_id(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _ID_RE.fullmatch(normalized):
        raise StorageError("invalid schedule id")
    return normalized


def _validate_slot(value: str) -> str:
    normalized = str(value or "").strip()
    if not _SLOT_RE.fullmatch(normalized):
        raise StorageError("invalid schedule slot id")
    return normalized


def _slot_keys(slots: Sequence[SlotKey]) -> list[SlotKey]:
    """The receipts' own primary key, validated as every other receipt method validates its own."""
    return [SlotKey(_validate_id(schedule_id), _validate_slot(slot)) for schedule_id, slot in slots]


def _owned_params(scope: OwnerScope) -> dict[str, Any]:
    return {"owner_id": scope.owner_id, "agent_id": scope.agent_id, "chat_id": scope.chat_id}


def due_select_for() -> str:
    return (
        "SELECT schedule_id, revision, payload_json, state, next_run_at FROM schedules s WHERE "
        + " AND ".join(_DUE_WHERE)
        + _DUE_TAIL
        + " FOR UPDATE SKIP LOCKED"
    )


class SqlScheduleStore(ScheduleStore, DispatchStore):
    """Schedules and slot receipts over the runtime DB engine this process composed."""

    backend_name = "database"

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @contextmanager
    def _guard(self, operation: str) -> Iterator[Connection]:
        """One transaction; the driver message is logged by type only — it names host and user."""
        try:
            with self._engine.begin() as conn:
                yield conn
        except SQLAlchemyError as exc:
            log.warning("event=schedule_storage_error operation=%s exception_type=%s", operation, type(exc).__name__)
            raise StorageError(f"schedule storage is unavailable: {operation}") from exc

    def _decode(self, schedule_id: Any, revision: Any, payload: Any, state: Any, next_run_at: Any) -> StoredSchedule:
        """Broad `except`: a failed decrypt raises `InvalidTag`, which is not a `ValueError`."""
        try:
            value = json.loads(decrypt_field(str(payload or "")))
        except Exception as exc:
            log.warning("event=schedule_decode_error exception_type=%s", type(exc).__name__)
            if isinstance(exc, (InvalidTag, FieldKeyUnavailable)):
                # No key, or the wrong one: this reader is broken, not the row. Parking is
                # fleet-wide and durable, so a malformed blob falls through to corruption.
                raise ScheduleUnreadableError("this process cannot decrypt an encrypted schedule") from exc
            raise ScheduleCorruptionError("stored schedule could not be decoded") from exc
        if not isinstance(value, dict):
            raise ScheduleCorruptionError("stored schedule is not an object")
        # The one cast in the package: this is where stored JSON becomes the declared shape.
        value = cast(ScheduleDefinition, value)
        # The row's own key, like the two columns below it: receipts are addressed by the column,
        # so a payload that disagreed would leave every claim of that slot a silent no-op.
        value["id"] = str(schedule_id)
        value["state"] = str(state or "")
        value["next_run_at"] = str(next_run_at or "") or None
        return StoredSchedule(schedule_id=str(schedule_id), value=value, token=str(int(revision)))

    def _row_params(self, value: ScheduleDefinition) -> dict[str, Any]:
        return {
            "schedule_id": _validate_id(str(value.get("id") or "")),
            "revision": int(value["revision"]),
            "state": str(value.get("state") or ""),
            "next_run_at": str(value.get("next_run_at") or ""),
            "payload_json": encrypt_field(json.dumps(value, ensure_ascii=False, sort_keys=True)),
            "updated_at": str(value.get("updated_at") or ""),
        }

    def create_schedule(self, schedule: ScheduleDefinition, *, scope: OwnerScope) -> StoredSchedule:
        value = schedule.copy()
        value["revision"] = 1
        params = {
            **self._row_params(value),
            **_owned_params(scope),
            "created_at": str(value.get("created_at") or ""),
        }
        with self._guard("create_schedule") as conn:
            applied = conn.execute(
                text(
                    """
                    INSERT INTO schedules (
                        schedule_id, owner_id, agent_id, chat_id, revision, state,
                        next_run_at, payload_json, created_at, updated_at
                    ) VALUES (
                        :schedule_id, :owner_id, :agent_id, :chat_id, :revision, :state,
                        :next_run_at, :payload_json, :created_at, :updated_at
                    )
                    ON CONFLICT(schedule_id) DO NOTHING
                    """
                ),
                params,
            ).rowcount
        if not applied:
            raise ScheduleAlreadyExistsError(f"schedule {params['schedule_id']!r} already exists")
        return StoredSchedule(schedule_id=str(params["schedule_id"]), value=value, token="1")

    def get_schedule(self, schedule_id: str, *, scope: OwnerScope) -> StoredSchedule | None:
        with self._guard("get_schedule") as conn:
            row = conn.execute(
                text(
                    "SELECT schedule_id, revision, payload_json, state, next_run_at FROM schedules "
                    f"WHERE schedule_id=:schedule_id AND {_OWNED}"
                ),
                {"schedule_id": _validate_id(schedule_id), **_owned_params(scope)},
            ).fetchone()
        return self._decode(*row) if row else None

    def list_schedules(
        self, *, scope: OwnerScope, state: str = "", offset: int = 0, limit: int = _LISTING_LIMIT
    ) -> list[StoredSchedule]:
        predicate = _OWNED + (" AND state=:state" if state else "")
        with self._guard("list_schedules") as conn:
            rows = conn.execute(
                text(
                    "SELECT schedule_id, revision, payload_json, state, next_run_at FROM schedules "
                    f"WHERE {predicate} ORDER BY "
                    "CASE WHEN state IN ('completed', 'cancelled') THEN 1 ELSE 0 END, "
                    "CASE WHEN next_run_at = '' THEN 1 ELSE 0 END, next_run_at, updated_at DESC "
                    "LIMIT :limit OFFSET :offset"
                ),
                {
                    **_owned_params(scope),
                    **({"state": state} if state else {}),
                    "limit": max(1, int(limit)),
                    "offset": max(0, int(offset)),
                },
            ).fetchall()
        return self._decode_listing(rows)

    def _decode_listing(self, rows: Sequence[Any]) -> list[StoredSchedule]:
        """One corrupt row must not cost the owner every other row.

        The tick already reports such a row rather than raising; a listing that raised instead
        would leave the owner unable to see, create or cancel anything until someone intervened.
        `ScheduleUnreadableError` is deliberately not caught: this reader has no key, and an empty
        page would tell the owner they have no schedules when a peer can read every one of them.
        """
        listed: list[StoredSchedule] = []
        skipped = 0
        for row in rows:
            try:
                listed.append(self._decode(*row))
            except ScheduleCorruptionError:
                skipped += 1
        if skipped:
            log.error("event=schedule_listing_skipped_corrupt count=%d", skipped)
        return listed

    def count_schedules_by_state(self, *, scope: OwnerScope) -> dict[str, int]:
        with self._guard("count_schedules_by_state") as conn:
            rows = conn.execute(
                text(f"SELECT state, COUNT(*) FROM schedules WHERE {_OWNED} GROUP BY state"),
                _owned_params(scope),
            ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def replace_schedule(
        self, schedule: ScheduleDefinition, *, expected_token: str, scope: OwnerScope
    ) -> StoredSchedule:
        return self._replace(
            schedule, expected_token=expected_token, scoped=f" AND {_OWNED}", params=_owned_params(scope)
        )

    def delete_schedule(self, schedule_id: str, *, revision: int, scope: OwnerScope) -> None:
        params = {"schedule_id": _validate_id(schedule_id), "revision": revision, **_owned_params(scope)}
        with self._guard("delete_schedule") as conn:
            applied = conn.execute(
                text(f"DELETE FROM schedules WHERE schedule_id=:schedule_id AND revision=:revision AND {_OWNED}"),
                params,
            ).rowcount
            if not applied:
                exists = conn.execute(
                    text(f"SELECT 1 FROM schedules WHERE schedule_id=:schedule_id AND {_OWNED}"), params
                ).fetchone()
                if exists is None:
                    raise ScheduleNotFoundError("schedule not found")
                raise ConcurrentUpdateError("schedule revision changed; list schedules and retry")

    def claim_due_slots(self, now_iso: str, *, limit: int, stale_before_iso: str) -> DueBatch:
        """Claim the oldest unclaimed due slots; on PostgreSQL scan and inserts are one transaction.

        A row that will not decode is reported, not raised: one of them must not cost the batch.
        """
        claimed: list[ClaimedSlot] = []
        lost: list[ClaimedSlot] = []
        undecodable: list[str] = []
        unreadable = 0
        params = {"now": str(now_iso), "limit": max(1, int(limit)), "stale_before": str(stale_before_iso)}
        with self._guard("claim_due_slots") as conn:
            rows = conn.execute(text(due_select_for()), params).fetchall()
            for row in rows:
                schedule_id = str(row[0])
                try:
                    stored = self._decode(*row)
                    slot_id = str(int(parse_iso_timestamp(row[4])))
                except ScheduleUnreadableError:
                    # This pod is the broken one; a peer with the right key claims the row normally.
                    unreadable += 1
                    continue
                except (ScheduleCorruptionError, ScheduleValidationError):
                    undecodable.append(schedule_id)
                    continue
                applied = conn.execute(
                    text(_CLAIM_SLOT),
                    {
                        "schedule_id": schedule_id,
                        "slot_id": slot_id,
                        "schedule_revision": int(stored.token),
                        "claimed_at": str(now_iso),
                    },
                ).rowcount
                (claimed if applied else lost).append(ClaimedSlot(schedule=stored, slot_id=slot_id))
        if unreadable:
            log.error("event=schedule_unreadable_here count=%d", unreadable)
        return DueBatch(claimed=claimed, lost=lost, undecodable=undecodable)

    def get_for_dispatch(self, schedule_id: str) -> StoredSchedule | None:
        with self._guard("get_for_dispatch") as conn:
            row = conn.execute(
                text(
                    "SELECT schedule_id, revision, payload_json, state, next_run_at "
                    "FROM schedules WHERE schedule_id=:schedule_id"
                ),
                {"schedule_id": _validate_id(schedule_id)},
            ).fetchone()
        return self._decode(*row) if row else None

    def replace_for_dispatch(self, schedule: ScheduleDefinition, *, expected_token: str) -> StoredSchedule:
        return self._replace(schedule, expected_token=expected_token, scoped="", params={})

    def _replace(
        self, schedule: ScheduleDefinition, *, expected_token: str, scoped: str, params: dict[str, Any]
    ) -> StoredSchedule:
        try:
            expected = int(expected_token)
        except (TypeError, ValueError) as exc:
            raise StorageError("invalid schedule revision token") from exc
        value = schedule.copy()
        value["revision"] = expected + 1
        params = {**self._row_params(value), "expected": expected, **params}
        with self._guard("replace_schedule") as conn:
            applied = conn.execute(
                text(
                    "UPDATE schedules SET revision=:revision, state=:state, next_run_at=:next_run_at, "
                    f"payload_json=:payload_json, updated_at=:updated_at "
                    f"WHERE schedule_id=:schedule_id AND revision=:expected{scoped}"
                ),
                params,
            ).rowcount
            if not applied:
                row = conn.execute(
                    text(f"SELECT 1 FROM schedules WHERE schedule_id=:schedule_id{scoped}"),
                    params,
                ).fetchone()
                if row is None:
                    raise ScheduleNotFoundError(f"schedule {params['schedule_id']!r} not found")
                raise ConcurrentUpdateError(f"schedule {params['schedule_id']!r} changed concurrently")
        return StoredSchedule(schedule_id=str(params["schedule_id"]), value=value, token=str(value["revision"]))

    def refresh_claims(self, slots: Sequence[SlotKey], *, now_iso: str) -> None:
        """Restamp the claims of one pass; bounded to those ids, for the reason in the rule."""
        if not slots:
            return
        with self._guard("refresh_claims") as conn:
            conn.execute(
                text(
                    "UPDATE schedule_runs SET claimed_at=:now "
                    "WHERE status='claimed' AND (schedule_id, slot_id) IN :pairs"
                ).bindparams(bindparam("pairs", expanding=True)),
                {"pairs": _slot_keys(slots), "now": str(now_iso)},
            )

    def holds_claim(self, schedule_id: str, slot_id: str) -> bool:
        """Whether the slot is still open; the receipt key is unique, so an open one is this pass's."""
        with self._guard("holds_claim") as conn:
            row = conn.execute(
                text(
                    "SELECT 1 FROM schedule_runs WHERE schedule_id=:schedule_id "
                    "AND slot_id=:slot_id AND status='claimed'"
                ),
                {"schedule_id": _validate_id(schedule_id), "slot_id": _validate_slot(slot_id)},
            ).fetchone()
        return row is not None

    def release_claims(self, slots: Sequence[SlotKey]) -> int:
        """Give back slots this pass claimed but never started; deleting the receipt un-claims them."""
        if not slots:
            return 0
        with self._guard("release_claims") as conn:
            return int(
                conn.execute(
                    text(
                        "DELETE FROM schedule_runs WHERE status='claimed' AND (schedule_id, slot_id) IN :pairs"
                    ).bindparams(bindparam("pairs", expanding=True)),
                    {"pairs": _slot_keys(slots)},
                ).rowcount
            )

    def get_receipt(self, schedule_id: str, slot_id: str) -> Receipt | None:
        schedule_id, slot_id = _validate_id(schedule_id), _validate_slot(slot_id)
        with self._guard("get_receipt") as conn:
            row = conn.execute(
                text(
                    "SELECT schedule_revision, status, claimed_at, finished_at, error, permanent "
                    "FROM schedule_runs WHERE schedule_id=:schedule_id AND slot_id=:slot_id"
                ),
                {"schedule_id": schedule_id, "slot_id": slot_id},
            ).fetchone()
        if row is None:
            return None
        return Receipt(
            schedule_id=schedule_id,
            slot_id=slot_id,
            schedule_revision=int(row[0] or 0),
            status=str(row[1] or ""),
            claimed_at=str(row[2] or ""),
            finished_at=str(row[3] or ""),
            error=str(row[4] or ""),
            permanent=bool(row[5]),
        )

    def update_receipt(
        self, schedule_id: str, slot_id: str, *, status: str, finished_at: str, error: str = "", permanent: bool = False
    ) -> None:
        with self._guard("update_receipt") as conn:
            applied = conn.execute(
                text(
                    "UPDATE schedule_runs SET status=:status, finished_at=:finished_at, error=:error, "
                    "permanent=:permanent WHERE schedule_id=:schedule_id AND slot_id=:slot_id"
                ),
                {
                    "schedule_id": _validate_id(schedule_id),
                    "slot_id": _validate_slot(slot_id),
                    "status": str(status or ""),
                    "finished_at": str(finished_at or ""),
                    "error": str(error or ""),
                    "permanent": 1 if permanent else 0,
                },
            ).rowcount
        if not applied:
            raise StorageError("cannot update a missing schedule receipt")

    def park_undecodable(self, schedule_id: str, *, now_iso: str) -> None:
        """Park a row whose payload will not decode, by column, so it leaves the due query."""
        with self._guard("park_undecodable") as conn:
            conn.execute(
                text(
                    "UPDATE schedules SET state='needs_attention', next_run_at='', updated_at=:now "
                    "WHERE schedule_id=:schedule_id"
                ),
                {"schedule_id": _validate_id(schedule_id), "now": str(now_iso)},
            )

    def sweep_runs(self, *, now_iso: str, stale_before_iso: str, cutoff_iso: str, limit: int) -> int:
        params = {
            "now": str(now_iso),
            "limit": max(1, int(limit)),
            "stale_before": str(stale_before_iso),
            "future_claim": utc_iso(2 * parse_iso_timestamp(now_iso) - parse_iso_timestamp(stale_before_iso)),
        }
        with self._guard("sweep_runs") as conn:
            conn.execute(
                text(
                    "UPDATE schedule_runs SET status='needs_attention', finished_at=:now, "
                    "error='dispatch outcome is unknown after deletion' WHERE (schedule_id, slot_id) IN ("
                    "SELECT schedule_id, slot_id FROM schedule_runs WHERE status='claimed' "
                    "AND (claimed_at <= :stale_before OR claimed_at > :future_claim) "
                    "AND NOT EXISTS (SELECT 1 FROM schedules s WHERE s.schedule_id=schedule_runs.schedule_id) "
                    "ORDER BY claimed_at LIMIT :limit FOR UPDATE SKIP LOCKED)"
                ),
                params,
            )
            return int(
                conn.execute(
                    text(
                        "DELETE FROM schedule_runs WHERE status != 'claimed' "
                        "AND finished_at != '' AND finished_at < :cutoff"
                    ),
                    {"cutoff": str(cutoff_iso)},
                ).rowcount
            )


def default_schedule_store() -> SqlScheduleStore:
    """The store over this process's runtime database engine."""
    from ouroboros.storage.runtime_engine import resolve_runtime_engine

    return SqlScheduleStore(resolve_runtime_engine("scheduler"))


__all__ = [
    "ClaimedSlot",
    "ConcurrentUpdateError",
    "DispatchStore",
    "DueBatch",
    "Receipt",
    "ScheduleAlreadyExistsError",
    "ScheduleCorruptionError",
    "ScheduleNotFoundError",
    "ScheduleStore",
    "ScheduleUnreadableError",
    "SlotKey",
    "SqlScheduleStore",
    "StorageError",
    "StoredSchedule",
    "default_schedule_store",
    "due_select_for",
]
