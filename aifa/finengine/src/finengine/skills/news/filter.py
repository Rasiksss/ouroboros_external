"""Step 2: rule-based filtering.

Tag each NewsItem with matched entities/topics. Drop items that match
nothing at all (those are noise — generic news with no IR relevance).

Matching is case-insensitive substring on title+snippet. We use the
canonical+aliases lists from sources.yaml. Not fuzzy — exact alias.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

from finengine.skills.news.config import EntityConfig, NewsConfig
from finengine.skills.news.models import NewsItem

log = logging.getLogger(__name__)


def _build_alias_matcher(entities: Iterable[EntityConfig]) -> list[tuple[re.Pattern, str, str]]:
    """Compile one regex per (canonical, alias) pair. Word-boundary on
    Cyrillic doesn't work with `\b` reliably — use lookarounds for
    non-word chars on either side, falling back to start/end-of-string."""
    out: list[tuple[re.Pattern, str, str]] = []
    for e in entities:
        for alias in e.aliases:
            # Escape regex special chars in alias, then add boundaries.
            esc = re.escape(alias)
            # Letters/digits should be considered "word" chars in both ASCII
            # and Cyrillic. Use lookbehind/ahead for non-letter context.
            pat = re.compile(
                rf"(?:(?<=^)|(?<=[^\wЀ-ӿ])){esc}(?:(?=[^\wЀ-ӿ])|(?=$))",
                re.IGNORECASE | re.UNICODE,
            )
            out.append((pat, e.canonical, e.kind))
    return out


def annotate(items: list[NewsItem], cfg: NewsConfig) -> list[NewsItem]:
    """Populate matched_entities + matched_topics. Mutates in place
    and returns the same list for chain-ability.

    Connectors that already know the issuer (e.g. EDisclosure pre-sets
    `matched_entities=["Сбер"]` because disclosure titles never embed
    the company name) get their hints preserved — we union, not
    overwrite — so the ranker's topic_priority boost still fires.
    """
    matchers = _build_alias_matcher(cfg.entities)
    for it in items:
        hay = f"{it.title} {it.snippet}"
        entities: set[str] = set(it.matched_entities or ())
        topics: set[str] = set(it.matched_topics or ())
        for pat, canonical, kind in matchers:
            if pat.search(hay):
                if kind == "macro_topic":
                    topics.add(canonical)
                else:
                    # watchlist_ru/world + regulator → entities
                    entities.add(canonical)
        it.matched_entities = sorted(entities)
        it.matched_topics = sorted(topics)
    return items


# Marketing / non-IR keywords. An item with a watchlist mention but a
# title that ONLY mentions one of these = marketing/PR fluff, not
# investor-relevant. We drop it before sending to LLM. Keep this list
# tight — false positives are worse than letting an extra ad through.
_MARKETING_PATTERNS = [
    re.compile(r"\bоткры(л|вает|тие)\s+(новое\s+)?отделени", re.IGNORECASE),
    re.compile(r"\bзапусти(л|ла|ло|вает)\s+(новое\s+)?приложени", re.IGNORECASE),
    re.compile(r"\bпровел(а|и)?\s+(благотворитель|мероприят|форум|конференц)", re.IGNORECASE),
    re.compile(r"\bпоздравил(а|и)?\s+с\s+(праздник|днём|годом)", re.IGNORECASE),
    re.compile(r"\bакци(я|и)?\s+(для\s+клиент|по\s+карт)", re.IGNORECASE),
    re.compile(r"\bпринял\s+участие\s+в", re.IGNORECASE),
    re.compile(r"\bподдержа(л|ла)?\s+(благотворитель|социальн|молодёжн)", re.IGNORECASE),
    re.compile(r"\bоткр(ыл|ыла)\s+(центр|клуб|выставк)", re.IGNORECASE),
]


def _is_marketing(item: NewsItem) -> bool:
    """True if the item's title looks like PR/marketing, not IR-material.
    Only applied to non-official sources — official press releases stay."""
    body = item.title
    return any(p.search(body) for p in _MARKETING_PATTERNS)


def filter_relevant(items: list[NewsItem], cfg: NewsConfig) -> list[NewsItem]:
    """Drop items with no matched entities/topics OR matched only by a
    trivial term. Items from `official` sources are always kept (their
    raw signal is high enough to be worth showing).

    Also drop marketing/PR fluff that happens to mention a watchlist
    company (e.g. «Сбер открыл новое отделение в Уфе» matches the entity
    filter but is noise for an IR digest).
    """
    out: list[NewsItem] = []
    marketing_dropped = 0
    for it in items:
        if it.source_type == "official":
            out.append(it)
            continue
        if not (it.matched_entities or it.matched_topics):
            continue
        if _is_marketing(it):
            marketing_dropped += 1
            continue
        out.append(it)
    log.info("filter: %d → %d items (kept %.0f%%, marketing dropped %d)",
             len(items), len(out),
             100 * len(out) / max(1, len(items)),
             marketing_dropped)
    return out


_DEDUP_STRIP_RE = re.compile(
    r"[^\wЀ-ӿ\s]|\d+",  # punctuation and digits — both noisy across SMI rewrites
    re.UNICODE,
)
# Verbose connector words that don't carry semantic weight for "same story?"
_DEDUP_STOPWORDS = {
    "в", "на", "за", "по", "и", "до", "от", "об", "о", "из", "для",
    "что", "это", "не", "ни", "же", "ли", "бы", "был", "была", "было",
    "году", "год", "годом", "годе", "размере", "сумме",
    "акцию", "акции", "акций", "акция", "руб", "рубля", "рублей",
    "может", "могут", "будет", "будут", "после", "ранее", "также", "более",
    "the", "a", "an", "of", "to", "in", "on", "at", "for", "by", "with",
}


def _normalize_for_dedup(s: str) -> str:
    """Aggressive normalization: lowercase, strip punctuation+digits, drop
    stopwords. Keeps only meaningful tokens. Removes 'размере 2.71 руб /
    акция за 2025 год' noise that breaks character-trigram matching across
    differently-worded versions of the same story."""
    if not s:
        return ""
    s = _DEDUP_STRIP_RE.sub(" ", s.lower())
    tokens = [t for t in s.split() if t and t not in _DEDUP_STOPWORDS]
    return " ".join(tokens)


def _trigrams(s: str) -> set[str]:
    """Character 3-grams over normalized title — robust to small typos
    and minor wording variations across SMI re-syndications."""
    s = _normalize_for_dedup(s)
    if len(s) < 3:
        return set()
    return {s[i:i + 3] for i in range(len(s) - 2)}


def dedup_near_duplicates(
    items: list[NewsItem],
    *,
    threshold: float = 0.35,
) -> list[NewsItem]:
    """Collapse same-story items across SMI by title-trigram Jaccard
    PLUS entity-overlap rule (same set of matched_entities → almost
    certainly the same story even if titles diverge).

    Google News proxy multiplies a single hot story across 15+ outlets
    with slightly different headlines; pure trigram Jaccard misses
    many of those. Adding the entity-overlap fast path catches them.

    Among collapsed duplicates, keep the one with highest source_weight
    (official > tier1 > wire > wire_tg > aggregator).
    """
    if not items:
        return []
    by_priority = sorted(items, key=lambda i: -i.source_weight)
    kept: list[NewsItem] = []
    kept_grams: list[set[str]] = []
    kept_entities: list[frozenset[str]] = []
    dups = 0
    for it in by_priority:
        g = _trigrams(it.title)
        ent = frozenset(it.matched_entities)
        is_dup = False
        for prev_g, prev_ent in zip(kept_grams, kept_entities):
            # Entity-overlap fast path: ≥2 entities in common AND at
            # least one trigram-similar — strong signal of same story.
            if ent and prev_ent:
                common = ent & prev_ent
                if len(common) >= 2:
                    # Need at least mild title similarity (avoid collapsing
                    # 2 different stories that both mention same 2 cos).
                    if g and prev_g:
                        union = g | prev_g
                        jacc = len(g & prev_g) / len(union) if union else 0.0
                        if jacc >= 0.20:
                            is_dup = True
                            break
                # One entity in common + meaningful trigram overlap
                if len(common) >= 1 and g and prev_g:
                    union = g | prev_g
                    jacc = len(g & prev_g) / len(union) if union else 0.0
                    if jacc >= threshold:
                        is_dup = True
                        break
            elif g and prev_g:
                # No entity match → standard trigram-only threshold
                union = g | prev_g
                jacc = len(g & prev_g) / len(union) if union else 0.0
                if jacc >= threshold:
                    is_dup = True
                    break
        if is_dup:
            dups += 1
            continue
        kept.append(it)
        kept_grams.append(g)
        kept_entities.append(ent)
    log.info("dedup: %d → %d items (%d near-duplicates removed)",
             len(items), len(kept), dups)
    return kept
