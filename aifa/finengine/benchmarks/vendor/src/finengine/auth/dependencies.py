"""FastAPI dependencies: get_current_user, require_admin."""
from __future__ import annotations
from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.orm import Session

from finengine.auth.db import SessionLocal, User, get_session
from finengine.auth.security import decode_token
from finengine.config import settings


def _cookie_alias():
    """Build the Cookie alias dynamically so the configured cookie name is used."""
    return Cookie(default=None, alias=settings.auth_cookie_name)


def get_current_user(
    session: Session = Depends(get_session),
    token: str | None = _cookie_alias(),
) -> User:
    """Resolve the logged-in user from the session cookie. 401 otherwise."""
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    user_id = decode_token(token)
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def get_current_user_detached(token: str | None = _cookie_alias()) -> User:
    """Resolve the logged-in user and close the DB session immediately.

    Long-lived StreamingResponse endpoints otherwise keep FastAPI dependency
    sessions open until SSE disconnects. A handful of parallel extraction
    streams can exhaust the SQLite connection pool before any LLM work starts.
    """
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    user_id = decode_token(token)
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")

    with SessionLocal() as session:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
        # Touch the fields used by streaming handlers before detaching the ORM
        # object. SessionLocal has expire_on_commit=False, but being explicit
        # makes the dependency safe if the session settings ever change.
        _ = (
            user.id,
            user.username,
            user.is_admin,
            user.allowed_models,
            user.role,
        )
        session.expunge(user)
        return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user
