"""Period detection, normalization, ordering."""

from __future__ import annotations

from finengine.skills.bulk_extractor import _select_run_periods

__all__ = ["select_run_periods"]


select_run_periods = _select_run_periods
