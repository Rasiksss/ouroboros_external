"""Minimal in-process Prometheus-style metrics output."""

from __future__ import annotations

from collections import Counter
from threading import Lock
from time import monotonic

from app.core.config import settings

_started_at = monotonic()
_request_totals: Counter[tuple[str, str, int]] = Counter()
_request_duration_totals: Counter[tuple[str, str]] = Counter()
_metrics_lock = Lock()


def observe_request(method: str, path: str, status_code: int, duration_sec: float) -> None:
    if not settings.METRICS_ENABLED:
        return
    with _metrics_lock:
        _request_totals[(method, path, status_code)] += 1
        _request_duration_totals[(method, path)] += duration_sec


def render_metrics() -> str:
    if not settings.METRICS_ENABLED:
        return ""

    lines = [
        "# HELP workstation_service_info Static service metadata.",
        "# TYPE workstation_service_info gauge",
        f'workstation_service_info{{name="{settings.SERVICE_NAME}",version="{settings.APP_VERSION}"}} 1',
        "# HELP workstation_uptime_seconds Service uptime in seconds.",
        "# TYPE workstation_uptime_seconds gauge",
        f"workstation_uptime_seconds {monotonic() - _started_at:.6f}",
        "# HELP workstation_http_requests_total Total number of HTTP requests.",
        "# TYPE workstation_http_requests_total counter",
    ]

    with _metrics_lock:
        for (method, path, status_code), total in sorted(_request_totals.items()):
            lines.append(
                f'workstation_http_requests_total{{method="{method}",path="{path}",status="{status_code}"}} {total}'
            )

        lines.extend(
            [
                "# HELP workstation_http_request_duration_seconds_total Total request duration by route.",
                "# TYPE workstation_http_request_duration_seconds_total counter",
            ]
        )
        for (method, path), total in sorted(_request_duration_totals.items()):
            lines.append(
                f'workstation_http_request_duration_seconds_total{{method="{method}",path="{path}"}} {total:.6f}'
            )

    return "\n".join(lines) + "\n"
