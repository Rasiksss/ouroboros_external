"""Per-user demo spend cap.

Invite/demo accounts get a USD budget (``User.spend_limit_usd``, default $5).
Every LLM call adds its cost to ``User.spent_usd`` (hooked in the tracer, which
is the single point all calls pass through), and billed endpoints call
``enforce_spend_budget()`` at the top — returning HTTP 402 once the cap is hit.

Admins, and any account whose limit is <= 0, are unlimited. Recording is
best-effort: a billing failure must never break an LLM call or a request.
"""
from __future__ import annotations

from fastapi import HTTPException


def enforce_spend_budget(user) -> None:
    """Raise HTTP 402 when a non-admin account has reached its spend cap.

    Called at the start of billed endpoints (extract / chat / quick / chains / insights).
    """
    if user is None or getattr(user, "is_admin", False):
        return
    limit = float(getattr(user, "spend_limit_usd", 0.0) or 0.0)
    if limit <= 0:
        return  # 0 / unset == unlimited
    spent = float(getattr(user, "spent_usd", 0.0) or 0.0)
    if spent >= limit:
        raise HTTPException(
            status_code=402,
            detail=(
                f"Демо-лимит ${limit:.2f} исчерпан (потрачено ${spent:.2f}). "
                "Обратитесь к администратору, чтобы увеличить лимит."
            ),
        )


def record_spend(
    user_id: int | None,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cached_tokens: int = 0,
) -> None:
    """Add one LLM call's cost to the user's running spend. Best-effort —
    swallows every error so it can never break the LLM path."""
    if not user_id:
        return
    try:
        from finengine.skills.pricing import cost_usd

        cost = cost_usd(
            model,
            int(tokens_in or 0),
            int(tokens_out or 0),
            int(cached_tokens or 0),
        )
        if not cost or cost <= 0:
            return
        from finengine.auth.db import SessionLocal, User

        session = SessionLocal()
        try:
            u = session.get(User, int(user_id))
            if u is not None:
                u.spent_usd = float(u.spent_usd or 0.0) + float(cost)
                session.commit()
        finally:
            session.close()
    except Exception:
        pass
