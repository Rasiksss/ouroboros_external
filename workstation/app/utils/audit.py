"""Standard deployment audit logger integration."""

from __future__ import annotations

import logging
from typing import Any

from app.utils.logging import redact_log_value


audit_logger = logging.getLogger("aif_audit")
AUDIT_SUCCESS_CODE = "C3_SERVICE_ACTION"
AUDIT_FAIL_CODE = "C4_FAIL_SERVICE_ACTION"
AUDIT_CODES = {AUDIT_SUCCESS_CODE, AUDIT_FAIL_CODE}


def audit_action(
    object_name: str,
    object_id: str,
    params: dict[str, Any] | None = None,
    *,
    code: str = AUDIT_SUCCESS_CODE,
) -> None:
    if code not in AUDIT_CODES:
        raise ValueError(f"Unsupported audit code: {code}")
    audit_logger.info(
        {
            "code": code,
            "params": {
                "object_name": object_name,
                "object_id": object_id,
                "params": redact_log_value(params or {}),
            },
        }
    )
