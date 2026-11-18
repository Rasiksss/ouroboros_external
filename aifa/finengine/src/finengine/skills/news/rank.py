"""Step 3: pure-code ranking before LLM classification.

We compute a score per item so the LLM batch only sees the top N candidates
(saves tokens). Formula is intentionally simple — all factors are
explainable and tunable in one place.

    score = source_weight × freshness_decay × topic_priority × novelty

Factors:
  source_weight   from sources.yaml (0.5..1.0)
  freshness_decay exp(-hours_old / TAU); TAU=18 → news older than 24h
                  worth ~25% of fresh news
  topic_priority  1.0 + 0.3 per matched regulator/watchlist entity,
                  1.0 + 0.15 per matched macro topic
  novelty         drop 0.5 if title-trigram-overlap >70% with an item
                  already published in any digest_runs in last 7 days
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from finengine.skills.news.models import NewsItem

log = logging.getLogger(__name__)


_TAU_HOURS = 18.0


def _freshness(item: NewsItem, *, now: datetime) -> float:
    delta_h = (now - item.published_at).total_seconds() / 3600.0
    if delta_h < 0:
        # future-dated, weird but possible — treat as fresh
        return 1.0
    # E-disclosure publications are corporate disclosures by law. They
    # arrive once a week or so per company, never breaking-news cadence.
    # The 18-hour TAU optimised for newswire freshness would push them
    # below the rank cutoff within a day, so they'd never reach the LLM
    # classifier. Stretch the half-life to 5 days (TAU=120) for these.
    tau = _TAU_HOURS
    if item.source_id.startswith("edisclosure_"):
        tau = 120.0
    return math.exp(-delta_h / tau)


def _topic_priority(item: NewsItem) -> float:
    """Bonus for matching watchlist / regulator / macro entities."""
    entity_count = len(item.matched_entities)
    topic_count = len(item.matched_topics)
    return 1.0 + 0.3 * entity_count + 0.15 * topic_count


def score(item: NewsItem, *, now: datetime | None = None) -> float:
    n = now or datetime.now(timezone.utc)
    return item.source_weight * _freshness(item, now=n) * _topic_priority(item)


def rank(items: list[NewsItem]) -> list[NewsItem]:
    """Return items sorted by score descending. Does NOT truncate —
    pipeline decides how many to forward to the LLM."""
    now = datetime.now(timezone.utc)
    items.sort(key=lambda i: score(i, now=now), reverse=True)
    return items


def top_n(
    items: list[NewsItem],
    n: int = 30,
    *,
    official_quota: int | None = None,
) -> list[NewsItem]:
    """Pick top-N candidates with stratification: reserve a quota of
    slots for `source_type == "official"` items so regulator and macro
    feeds (ЦБ РФ, ФРС, ЕЦБ, BEA, BLS, EIA, SEC) aren't drowned out by
    the much larger pool of corporate news from Google News proxies.

    Default quota: 25% of n. With n=60 → 15 official slots reserved.
    If we have fewer official items than the quota, the unused slots
    spill back to the general pool.
    """
    if official_quota is None:
        official_quota = max(1, n // 4)

    ranked = rank(items)
    official = [it for it in ranked if it.source_type == "official"]
    non_official = [it for it in ranked if it.source_type != "official"]

    picked_official = official[:official_quota]
    remaining_slots = n - len(picked_official)
    picked_non_official = non_official[:remaining_slots]
    # Re-sort the combined set by score so the LLM sees them in
    # importance order (visual / context cues for the model).
    combined = picked_official + picked_non_official
    now = datetime.now(timezone.utc)
    combined.sort(key=lambda i: score(i, now=now), reverse=True)
    return combined
