"""Auth endpoints. Mounted at /auth/* in main.py."""
from __future__ import annotations
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from finengine.auth.db import Invite, User, get_session
from finengine.auth.dependencies import get_current_user, require_admin
from finengine.auth.schemas import (
    CreateInviteIn,
    InviteOut,
    LoginIn,
    RegisterIn,
    UserOut,
)
from finengine.auth.security import create_access_token, hash_password, verify_password
from finengine.config import settings

router = APIRouter(prefix="/auth", tags=["auth"])

# Whitelist of premium model keys an admin can grant via invite.
# Keys match `key` in web/src/lib/prefs.ts MODEL_OPTIONS.
_PREMIUM_MODEL_KEYS = {"kimi", "glm"}


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        max_age=settings.auth_token_ttl_days * 24 * 3600,
        httponly=True,
        secure=True,        # only over https — in dev http this is dropped silently
        samesite="lax",
        path="/",
    )


def _clear_cookie(response: Response) -> None:
    response.delete_cookie(key=settings.auth_cookie_name, path="/")


def _generate_invite_code() -> str:
    # 16 chars, url-safe, ~96 bits of entropy.
    return secrets.token_urlsafe(12)[:16]


@router.post("/register", response_model=UserOut)
def register(
    body: RegisterIn,
    response: Response,
    session: Session = Depends(get_session),
):
    # Validate invite first so we don't leak whether a username exists.
    invite = session.get(Invite, body.invite_code)
    if invite is None:
        raise HTTPException(status_code=400, detail="Invalid invite code")
    if invite.used_by_id is not None:
        raise HTTPException(status_code=400, detail="Invite already used")
    if invite.expires_at is not None:
        # SQLite returns naive datetimes even with DateTime(timezone=True);
        # treat them as UTC so the comparison is well-defined.
        exp = invite.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="Invite expired")

    if session.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(status_code=409, detail="Username already taken")

    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        is_admin=False,
        allowed_models=invite.allowed_models or "",
    )
    session.add(user)
    session.flush()  # populate user.id
    invite.used_by_id = user.id
    invite.used_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(user)

    _set_cookie(response, create_access_token(user.id))
    return UserOut(
        username=user.username,
        is_admin=user.is_admin,
        allowed_models=_parse_allowed(user.allowed_models),
    )


@router.post("/login", response_model=UserOut)
def login(
    body: LoginIn,
    response: Response,
    session: Session = Depends(get_session),
):
    user = session.scalar(select(User).where(User.username == body.username))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    _set_cookie(response, create_access_token(user.id))
    return UserOut(
        username=user.username,
        is_admin=user.is_admin,
        allowed_models=_parse_allowed(user.allowed_models),
    )


@router.post("/logout")
def logout(response: Response):
    _clear_cookie(response)
    return {"status": "ok"}


def _parse_allowed(s: str | None) -> list[str]:
    if not s:
        return []
    return [k.strip() for k in s.split(",") if k.strip()]


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return UserOut(
        username=user.username,
        is_admin=user.is_admin,
        allowed_models=_parse_allowed(user.allowed_models),
    )


# --- Invites (admin only) ----------------------------------------------------

@router.post("/invites", response_model=InviteOut)
def create_invite(
    body: CreateInviteIn,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
):
    code = _generate_invite_code()
    expires_at: Optional[datetime] = None
    if body.expires_in_days:
        expires_at = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)
    # Whitelist incoming model keys against what the UI knows.
    allowed = ",".join(k for k in body.allowed_models if k in _PREMIUM_MODEL_KEYS)
    inv = Invite(
        code=code,
        created_by_id=admin.id,
        expires_at=expires_at,
        allowed_models=allowed,
    )
    session.add(inv)
    session.commit()
    session.refresh(inv)
    return InviteOut(
        code=inv.code,
        created_at=inv.created_at,
        expires_at=inv.expires_at,
        used_by=None,
        allowed_models=_parse_allowed(inv.allowed_models),
    )


@router.get("/invites", response_model=list[InviteOut])
def list_invites(
    _: User = Depends(require_admin),
    session: Session = Depends(get_session),
):
    invs = session.scalars(select(Invite).order_by(Invite.created_at.desc())).all()
    return [
        InviteOut(
            code=i.code,
            created_at=i.created_at,
            expires_at=i.expires_at,
            used_by=i.used_by.username if i.used_by else None,
            allowed_models=_parse_allowed(i.allowed_models),
        )
        for i in invs
    ]


@router.delete("/invites/{code}")
def delete_invite(
    code: str,
    _: User = Depends(require_admin),
    session: Session = Depends(get_session),
):
    inv = session.get(Invite, code)
    if inv is None:
        raise HTTPException(status_code=404, detail="Not found")
    if inv.used_by_id is not None:
        raise HTTPException(status_code=400, detail="Cannot delete used invite")
    session.delete(inv)
    session.commit()
    return {"status": "deleted"}
