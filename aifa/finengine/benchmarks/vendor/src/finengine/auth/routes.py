"""Auth endpoints. Mounted at /auth/* in main.py."""
from __future__ import annotations
import json
import logging
import os
import re
import secrets
import smtplib
import time as _time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from finengine.analytics import rollup
from finengine.auth.db import Invite, UsageDaily, User, get_session
from finengine.auth.dependencies import get_current_user, require_admin
from finengine.auth.schemas import (
    CreateInviteIn,
    InviteOut,
    LoginIn,
    RegisterIn,
    UserOut,
)
from pydantic import BaseModel

from finengine.auth.security import create_access_token, hash_password, verify_password
from finengine.config import settings
from finengine.i18n import language_directive, scaffold
from finengine.roles.access import (
    available_roles_for_user,
    normalize_account_roles,
    primary_account_role,
    serialize_account_roles,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# Whitelist of premium model keys an admin can grant via invite.
# Keys match `key` in web/src/lib/prefs.ts MODEL_OPTIONS, plus arch-level
# grants like "consensus" (Triumvirat). When this set grows, mirror it in
# the admin UI's invite-creation form.
_PREMIUM_MODEL_KEYS = {"kimi", "glm", "gigachat", "consensus"}


def _secure_cookie_for_request(request: Request) -> bool:
    if not settings.auth_cookie_secure:
        return False
    host = (request.url.hostname or "").lower()
    if request.url.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}:
        return False
    return True


def _set_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        max_age=settings.auth_token_ttl_days * 24 * 3600,
        httponly=True,
        secure=_secure_cookie_for_request(request),
        samesite="lax",
        path="/",
    )


def _clear_cookie(response: Response) -> None:
    response.delete_cookie(key=settings.auth_cookie_name, path="/")


def _generate_invite_code() -> str:
    # 16 chars, url-safe, ~96 bits of entropy.
    return secrets.token_urlsafe(12)[:16]


def _user_out(user: User) -> UserOut:
    roles = normalize_account_roles(getattr(user, "role", None))
    return UserOut(
        username=user.username,
        is_admin=user.is_admin,
        role=primary_account_role(roles),
        roles=roles,
        available_roles=available_roles_for_user(user),
        allowed_models=_parse_allowed(user.allowed_models),
    )


def _invite_out(inv: Invite) -> InviteOut:
    roles = normalize_account_roles(getattr(inv, "role", None))
    return InviteOut(
        code=inv.code,
        created_at=inv.created_at,
        expires_at=inv.expires_at,
        used_by=inv.used_by.username if inv.used_by else None,
        role=primary_account_role(roles),
        roles=roles,
        allowed_models=_parse_allowed(inv.allowed_models),
        first_name=getattr(inv, "first_name", "") or "",
        last_name=getattr(inv, "last_name", "") or "",
        company=getattr(inv, "company", "") or "",
        email=getattr(inv, "email", "") or "",
    )


@router.post("/register", response_model=UserOut)
def register(
    body: RegisterIn,
    response: Response,
    request: Request,
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
        role=serialize_account_roles(getattr(invite, "role", None)),
        first_name=getattr(invite, "first_name", "") or "",
        last_name=getattr(invite, "last_name", "") or "",
        company=getattr(invite, "company", "") or "",
        email=getattr(invite, "email", "") or "",
    )
    session.add(user)
    session.flush()  # populate user.id
    invite.used_by_id = user.id
    invite.used_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(user)

    _set_cookie(response, request, create_access_token(user.id))
    return _user_out(user)


def _touch_activity(session: Session, user: User, *, login: bool) -> None:
    """Stamp last_login and count session starts for analytics (Phase 2).

    On explicit ``/login`` (login=True) always counts a login. On ``/me``
    (login=False) counts only when the user's previous activity was on an
    earlier UTC day — i.e. a cookie-resumed session on a new day — so daily
    activity is captured even without a re-login. To keep the frequently-hit
    ``/me`` cheap, the whole write is skipped when last_login is recent
    (<5 min) and it isn't a new day. Best-effort: never breaks the request.

    ``last_login`` is always stored as UTC; SQLite may return it naive, so we
    relabel a naive value as UTC before comparing.
    """
    from finengine.auth.db import UsageDaily

    try:
        now = datetime.now(timezone.utc)
        prev = getattr(user, "last_login", None)
        if prev is not None and prev.tzinfo is None:
            prev = prev.replace(tzinfo=timezone.utc)
        new_day = prev is None or prev.date() < now.date()
        if not login and not new_day and prev is not None and (now - prev).total_seconds() < 300:
            return
        user.last_login = now
        if login or new_day:
            user.login_count = int(getattr(user, "login_count", 0) or 0) + 1
            day = now.date().isoformat()
            row = session.get(UsageDaily, (user.id, day))
            if row is None:
                row = UsageDaily(user_id=user.id, day=day)
                session.add(row)
            row.logins = int(row.logins or 0) + 1
        session.commit()
    except Exception:
        try:
            session.rollback()
        except Exception:
            pass


@router.post("/login", response_model=UserOut)
def login(
    body: LoginIn,
    response: Response,
    request: Request,
    session: Session = Depends(get_session),
):
    user = session.scalar(select(User).where(User.username == body.username))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    _set_cookie(response, request, create_access_token(user.id))
    _touch_activity(session, user, login=True)
    return _user_out(user)


@router.post("/logout")
def logout(response: Response):
    _clear_cookie(response)
    return {"status": "ok"}


# ── Access request (public, no auth) ────────────────────────────────────
# /login page has a «Запросить доступ» link → /request-access. The form
# posts here. We always JSONL-log the request (so admin can sort through
# requests via /admin/access-requests). When SMTP env vars are set, we
# also send a notification email to FINENGINE_REQUEST_ACCESS_NOTIFY_TO.
_ACCESS_REQUESTS_PATH = Path(settings.data_dir) / "documents" / "access_requests.jsonl"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_log = logging.getLogger("finengine.access_request")


def _send_access_request_email(payload: dict) -> tuple[bool, str]:
    """Send an admin-notification email. Returns (delivered, status_msg).

    Required env vars (all must be set; otherwise email step is skipped):
      FINENGINE_SMTP_HOST          e.g. "smtp.beget.com"
      FINENGINE_SMTP_PORT          e.g. 465 (SSL) or 587 (STARTTLS)
      FINENGINE_SMTP_USER          mailbox login
      FINENGINE_SMTP_PASSWORD      mailbox password
      FINENGINE_SMTP_FROM          From-address (often == SMTP_USER)
      FINENGINE_REQUEST_ACCESS_NOTIFY_TO   destination (default nero27@mail.ru)
    """
    host = os.environ.get("FINENGINE_SMTP_HOST", "").strip()
    port = int(os.environ.get("FINENGINE_SMTP_PORT", "0") or "0")
    user = os.environ.get("FINENGINE_SMTP_USER", "").strip()
    pwd = os.environ.get("FINENGINE_SMTP_PASSWORD", "")
    sender = os.environ.get("FINENGINE_SMTP_FROM", "").strip() or user
    to_addr_raw = os.environ.get("FINENGINE_REQUEST_ACCESS_NOTIFY_TO", "nero27@mail.ru").strip()
    # Support comma-separated list of recipients (e.g.
    # "nero27@mail.ru,papikulev@sberbank.ru"). Useful when several admins
    # want notifications without setting up a mailing list.
    to_addrs = [a.strip() for a in to_addr_raw.split(",") if a.strip()]
    if not (host and port and user and pwd and sender and to_addrs):
        return False, "smtp_not_configured"
    body_lines = [
        "Новая заявка на доступ к Finengine:",
        "",
        f"Имя:      {payload.get('first_name','')}",
        f"Фамилия:  {payload.get('last_name','')}",
        f"Компания: {payload.get('company','')}",
        f"Email:    {payload.get('email','')}",
        "",
        f"Время:    {payload.get('ts','')}",
        "",
        "Создать инвайт можно в админ-панели на /admin/invites.",
    ]
    msg = MIMEText("\n".join(body_lines), _charset="utf-8")
    msg["Subject"] = f"Finengine: заявка на доступ — {payload.get('company','')}"
    msg["From"] = sender
    msg["To"] = ", ".join(to_addrs)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20) as s:
                s.login(user, pwd)
                s.send_message(msg, to_addrs=to_addrs)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.starttls()
                s.login(user, pwd)
                s.send_message(msg, to_addrs=to_addrs)
        return True, "ok"
    except Exception as e:
        _log.warning("access-request email send failed: %s", e)
        return False, f"smtp_error: {type(e).__name__}"


def _send_invite_email(to_email: str, code: str, first_name: str = "") -> tuple[bool, str]:
    """Email an invite code to the invitee. Returns (delivered, status_msg).
    Reuses the same SMTP env vars as the access-request notification."""
    host = os.environ.get("FINENGINE_SMTP_HOST", "").strip()
    port = int(os.environ.get("FINENGINE_SMTP_PORT", "0") or "0")
    user = os.environ.get("FINENGINE_SMTP_USER", "").strip()
    pwd = os.environ.get("FINENGINE_SMTP_PASSWORD", "")
    sender = os.environ.get("FINENGINE_SMTP_FROM", "").strip() or user
    to_addr = (to_email or "").strip()
    if not (host and port and user and pwd and sender and to_addr):
        return False, "smtp_not_configured"
    base = os.environ.get("FINENGINE_PUBLIC_BASE_URL", "https://aifanalys.ru").rstrip("/")
    greeting = f"Здравствуйте, {first_name}!" if first_name else "Здравствуйте!"
    body_lines = [
        greeting,
        "",
        "Вам открыт доступ к Finengine. Ваш код приглашения:",
        "",
        f"    {code}",
        "",
        f"Регистрация: {base}/register — введите код, придумайте логин и пароль.",
    ]
    msg = MIMEText("\n".join(body_lines), _charset="utf-8")
    msg["Subject"] = "Finengine: ваш код приглашения"
    msg["From"] = sender
    msg["To"] = to_addr
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20) as s:
                s.login(user, pwd)
                s.send_message(msg, to_addrs=[to_addr])
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.starttls()
                s.login(user, pwd)
                s.send_message(msg, to_addrs=[to_addr])
        return True, "ok"
    except Exception as e:
        _log.warning("invite email send failed: %s", e)
        return False, f"smtp_error: {type(e).__name__}"


@router.post("/request-access")
def request_access(payload: dict = Body(...)):
    """Public endpoint — anonymous user submits an access request.

    Always appends to access_requests.jsonl. Tries to email when SMTP env
    vars are configured. Returns 200 either way (form should look successful
    to the requester regardless of admin's email setup).
    """
    first_name = (payload.get("first_name") or "").strip()
    last_name = (payload.get("last_name") or "").strip()
    company = (payload.get("company") or "").strip()
    email = (payload.get("email") or "").strip()
    if not (first_name and last_name and company and email):
        raise HTTPException(status_code=400, detail={"code": "all_fields_required", "message": "Все 4 поля обязательны"})
    if len(first_name) > 80 or len(last_name) > 80 or len(company) > 200 or len(email) > 200:
        raise HTTPException(status_code=413, detail={"code": "field_too_long", "message": "Слишком длинное поле"})
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail={"code": "invalid_email", "message": "Некорректный email"})
    entry = {
        "ts": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
        "first_name": first_name,
        "last_name": last_name,
        "company": company,
        "email": email,
    }
    _ACCESS_REQUESTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_ACCESS_REQUESTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    delivered, email_status = _send_access_request_email(entry)
    return {"ok": True, "email_delivered": delivered, "email_status": email_status}


@router.get("/admin/access-requests")
def list_access_requests(user: User = Depends(get_current_user)):
    """Admin-only — list all access requests, newest first."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admin only")
    if not _ACCESS_REQUESTS_PATH.exists():
        return {"entries": []}
    entries = []
    with open(_ACCESS_REQUESTS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    entries.reverse()
    return {"entries": entries}


def _parse_allowed(s: str | None) -> list[str]:
    if not s:
        return []
    return [k.strip() for k in s.split(",") if k.strip()]


@router.get("/me", response_model=UserOut)
def me(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    _touch_activity(session, user, login=False)
    return _user_out(user)


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
        role=serialize_account_roles(body.roles or [body.role]),
        first_name=(body.first_name or "").strip(),
        last_name=(body.last_name or "").strip(),
        company=(body.company or "").strip(),
        email=(body.email or "").strip(),
    )
    session.add(inv)
    session.commit()
    session.refresh(inv)
    out = _invite_out(inv)
    # Email the code to the invitee when an address is set + SMTP configured.
    if inv.email:
        delivered, status = _send_invite_email(inv.email, inv.code, inv.first_name)
        out.email_sent = delivered
        out.email_status = status
    return out


@router.get("/invites", response_model=list[InviteOut])
def list_invites(
    _: User = Depends(require_admin),
    session: Session = Depends(get_session),
):
    invs = session.scalars(select(Invite).order_by(Invite.created_at.desc())).all()
    return [
        _invite_out(i)
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


# --- Users + demo spend caps (admin only) ------------------------------------

class AdminUserOut(BaseModel):
    id: int
    username: str
    is_admin: bool
    role: str
    created_at: datetime
    spend_limit_usd: float
    spent_usd: float
    # Activity (lifetime).
    last_login: Optional[datetime] = None
    login_count: int = 0
    # Usage over the selected window (`period_days`; 0 = all time).
    period_days: int = 30
    reports: int = 0
    tokens: int = 0
    cost_usd_period: float = 0.0
    llm_seconds: float = 0.0
    active_seconds: float = 0.0
    logins: int = 0


class AnalyticsSummaryOut(BaseModel):
    period_days: int          # 0 = all time
    generated_at: datetime
    total_users: int
    new_users: int            # registered within the period
    active_users: int         # active within the period
    dau: int                  # active today (UTC)
    wau: int                  # active in the last 7 days
    mau: int                  # active in the last 30 days
    reports: int              # period total
    tokens: int               # period total (in + out)
    cost_usd: float           # period total
    llm_hours: float          # period total LLM compute time


class SeriesPointOut(BaseModel):
    day: str                  # UTC ISO date
    active_users: int
    reports: int
    tokens: int
    cost_usd: float


class AnalyticsChatIn(BaseModel):
    message: str
    history: list[dict] = []  # [{role: "user"|"assistant", content: str}], recent turns
    period: str = "30d"


class SetSpendLimitIn(BaseModel):
    spend_limit_usd: float
    reset_spent: bool = False


def _period_to_days(period: str | None) -> int:
    """Map a period selector to a day count. 0 means all-time."""
    return {"7d": 7, "30d": 30, "90d": 90, "all": 0}.get((period or "30d").lower(), 30)


def _cutoff_day(days: int) -> str:
    """UTC ISO day that starts an inclusive `days`-long window ending today.
    days <= 0 returns a floor date so the window covers all history."""
    if days <= 0:
        return "0001-01-01"
    start = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    return start.isoformat()


def _admin_user_out(u: User, agg: dict | None = None, period_days: int = 30) -> AdminUserOut:
    a = agg or {}
    return AdminUserOut(
        id=u.id,
        username=u.username,
        is_admin=u.is_admin,
        role=primary_account_role(normalize_account_roles(getattr(u, "role", None))),
        created_at=u.created_at,
        spend_limit_usd=float(getattr(u, "spend_limit_usd", 0.0) or 0.0),
        spent_usd=float(getattr(u, "spent_usd", 0.0) or 0.0),
        last_login=getattr(u, "last_login", None),
        login_count=int(getattr(u, "login_count", 0) or 0),
        period_days=period_days,
        reports=int(a.get("reports", 0) or 0),
        tokens=int(a.get("tokens", 0) or 0),
        cost_usd_period=float(a.get("cost_usd", 0.0) or 0.0),
        llm_seconds=float(a.get("llm_seconds", 0.0) or 0.0),
        active_seconds=float(a.get("active_seconds", 0.0) or 0.0),
        logins=int(a.get("logins", 0) or 0),
    )


@router.get("/admin/users", response_model=list[AdminUserOut])
def list_users(
    period: str = "30d",
    _: User = Depends(require_admin),
    session: Session = Depends(get_session),
):
    """All accounts with demo spend cap, running spend, and per-user usage over
    the selected window (`period` = 7d | 30d | 90d | all)."""
    rollup.ensure_fresh()
    days = _period_to_days(period)
    cutoff = _cutoff_day(days)
    rows = session.execute(
        select(
            UsageDaily.user_id,
            func.coalesce(func.sum(UsageDaily.reports), 0),
            func.coalesce(func.sum(UsageDaily.tokens_in + UsageDaily.tokens_out), 0),
            func.coalesce(func.sum(UsageDaily.cost_usd), 0.0),
            func.coalesce(func.sum(UsageDaily.llm_seconds), 0.0),
            func.coalesce(func.sum(UsageDaily.active_seconds), 0.0),
            func.coalesce(func.sum(UsageDaily.logins), 0),
        )
        .where(UsageDaily.day >= cutoff)
        .group_by(UsageDaily.user_id)
    ).all()
    agg = {
        r[0]: {
            "reports": r[1],
            "tokens": r[2],
            "cost_usd": r[3],
            "llm_seconds": r[4],
            "active_seconds": r[5],
            "logins": r[6],
        }
        for r in rows
    }
    users = session.scalars(select(User).order_by(User.created_at.desc())).all()
    return [_admin_user_out(u, agg.get(u.id), days) for u in users]


@router.post("/admin/users/{username}/spend-limit", response_model=AdminUserOut)
def set_user_spend_limit(
    username: str,
    body: SetSpendLimitIn,
    _: User = Depends(require_admin),
    session: Session = Depends(get_session),
):
    """Raise/lower a user's demo cap; optionally reset their running spend."""
    u = session.scalar(select(User).where(User.username == username))
    if u is None:
        raise HTTPException(status_code=404, detail="User not found")
    u.spend_limit_usd = max(0.0, float(body.spend_limit_usd))
    if body.reset_spent:
        u.spent_usd = 0.0
    session.commit()
    session.refresh(u)
    return _admin_user_out(u)


@router.get("/admin/analytics/summary", response_model=AnalyticsSummaryOut)
def analytics_summary(
    period: str = "30d",
    _: User = Depends(require_admin),
    session: Session = Depends(get_session),
):
    """Top-line usage metrics for the admin dashboard: DAU/WAU/MAU (fixed
    1/7/30-day windows) plus totals over the selected window. 'Active' = at
    least one LLM call or login that day. All windows are UTC."""
    rollup.ensure_fresh()
    days = _period_to_days(period)
    cutoff = _cutoff_day(days)
    today = datetime.now(timezone.utc).date()

    def _active_since(day_str: str) -> int:
        return int(
            session.scalar(
                select(func.count(func.distinct(UsageDaily.user_id))).where(
                    UsageDaily.day >= day_str,
                    (UsageDaily.calls > 0) | (UsageDaily.logins > 0),
                )
            )
            or 0
        )

    totals = session.execute(
        select(
            func.coalesce(func.sum(UsageDaily.reports), 0),
            func.coalesce(func.sum(UsageDaily.tokens_in + UsageDaily.tokens_out), 0),
            func.coalesce(func.sum(UsageDaily.cost_usd), 0.0),
            func.coalesce(func.sum(UsageDaily.llm_seconds), 0.0),
        ).where(UsageDaily.day >= cutoff)
    ).one()

    total_users = int(session.scalar(select(func.count(User.id))) or 0)
    if days <= 0:
        new_users = total_users
    else:
        cutoff_dt = (datetime.now(timezone.utc) - timedelta(days=days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        new_users = int(
            session.scalar(select(func.count(User.id)).where(User.created_at >= cutoff_dt)) or 0
        )

    return AnalyticsSummaryOut(
        period_days=days,
        generated_at=datetime.now(timezone.utc),
        total_users=total_users,
        new_users=new_users,
        active_users=_active_since(cutoff),
        dau=_active_since(today.isoformat()),
        wau=_active_since((today - timedelta(days=6)).isoformat()),
        mau=_active_since((today - timedelta(days=29)).isoformat()),
        reports=int(totals[0] or 0),
        tokens=int(totals[1] or 0),
        cost_usd=round(float(totals[2] or 0.0), 4),
        llm_hours=round(float(totals[3] or 0.0) / 3600.0, 2),
    )


@router.get("/admin/analytics/series", response_model=list[SeriesPointOut])
def analytics_series(
    period: str = "30d",
    _: User = Depends(require_admin),
    session: Session = Depends(get_session),
):
    """Daily trend series (zero-filled) for the charts: active users, reports,
    tokens, cost per UTC day. 'all' is capped at 90 days for a readable axis."""
    rollup.ensure_fresh()
    days = _period_to_days(period)
    eff_days = days if days > 0 else 90
    cutoff = _cutoff_day(eff_days)
    rows = session.execute(
        select(
            UsageDaily.day,
            func.coalesce(
                func.sum(case((or_(UsageDaily.calls > 0, UsageDaily.logins > 0), 1), else_=0)), 0
            ),
            func.coalesce(func.sum(UsageDaily.reports), 0),
            func.coalesce(func.sum(UsageDaily.tokens_in + UsageDaily.tokens_out), 0),
            func.coalesce(func.sum(UsageDaily.cost_usd), 0.0),
        )
        .where(UsageDaily.day >= cutoff)
        .group_by(UsageDaily.day)
    ).all()
    by_day = {r[0]: r for r in rows}
    today = datetime.now(timezone.utc).date()
    out: list[SeriesPointOut] = []
    for i in range(eff_days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        r = by_day.get(d)
        out.append(
            SeriesPointOut(
                day=d,
                active_users=int(r[1]) if r else 0,
                reports=int(r[2]) if r else 0,
                tokens=int(r[3]) if r else 0,
                cost_usd=round(float(r[4]), 4) if r else 0.0,
            )
        )
    return out


_ANALYTICS_SYSTEM_PROMPT = """Ты — ассистент-аналитик сервиса Finengine (извлечение финансовых \
показателей из отчётов компаний: PDF → таблица метрик, плюс чат по документам). Отвечаешь \
администратору на вопросы о метриках использования сервиса и о том, как сервис устроен.

Правила:
- У тебя есть только агрегированные данные использования (ниже). Отвечай по ним; не выдумывай \
числа, которых нет. Если данных не хватает — так и скажи.
- Режим «только чтение»: ты не можешь менять лимиты, удалять пользователей или выполнять \
действия. Если просят действие — подскажи, где это в интерфейсе (лимиты — раздел «Инвайты»).
- Отвечай кратко, по делу. Числа бери из данных.

Значение метрик:
- DAU/WAU/MAU — уникальные активные пользователи за 1/7/30 дней (UTC); «активен» = ≥1 вызов LLM \
или вход за день.
- «Отчёт» — один прогон извлечения по документу.
- «Токены» — входные+выходные токены LLM; «стоимость» — оценка в USD по тарифам моделей.
- «LLM-часы» — машинное время ответов модели; «~сессия» — приблизительное активное время \
(кластеризация по паузам >30 мин).
- Лимит трат — демо-кап в USD на пользователя (по умолчанию $5); админы без лимита."""


def _build_analytics_context(session: Session, days: int) -> str:
    cutoff = _cutoff_day(days)
    today = datetime.now(timezone.utc).date()

    def _active_since(day_str: str) -> int:
        return int(
            session.scalar(
                select(func.count(func.distinct(UsageDaily.user_id))).where(
                    UsageDaily.day >= day_str,
                    or_(UsageDaily.calls > 0, UsageDaily.logins > 0),
                )
            )
            or 0
        )

    totals = session.execute(
        select(
            func.coalesce(func.sum(UsageDaily.reports), 0),
            func.coalesce(func.sum(UsageDaily.tokens_in + UsageDaily.tokens_out), 0),
            func.coalesce(func.sum(UsageDaily.cost_usd), 0.0),
            func.coalesce(func.sum(UsageDaily.llm_seconds), 0.0),
        ).where(UsageDaily.day >= cutoff)
    ).one()
    total_users = int(session.scalar(select(func.count(User.id))) or 0)

    lines = [
        ("Период: всё время." if days <= 0 else f"Период: последние {days} дней (UTC)."),
        f"Пользователей всего: {total_users}.",
        f"DAU={_active_since(today.isoformat())}, "
        f"WAU={_active_since((today - timedelta(days=6)).isoformat())}, "
        f"MAU={_active_since((today - timedelta(days=29)).isoformat())}.",
        f"За период: отчётов={int(totals[0])}, токенов={int(totals[1])}, "
        f"стоимость=${float(totals[2]):.2f}, LLM-часов={float(totals[3]) / 3600:.1f}.",
        "",
        "Топ пользователей за период (логин · отчёты · токены · $ · посл.вход):",
    ]
    urows = session.execute(
        select(
            User.username,
            User.last_login,
            func.coalesce(func.sum(UsageDaily.reports), 0),
            func.coalesce(func.sum(UsageDaily.tokens_in + UsageDaily.tokens_out), 0),
            func.coalesce(func.sum(UsageDaily.cost_usd), 0.0),
        )
        .join(User, User.id == UsageDaily.user_id)
        .where(UsageDaily.day >= cutoff)
        .group_by(User.username, User.last_login)
        .order_by(func.sum(UsageDaily.reports).desc())
        .limit(30)
    ).all()
    for u in urows:
        last = u[1].date().isoformat() if u[1] else "—"
        lines.append(f"- {u[0]}: {int(u[2])} · {int(u[3])} · ${float(u[4]):.2f} · {last}")
    return "\n".join(lines)


@router.post("/admin/analytics/chat")
async def analytics_chat(
    body: AnalyticsChatIn,
    user: User = Depends(require_admin),
    session: Session = Depends(get_session),
):
    """Read-only analytics Q&A (admin). Streams an LLM answer over the current
    aggregated usage data + a description of the service. SSE: text_delta/done/
    error frames, matching the frontend streamSSE consumer."""
    rollup.ensure_fresh()
    days = _period_to_days(body.period)
    context = _build_analytics_context(session, days)  # read DB before streaming
    system_prompt = (
        _ANALYTICS_SYSTEM_PROMPT
        + "\n\n# Текущие данные\n"
        + context
        + "\n\n"
        + language_directive()
    )

    marks = scaffold()
    parts: list[str] = []
    for h in (body.history or [])[-6:]:
        role = marks["role_user"] if (h.get("role") == "user") else marks["role_assistant"]
        content = str(h.get("content") or "").strip()
        if content:
            parts.append(f"{role}: {content}")
    parts.append(f"{marks['role_user']}: {body.message.strip()}")
    prompt = "\n\n".join(parts)
    uid = user.id

    async def gen():
        from finengine.llm import openrouter
        from finengine.llm.tracer import set_trace_context

        set_trace_context(kind="analytics_chat", user_id=uid)
        try:
            async for delta in openrouter.stream(
                prompt=prompt,
                system_prompt=system_prompt,
                model=settings.openrouter_model,
                max_output=2000,
            ):
                if delta:
                    yield f"data: {json.dumps({'type': 'text_delta', 'text': delta}, ensure_ascii=False)}\n\n".encode()
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n".encode()
        yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n".encode()

    return StreamingResponse(gen(), media_type="text/event-stream")
