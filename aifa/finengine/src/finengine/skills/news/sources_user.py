"""Per-user subscription overlay on top of the global sources.yaml.

Conceptual model
----------------
Two layers:

1. **Default catalogue** — `sources.yaml` shipped in code. Every user
   sees these out of the box (РБК, Vedomosti, Reuters, ЦБ РФ, …).
2. **User overlay** — `data/users/<id>/sources.json`. Records the
   user's deltas against the default catalogue:
     - `added`  — sources the user pasted into the sidebar
     - `disabled` — default ids the user toggled off

The resolved view a pipeline run sees:

    resolved = (defaults - disabled) ∪ added

We keep added and disabled as separate lists rather than persisting
the full resolved view because (a) the default catalogue evolves (we
keep adding sources to `sources.yaml`) and the user shouldn't have to
re-opt-in, and (b) "the user explicitly said remove this" is its own
intent we want to preserve even if the source happens to also be
disabled in the default for some other reason.

Schema (sources.json):

    {
      "version": 1,
      "disabled": ["techcrunch", "seeking_alpha"],
      "added": [
        {
          "id": "user_<hash>",
          "url": "https://example.com/feed.rss",
          "connector": "rss",
          "display_name": "Example Feed",
          "type": "wire",
          "region": "global",
          "weight": 0.7,
          "added_at": "2026-06-07T15:20:00Z"
        },
        ...
      ]
    }
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from finengine.store import state

log = logging.getLogger(__name__)

# Schema version — bump when the on-disk format changes incompatibly.
_SCHEMA_VERSION = 1


# ─── Read / write ──────────────────────────────────────────────────────


def _empty() -> dict:
    return {"version": _SCHEMA_VERSION, "disabled": [], "added": []}


def load(user_id: int) -> dict:
    """Return the user's overlay. Always returns a dict in the current
    schema even if the file is missing/corrupt — the pipeline never
    has to special-case a fresh user."""
    raw = state.get(state.USER_STATE, owner=int(user_id), key="sources") or _empty()
    if not isinstance(raw, dict):
        return _empty()
    # Fill in missing keys so caller can index without get()s.
    raw.setdefault("version", _SCHEMA_VERSION)
    raw.setdefault("disabled", [])
    raw.setdefault("added", [])
    return raw


def save(user_id: int, overlay: dict) -> bool:
    overlay.setdefault("version", _SCHEMA_VERSION)
    state.put(state.USER_STATE, owner=int(user_id), key="sources", doc=overlay)
    return True


# ─── Mutations ─────────────────────────────────────────────────────────


def disable(user_id: int, source_id: str) -> bool:
    """Turn off a default-catalogue source for this user."""
    o = load(user_id)
    if source_id not in o["disabled"]:
        o["disabled"].append(source_id)
        return save(user_id, o)
    return True


def enable(user_id: int, source_id: str) -> bool:
    """Undo a previous disable. No-op if it wasn't disabled."""
    o = load(user_id)
    if source_id in o["disabled"]:
        o["disabled"].remove(source_id)
        return save(user_id, o)
    return True


def add(
    user_id: int,
    *,
    url: str,
    connector: str = "rss",
    display_name: str | None = None,
    type: str = "wire",
    region: str = "global",
    weight: float = 0.7,
) -> dict:
    """Append a user-added source. Returns the source dict that was
    stored (with assigned `id`). Idempotent — adding the same URL
    twice returns the existing entry instead of creating a duplicate."""
    o = load(user_id)
    # Stable id derived from URL — keeps subsequent add() calls
    # idempotent even after frontend redraws.
    sid = "user_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    for existing in o["added"]:
        if existing.get("id") == sid:
            return existing
    src = {
        "id": sid,
        "url": url,
        "connector": connector,
        "display_name": display_name or _slug_from_url(url),
        "type": type,
        "region": region,
        "weight": weight,
        "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    o["added"].append(src)
    save(user_id, o)
    return src


def remove(user_id: int, source_id: str) -> bool:
    """Delete a user-added source. No-op if it wasn't there. Use
    `disable` for default-catalogue ids — `remove` only acts on the
    user's own `added` list."""
    o = load(user_id)
    before = len(o["added"])
    o["added"] = [s for s in o["added"] if s.get("id") != source_id]
    if len(o["added"]) == before:
        return True
    return save(user_id, o)


# ─── Helpers ───────────────────────────────────────────────────────────


def _slug_from_url(url: str) -> str:
    """Strip a URL down to its hostname for a default display name.
    `https://www.bloomberg.com/feed/news.rss` → `bloomberg.com`."""
    from urllib.parse import urlparse
    host = urlparse(url).hostname or url
    if host.startswith("www."):
        host = host[4:]
    return host


# ─── Resolution: default catalogue + user overlay → pipeline view ──────


def resolve_sources(
    user_id: int | None,
    default_sources: list,
) -> list:
    """Apply the user's overlay onto the default catalogue and return
    the list of `SourceConfig`-shaped entries the pipeline should
    actually fetch.

    `default_sources` comes from `finengine.skills.news.config.load()`'s
    `sources` field (already includes per-company virtual sources for
    EDGAR + e-disclosure + Google News).

    `user_id=None` means "anonymous request" — no overlay applied; the
    pipeline sees the default catalogue verbatim. This keeps the
    background daily-trigger scheduler (which has no user context)
    working as before.
    """
    if not user_id or user_id <= 0:
        return list(default_sources)

    try:
        overlay = load(int(user_id))
    except Exception:  # noqa: BLE001
        log.warning("sources_user: overlay load failed for user=%s; "
                    "falling back to defaults", user_id, exc_info=True)
        return list(default_sources)

    disabled = set(overlay.get("disabled") or [])
    base = [s for s in default_sources if getattr(s, "id", None) not in disabled]

    # User-added sources are joined as plain dicts; we convert to the
    # SourceConfig dataclass at the call site (sources_user doesn't
    # import the dataclass to avoid a circular import).
    from finengine.skills.news.config import SourceConfig
    for entry in overlay.get("added") or []:
        try:
            base.append(SourceConfig(
                id=entry["id"],
                display_name=entry.get("display_name") or entry["id"],
                type=entry.get("type", "wire"),
                region=entry.get("region", "global"),
                connector=entry.get("connector", "rss"),
                url=entry["url"],
                weight=float(entry.get("weight", 0.7)),
                active=True,
            ))
        except (KeyError, ValueError) as e:
            log.warning("sources_user: skipping malformed added source: %s", e)
            continue
    return base


def list_for_user(user_id: int, default_sources: list) -> list[dict]:
    """UI-shaped view: for each default-catalogue source, indicate
    whether the user has disabled it; then append the user's own
    added sources. Suitable for the Sources Panel sidebar."""
    overlay = load(user_id)
    disabled = set(overlay.get("disabled") or [])
    out: list[dict] = []
    for s in default_sources:
        sid = getattr(s, "id", None)
        if not sid:
            continue
        out.append({
            "id": sid,
            "display_name": s.display_name,
            "type": s.type,
            "region": s.region,
            "connector": s.connector,
            "url": s.url,
            "weight": s.weight,
            "active": (sid not in disabled) and bool(getattr(s, "active", True)),
            "kind": "default",
        })
    for entry in overlay.get("added") or []:
        out.append({**entry, "active": True, "kind": "user"})
    return out
