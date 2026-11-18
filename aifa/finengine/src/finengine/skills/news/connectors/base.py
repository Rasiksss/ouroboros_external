"""Connector protocol — what every source-type wrapper implements."""
from __future__ import annotations

import logging
from typing import Protocol

from finengine.skills.news.config import SourceConfig
from finengine.skills.news.models import NewsItem

log = logging.getLogger(__name__)


class Connector(Protocol):
    """One connector per active source. Implementations should be cheap
    to construct — they're created per pipeline run from sources.yaml."""

    source: SourceConfig

    def fetch(self, *, since_hours: int = 48, limit: int = 50) -> list[NewsItem]:
        """Return a list of normalized items published within the last
        `since_hours`. May return fewer than `limit` (network error,
        feed empty, etc.) — never raises into the pipeline."""
        ...
