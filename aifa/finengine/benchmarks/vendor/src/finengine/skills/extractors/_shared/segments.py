"""Segment scope constants — re-exports + helpers for cells.

The segment-breakdown LLM stage extracts operating segments / geography /
products only for a small set of "headline" metrics. Different reporting
standards have different conventional headline metrics (IFRS commonly
discloses EBITDA + finance costs; US GAAP P&L has GROSS_PROFIT + a
dedicated OPERATING_INCOME line).

Cells pick which set they want by reading these re-exports and pinning
`SEGMENT_METRICS` in the cell module. When a cell wants to diverge from
the legacy buckets, it stops importing from here.
"""

from __future__ import annotations

from finengine.skills.bulk_extractor import (
    _SEGMENT_METRICS_IFRS_RU,
    _SEGMENT_METRICS_US_GAAP_EN,
)

__all__ = [
    "SEGMENT_METRICS_IFRS_RU",
    "SEGMENT_METRICS_US_GAAP_EN",
    "segment_metric_scope",
]


SEGMENT_METRICS_IFRS_RU = _SEGMENT_METRICS_IFRS_RU
SEGMENT_METRICS_US_GAAP_EN = _SEGMENT_METRICS_US_GAAP_EN


def segment_metric_scope(metrics: list[dict], wanted: set[str]) -> list[dict]:
    """Filter a metric registry to the rows whose enum is in `wanted`.

    Cell version of bulk_extractor._segment_metric_scope — takes the
    target set explicitly instead of switching on a `standard` string.
    """
    return [m for m in metrics if m.get("enum") in wanted]
