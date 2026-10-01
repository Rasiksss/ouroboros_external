"""Navigator BI transport helpers for navigator_bi_search."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from . import constants as catalog

__all__ = [
    "NavigatorApiError",
    "NavigatorConfig",
    "NavigatorDeadline",
    "build_config",
    "configured_model_ids",
    "httpx",
    "post_dataset",
    "post_structure",
    "redact",
    "total_timeout",
]

_IDENTITY_ERROR = "Navigator identity is not configured. Set NAVIGATOR_BI_SBER_PDI and/or NAVIGATOR_BI_EMAIL."
_MODEL_IDS_ERROR = "Set NAVIGATOR_BI_MODEL_IDS to a comma-separated list of Navigator nID values."


@dataclass(frozen=True)
class NavigatorConfig:
    """Skill-local settings snapshot, built from PluginAPI.get_settings values.

    Decouples the payload from the core AppSettings schema: the plugin reads the
    allowlisted NAVIGATOR_BI_* keys via get_settings (which unwraps SecretStr, so
    sber_pdi arrives as plaintext) and builds this frozen config.
    """

    base_url: str
    sber_pdi: str
    email: str
    model_ids: str
    timeout_sec: float | None
    total_timeout_sec: float | None


def _coerce_timeout(field: str, value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is not a number") from exc


def build_config(values: dict[str, Any]) -> NavigatorConfig:
    """Build a NavigatorConfig from the get_settings values dict.

    Keys are the full NAVIGATOR_BI_* names. The two timeouts are coerced with
    float() (None/"" -> None); a non-parseable timeout raises ValueError whose
    message names the offending field so the plugin can surface it (field names
    only, never values).
    """
    return NavigatorConfig(
        base_url=str(values.get("NAVIGATOR_BI_BASE_URL") or ""),
        sber_pdi=str(values.get("NAVIGATOR_BI_SBER_PDI") or ""),
        email=str(values.get("NAVIGATOR_BI_EMAIL") or ""),
        model_ids=str(values.get("NAVIGATOR_BI_MODEL_IDS") or ""),
        timeout_sec=_coerce_timeout("NAVIGATOR_BI_TIMEOUT_SEC", values.get("NAVIGATOR_BI_TIMEOUT_SEC")),
        total_timeout_sec=_coerce_timeout(
            "NAVIGATOR_BI_TOTAL_TIMEOUT_SEC", values.get("NAVIGATOR_BI_TOTAL_TIMEOUT_SEC")
        ),
    )


class NavigatorApiError(RuntimeError):
    pass


class NavigatorDeadline:
    def __init__(self, total_timeout_sec: float) -> None:
        import time

        self.total_timeout_sec = max(0.001, float(total_timeout_sec))
        self._deadline = time.monotonic() + self.total_timeout_sec

    def error_message(self) -> str:
        return f"Navigator BI total timeout exceeded ({self.total_timeout_sec:g}s)."

    def remaining(self) -> float:
        import time

        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise NavigatorApiError(self.error_message())
        return remaining

    def request_timeout(self, settings: NavigatorConfig) -> float:
        return max(0.001, min(timeout(settings), self.remaining()))


def base_url(settings: NavigatorConfig) -> str:
    return (settings.base_url or catalog.DEFAULT_BASE_URL).rstrip("/")


def sber_pdi(settings: NavigatorConfig) -> str:
    return (settings.sber_pdi or "").strip()


def email(settings: NavigatorConfig) -> str:
    return (settings.email or "").strip()


def _identity(settings: NavigatorConfig) -> tuple[str, str]:
    active_sber_pdi = sber_pdi(settings)
    active_email = email(settings)
    if not active_sber_pdi and not active_email:
        raise NavigatorApiError(_IDENTITY_ERROR)
    return active_sber_pdi, active_email


def timeout(settings: NavigatorConfig) -> float:
    try:
        parsed = float(settings.timeout_sec or catalog.DEFAULT_TIMEOUT_SEC)
    except (TypeError, ValueError):
        return catalog.DEFAULT_TIMEOUT_SEC
    return parsed if parsed > 0 else catalog.DEFAULT_TIMEOUT_SEC


def total_timeout(settings: NavigatorConfig) -> float:
    try:
        parsed = float(settings.total_timeout_sec or catalog.DEFAULT_TOTAL_TIMEOUT_SEC)
    except (TypeError, ValueError):
        return catalog.DEFAULT_TOTAL_TIMEOUT_SEC
    return parsed if parsed > 0 else catalog.DEFAULT_TOTAL_TIMEOUT_SEC


def configured_model_ids(settings: NavigatorConfig) -> list[int]:
    raw = str(settings.model_ids or "").strip()
    if not raw:
        raise NavigatorApiError(_MODEL_IDS_ERROR)

    seen: set[int] = set()
    model_ids: list[int] = []
    for token in raw.split(","):
        text = token.strip()
        if not text:
            continue
        try:
            n_id = int(text)
        except ValueError:
            continue
        if n_id in seen:
            continue
        seen.add(n_id)
        model_ids.append(n_id)

    if not model_ids:
        raise NavigatorApiError(_MODEL_IDS_ERROR)
    return model_ids


def _navigator_headers(active_sber_pdi: str, active_email: str) -> dict[str, str]:
    headers = {
        "datapreview": "1",
        "Nav-Client-Platform": "3",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if active_sber_pdi:
        headers["sberpdi"] = active_sber_pdi
    if active_email:
        headers["email"] = active_email
    return headers


def redact(settings: NavigatorConfig, text: str) -> str:
    redacted = re.sub(r"sberpdi:\s*\S+", "sberpdi: [REDACTED]", str(text), flags=re.IGNORECASE)
    redacted = re.sub(r"email:\s*\S+", "email: [REDACTED]", redacted, flags=re.IGNORECASE)
    redacted = re.sub(r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer [REDACTED]", redacted, flags=re.IGNORECASE)
    active_sber_pdi = sber_pdi(settings)
    if active_sber_pdi:
        redacted = redacted.replace(active_sber_pdi, "[REDACTED]")
    active_email = email(settings)
    if active_email:
        redacted = redacted.replace(active_email, "[REDACTED]")
    return redacted


def _post(
    settings: NavigatorConfig,
    path: str,
    body: dict[str, Any],
    *,
    deadline: NavigatorDeadline | None = None,
) -> dict[str, Any]:
    active_sber_pdi, active_email = _identity(settings)
    url = f"{base_url(settings)}{path}"
    request_timeout = deadline.request_timeout(settings) if deadline else timeout(settings)
    try:
        with httpx.Client(
            timeout=request_timeout,
            headers=_navigator_headers(active_sber_pdi, active_email),
        ) as client:
            response = client.post(url, json=body)
            response.raise_for_status()
            payload = response.json()
        if deadline:
            deadline.remaining()
    except NavigatorApiError:
        raise
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in {401, 403}:
            raise NavigatorApiError("Navigator access denied.") from exc
        raise NavigatorApiError(redact(settings, str(exc))) from exc
    except Exception as exc:
        raise NavigatorApiError(redact(settings, str(exc))) from exc
    if not isinstance(payload, dict):
        raise NavigatorApiError("Navigator response is not a JSON object.")
    return payload


def post_structure(
    settings: NavigatorConfig,
    model_ids: list[int],
    *,
    deadline: NavigatorDeadline | None = None,
) -> dict[str, Any]:
    return _post(
        settings,
        catalog.STRUCTURE_PATH,
        {"model_ids": [str(model_id) for model_id in model_ids]},
        deadline=deadline,
    )


def post_dataset(
    settings: NavigatorConfig,
    body: dict[str, Any],
    *,
    deadline: NavigatorDeadline | None = None,
) -> dict[str, Any]:
    return _post(settings, catalog.DATASET_PATH, body, deadline=deadline)
