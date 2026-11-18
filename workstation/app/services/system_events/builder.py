"""Построение payload для события AGENT_SYSTEM_CONFIGURATION."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast

from app.core.config import settings as app_settings
from app.models.session import Session
from app.utils.time import ensure_utc, utcnow

CONTRACT_VERSION = "1.0"
EVENT_TYPE = "AGENT_SYSTEM_CONFIGURATION"
CONFIG_WORKSTATION_START = "aef-workstation-start"
CONFIG_WORKSTATION_CREATE_SESSION = "aef-workstation-create-session"
CONFIG_WORKSTATION_DELETE_SESSION = "aef-workstation-delete-session"

_METADATA_ENV_MAP = {
    "agent-id": "AGENT_ID",
    "cluster-id": "CLUSTER_ID",
    "namespace": "POD_NAMESPACE",
    "distributive": "DISTRIBUTIVE",
}


def build_workstation_start_event(*, now: datetime | None = None) -> dict[str, Any]:
    """Собрать событие старта AEF WorkStation."""
    current = now or utcnow()
    return _build_event(
        config_type=CONFIG_WORKSTATION_START,
        attributes={
            "aef-workstation-version": _string_or_empty(app_settings.APP_VERSION),
        },
        timestamp=current,
    )


def build_session_created_event(
    session: Session,
    *,
    total_sandbox_sessions: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Собрать событие фактического создания sandbox-сессии."""
    return _build_event(
        config_type=CONFIG_WORKSTATION_CREATE_SESSION,
        attributes={
            "aef-workstation-version": _string_or_empty(app_settings.APP_VERSION),
            "sandbox-session-id": _string_or_empty(session.id),
            "created-at": _format_datetime_or_now(session.created_at),
            "total-sandbox-session": _integer_or_zero(total_sandbox_sessions),
        },
        timestamp=now or utcnow(),
    )


def build_session_deleted_event(
    session: Session,
    *,
    total_sandbox_sessions: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Собрать событие фактического завершения sandbox-сессии."""
    return _build_event(
        config_type=CONFIG_WORKSTATION_DELETE_SESSION,
        attributes={
            "aef-workstation-version": _string_or_empty(app_settings.APP_VERSION),
            "sandbox-session-id": _string_or_empty(session.id),
            "created-at": _format_datetime_or_now(session.created_at),
            "expires-at": _format_datetime_or_now(session.terminated_at),
            "total-sandbox-session": _integer_or_zero(total_sandbox_sessions),
        },
        timestamp=now or utcnow(),
    )


def _build_event(
    *,
    config_type: str,
    attributes: Mapping[str, Any],
    timestamp: datetime,
) -> dict[str, Any]:
    """Собрать общий envelope контракта AGENT_SYSTEM_CONFIGURATION."""
    return {
        "contract-version": CONTRACT_VERSION,
        "timestamp": _format_datetime(timestamp),
        "event-type": EVENT_TYPE,
        "metadata": _build_metadata(),
        "payload": [
            {
                "config-type": config_type,
                "attributes": dict(attributes),
            }
        ],
    }


def _build_metadata() -> dict[str, str]:
    """Прочитать metadata из process env, подставляя пустые строки для пропусков."""
    return {
        field_name: _string_or_empty(os.environ.get(env_name)) for field_name, env_name in _METADATA_ENV_MAP.items()
    }


def _format_datetime_or_now(value: datetime | None) -> str:
    """Нормализовать date-time; при отсутствии значения использовать текущий UTC."""
    return _format_datetime(value or utcnow())


def _format_datetime(value: datetime) -> str:
    """Вернуть UTC timestamp в компактном ISO-формате контракта."""
    normalized = cast(datetime, ensure_utc(value))
    return normalized.isoformat(timespec="seconds").replace("+00:00", "Z")


def _string_or_empty(value: object | None) -> str:
    """Нормализовать отсутствующую строку в пустую строку."""
    return "" if value is None else str(value)


def _integer_or_zero(value: int | None) -> int:
    """Нормализовать отсутствующее целое число в ноль."""
    return 0 if value is None else int(value)
