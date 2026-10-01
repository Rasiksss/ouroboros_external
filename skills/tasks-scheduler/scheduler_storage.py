"""Scheduler persistence over the application's namespaced state store."""

from __future__ import annotations

import json
import logging
import pathlib
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ouroboros.contracts.plugin_api import SkillStateStore

_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SLOT_RE = re.compile(r"^[0-9]{1,20}$")
_UNSAFE_LOG_FIELD_RE = re.compile(r"[^A-Za-z0-9._/-]+")

log = logging.getLogger(__name__)


class StorageError(RuntimeError):
    """Base storage failure."""


class StorageCorruptionError(StorageError):
    """Stored state was malformed and has been quarantined."""


class ConcurrentUpdateError(StorageError):
    """The caller attempted to replace an outdated schedule revision."""


class ScheduleAlreadyExistsError(StorageError):
    """A schedule id already exists."""


@dataclass(frozen=True)
class StoredSchedule:
    value: dict[str, Any]
    token: str


class ScheduleStore(Protocol):
    def create_schedule(self, schedule: dict[str, Any]) -> StoredSchedule: ...

    def get_schedule(self, schedule_id: str) -> StoredSchedule | None: ...

    def list_schedules(self) -> list[StoredSchedule]: ...

    def replace_schedule(self, schedule: dict[str, Any], *, expected_token: str) -> StoredSchedule: ...

    def claim_slot(self, schedule_id: str, slot_id: str, receipt: dict[str, Any]) -> bool: ...

    def get_receipt(self, schedule_id: str, slot_id: str) -> dict[str, Any] | None: ...

    def update_receipt(self, schedule_id: str, slot_id: str, receipt: dict[str, Any]) -> None: ...


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


def _decoded_json(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StorageCorruptionError(f"corrupt scheduler JSON: {label}") from exc
    if not isinstance(value, dict):
        raise StorageCorruptionError(f"scheduler JSON is not an object: {label}")
    return value


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", {})
    if not isinstance(response, dict):
        return ""
    error = response.get("Error")
    return str(error.get("Code") or "") if isinstance(error, dict) else ""


def _safe_log_field(value: Any, *, fallback: str = "unknown") -> str:
    normalized = _UNSAFE_LOG_FIELD_RE.sub("_", str(value or "").strip())[:200]
    return normalized or fallback


class SharedScheduleStore:
    """Scheduler storage backed by the host application's state provider."""

    backend_name = "shared"

    def __init__(self, state_store: SkillStateStore) -> None:
        self.state_store = state_store

    @staticmethod
    def _schedule_key(schedule_id: str) -> str:
        return f"schedules/{_validate_id(schedule_id)}.json"

    @staticmethod
    def _receipt_key(schedule_id: str, slot_id: str) -> str:
        return f"runs/{_validate_id(schedule_id)}/{_validate_slot(slot_id)}.json"

    def _log_failure(self, operation: str, exc: Exception) -> None:
        log.warning(
            "event=tasks_scheduler_storage_error operation=%s error_code=%s exception_type=%s namespace=skill_state",
            _safe_log_field(operation),
            _safe_log_field(_error_code(exc)),
            type(exc).__name__,
        )

    def _quarantine(self, key: str, raw: bytes) -> None:
        name = pathlib.PurePosixPath(key).name.removesuffix(".json")
        quarantine = f"quarantine/{name}.{uuid.uuid4().hex}.json"
        try:
            self.state_store.put_bytes(quarantine, raw)
            self.state_store.delete(key)
        except Exception as exc:
            self._log_failure("quarantine_schedule", exc)
            raise StorageCorruptionError(f"failed to quarantine corrupt schedule {name}") from exc

    def _get_schedule_key(self, key: str) -> StoredSchedule | None:
        try:
            raw = self.state_store.get_bytes(key)
        except Exception as exc:
            self._log_failure("read_schedule", exc)
            raise StorageError("failed to read schedule") from exc
        if raw is None:
            return None
        try:
            value = _decoded_json(raw, label=key)
            revision = int(value.get("revision") or 0)
            if revision <= 0:
                raise StorageCorruptionError(f"schedule has invalid revision: {key}")
        except StorageCorruptionError:
            self._quarantine(key, raw)
            raise
        return StoredSchedule(value=value, token=str(revision))

    def create_schedule(self, schedule: dict[str, Any]) -> StoredSchedule:
        schedule_id = _validate_id(str(schedule.get("id") or ""))
        value = dict(schedule)
        value["revision"] = 1
        try:
            created = self.state_store.create_json_if_absent(self._schedule_key(schedule_id), value)
        except Exception as exc:
            self._log_failure("create_schedule", exc)
            raise StorageError("failed to create schedule") from exc
        if not created:
            raise ScheduleAlreadyExistsError(f"schedule {schedule_id!r} already exists")
        return StoredSchedule(value=value, token="1")

    def get_schedule(self, schedule_id: str) -> StoredSchedule | None:
        return self._get_schedule_key(self._schedule_key(schedule_id))

    def list_schedules(self) -> list[StoredSchedule]:
        try:
            keys = self.state_store.list_keys("schedules/")
        except Exception as exc:
            self._log_failure("list_schedules", exc)
            raise StorageError("failed to list schedules") from exc
        result: list[StoredSchedule] = []
        for key in sorted(key for key in keys if key.endswith(".json")):
            stored = self._get_schedule_key(key)
            if stored is not None:
                result.append(stored)
        return result

    def replace_schedule(self, schedule: dict[str, Any], *, expected_token: str) -> StoredSchedule:
        schedule_id = _validate_id(str(schedule.get("id") or ""))
        key = self._schedule_key(schedule_id)
        current = self._get_schedule_key(key)
        if current is None:
            raise StorageError(f"schedule {schedule_id!r} not found")
        if current.token != str(expected_token):
            raise ConcurrentUpdateError(f"schedule {schedule_id!r} changed concurrently")
        value = dict(schedule)
        value["revision"] = int(current.value["revision"]) + 1
        try:
            replaced = self.state_store.replace_json_if_matches(key, current.value, value)
        except Exception as exc:
            self._log_failure("replace_schedule", exc)
            raise StorageError("failed to replace schedule") from exc
        if not replaced:
            raise ConcurrentUpdateError(f"schedule {schedule_id!r} changed concurrently")
        return StoredSchedule(value=value, token=str(value["revision"]))

    def claim_slot(self, schedule_id: str, slot_id: str, receipt: dict[str, Any]) -> bool:
        try:
            return self.state_store.create_json_if_absent(self._receipt_key(schedule_id, slot_id), receipt)
        except Exception as exc:
            self._log_failure("claim_slot", exc)
            raise StorageError("failed to claim schedule slot") from exc

    def get_receipt(self, schedule_id: str, slot_id: str) -> dict[str, Any] | None:
        key = self._receipt_key(schedule_id, slot_id)
        try:
            raw = self.state_store.get_bytes(key)
        except Exception as exc:
            self._log_failure("get_receipt", exc)
            raise StorageError("failed to read schedule receipt") from exc
        return _decoded_json(raw, label=key) if raw is not None else None

    def update_receipt(self, schedule_id: str, slot_id: str, receipt: dict[str, Any]) -> None:
        key = self._receipt_key(schedule_id, slot_id)
        try:
            if self.state_store.get_bytes(key) is None:
                raise StorageError("cannot update a missing schedule receipt")
            self.state_store.put_json(key, receipt)
        except StorageError:
            raise
        except Exception as exc:
            self._log_failure("update_receipt", exc)
            raise StorageError("failed to update schedule receipt") from exc


__all__ = [
    "ConcurrentUpdateError",
    "ScheduleAlreadyExistsError",
    "ScheduleStore",
    "SharedScheduleStore",
    "StorageCorruptionError",
    "StorageError",
    "StoredSchedule",
]
