"""HTTP API for the Sources Panel + per-chat knowledge base.

Mounted from `api/main.py` with:

    from finengine.api.personal_sources import router as personal_router
    app.include_router(personal_router)

Two related but distinct surfaces, kept on one router because they
share the user_id flow off `get_current_user`:

  /sources …       — user-scoped subscriptions (Daily News overlay)
  /chats/<id>/kb … — per-chat knowledge base (lazy-RAG over pasted URLs)
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from finengine.auth.db import User
from finengine.auth.dependencies import get_current_user
from finengine.skills.news import (
    config as _cfg,
    sources_user as _su,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["personal-sources"])


# ─── Pydantic request/response models ──────────────────────────────────


class AddSourceBody(BaseModel):
    url: str = Field(..., description="Полный URL для подписки.")
    display_name: Optional[str] = None
    region: str = Field(
        "global",
        pattern="^(RU|US|EU|CN|global)$",
    )
    type: str = Field("wire", pattern="^(official|tier1|wire|wire_tg)$")
    connector: str = Field(
        "rss",
        pattern="^(rss|telegram|edgar|html|edisclosure|playwright_rss)$",
    )
    weight: float = Field(0.7, ge=0.0, le=1.0)


# ─── /sources — user's subscription overlay ────────────────────────────


@router.get("/sources")
def list_sources(user: User = Depends(get_current_user)):
    """Combined view: every source the user sees in Daily News, marked
    as `kind=default` or `kind=user`, with `active` reflecting any
    disable they applied."""
    cfg = _cfg.load()
    return {"sources": _su.list_for_user(user.id, cfg.sources)}


@router.post("/sources")
def add_source(body: AddSourceBody, user: User = Depends(get_current_user)):
    """User-added URL. Idempotent on URL — adding the same URL twice
    returns the existing entry's id."""
    src = _su.add(
        user.id,
        url=body.url,
        display_name=body.display_name,
        region=body.region,
        type=body.type,
        connector=body.connector,
        weight=body.weight,
    )
    return {"source": src}


@router.delete("/sources/{source_id}")
def delete_source(source_id: str, user: User = Depends(get_current_user)):
    """Remove a user-added source, OR disable a default-catalogue one
    (we dispatch by id prefix — user-added ids start with `user_`)."""
    if source_id.startswith("user_"):
        ok = _su.remove(user.id, source_id)
    else:
        ok = _su.disable(user.id, source_id)
    if not ok:
        raise HTTPException(500, "delete failed")
    return {"ok": True}


@router.post("/sources/{source_id}/enable")
def enable_source(source_id: str, user: User = Depends(get_current_user)):
    """Re-enable a default source the user previously disabled."""
    if source_id.startswith("user_"):
        # User-added sources are always-on; can't re-enable a deleted one.
        raise HTTPException(
            400,
            "user-added sources are deleted permanently — re-add via POST",
        )
    if not _su.enable(user.id, source_id):
        raise HTTPException(500, "enable failed")
    return {"ok": True}
