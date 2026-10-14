"""DTOs that travel through the news pipeline.

`NewsItem` is the single normalized shape every connector returns.
Pipeline stages (dedup/filter/rank) operate on lists of these.
`ClassifiedItem` is the post-LLM enrichment; `DigestItem` is the
final shape persisted in SQLite and rendered as markdown.
"""
from __future__ import annotations

import dataclasses
import hashlib
from datetime import datetime
from typing import Literal, Optional

Region = Literal["RU", "US", "EU", "CN", "global"]
SourceType = Literal[
    "official",     # ЦБ РФ, ФРС, ЕЦБ, SEC — regulators/companies own sites
    "wire",         # Reuters, Интерфакс, Финмаркет
    "tier1",        # FT, WSJ, РБК, Forbes
    "wire_tg",      # Сигналы РЦБ, MarketTwits, Смартлаб
    "aggregator",   # GDELT, NewsAPI, Tavily fallback
]
Category = Literal["macro", "regulator", "corporate", "unknown"]


@dataclasses.dataclass
class NewsItem:
    """Output of any Connector. The only fields downstream stages rely on."""
    source: str                 # display name, e.g. "ЦБ РФ", "Reuters"
    source_id: str              # stable id from sources.yaml, e.g. "cbr_press"
    source_type: SourceType
    region: Region
    source_weight: float        # 0.0..1.0, from sources.yaml (used by rank)
    title: str
    snippet: str                # 1-3 sentences, NOT full article body
    url: str
    published_at: datetime      # tz-aware UTC
    raw_hash: str               # stable dedup key (see canonical_hash)
    matched_entities: list[str] = dataclasses.field(default_factory=list)
    matched_topics: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class ClassifiedItem:
    """What the LLM batch call returns per candidate. Strict JSON shape."""
    item: NewsItem
    is_relevant: bool
    category: Category
    region: Region              # may differ from NewsItem.region (LLM re-classifies)
    importance: float           # 0.0..1.0
    one_sentence_ru: str        # ≤200 chars summary in Russian
    why_important: str = ""     # short reason, for debug column
    confidence: float = 0.5


@dataclasses.dataclass
class DigestItem:
    """Final ranked & classified item ready for markdown render."""
    classified: ClassifiedItem
    final_score: float          # source × freshness × importance × novelty
    in_long_list: bool          # True → goes into top section, else "Прочие"
    watchlist_match: Optional[str] = None  # company name from watchlist or None


def canonical_hash(url: str, title: str) -> str:
    """Stable dedup hash: canonical URL ∪ normalized title.

    Two different SMI re-syndicating the same Reuters wire produce
    different URLs but identical titles → collide on title hash.
    Same news bot writing tweaked snippets but identical URL → collide
    on URL hash. Combine both: collision = same story.
    """
    # URL canonicalization: drop fragment, lowercase host, strip utm_ params.
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    try:
        s = urlsplit(url)
        q = [(k, v) for k, v in parse_qsl(s.query) if not k.startswith("utm_")]
        canonical = urlunsplit((s.scheme, s.netloc.lower(), s.path.rstrip("/"),
                                urlencode(sorted(q)), ""))
    except ValueError:
        canonical = url

    title_norm = " ".join(title.lower().split())[:200]
    h = hashlib.sha256()
    h.update(canonical.encode("utf-8"))
    h.update(b"|")
    h.update(title_norm.encode("utf-8"))
    return h.hexdigest()[:32]
