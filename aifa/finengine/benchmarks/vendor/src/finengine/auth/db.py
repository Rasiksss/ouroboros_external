"""SQLAlchemy models + session for auth (sqlite, sync — invite-only is low write)."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import String, Boolean, Float, Integer, ForeignKey, DateTime, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker, Session

from finengine.config import settings


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # Comma-separated keys of premium models the user is allowed to switch to
    # (e.g. "kimi,glm"). The base model is always available — gating only
    # controls extras. Set on registration from the invite that the user used.
    allowed_models: Mapped[str] = mapped_column(String(255), default="")
    # Comma-separated account business role grants. Admin status is still
    # `is_admin`; admins keep role="other" and can choose any extraction role
    # at request time.
    role: Mapped[str] = mapped_column(String(255), default="other")
    # Demo spend cap in USD. Non-admin accounts are blocked from starting new
    # LLM work once spent_usd >= spend_limit_usd. Admins raise the cap per user
    # in the panel. spent_usd is a running counter incremented per LLM call.
    spend_limit_usd: Mapped[float] = mapped_column(Float, default=5.0)
    spent_usd: Mapped[float] = mapped_column(Float, default=0.0)
    # Contact fields — copied from the invite on registration (or set by the
    # admin). first_name/last_name mirror the public /request-access form.
    first_name: Mapped[str] = mapped_column(String(128), default="")
    last_name: Mapped[str] = mapped_column(String(128), default="")
    company: Mapped[str] = mapped_column(String(255), default="")
    email: Mapped[str] = mapped_column(String(320), default="")
    # Activity tracking (analytics). last_login = most recent authenticated
    # activity (stamped on /login and /me); login_count = session starts
    # (explicit /login, or a cookie-resumed session on a new day).
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    login_count: Mapped[int] = mapped_column(Integer, default=0)


class Invite(Base):
    __tablename__ = "invites"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    used_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Comma-separated premium model keys to grant when this invite is redeemed.
    allowed_models: Mapped[str] = mapped_column(String(255), default="")
    # Comma-separated business roles granted to the new account when this
    # invite is redeemed.
    role: Mapped[str] = mapped_column(String(255), default="other")
    # Invitee contact details — shown in the admin panel and copied to the User
    # on registration. Prefilled from an access request, or typed by the admin.
    # The code is emailed to `email` (when set + SMTP configured).
    first_name: Mapped[str] = mapped_column(String(128), default="")
    last_name: Mapped[str] = mapped_column(String(128), default="")
    company: Mapped[str] = mapped_column(String(255), default="")
    email: Mapped[str] = mapped_column(String(320), default="")

    created_by: Mapped["User"] = relationship(foreign_keys=[created_by_id])
    used_by: Mapped[Optional["User"]] = relationship(foreign_keys=[used_by_id])


class UsageDaily(Base):
    """Per-user, per-day usage rollup. Trace-derived columns (calls, reports,
    tokens, cost, llm_seconds, active_seconds) are filled nightly by
    `analytics.rollup` from the llm_traces JSONL; `logins` is counted live by
    the auth routes. Persisting the rollup keeps analytics history durable even
    after old trace files are deleted. `day` is a UTC ISO date (YYYY-MM-DD),
    matching the trace file partition; ISO strings sort chronologically so date
    ranges are plain string comparisons."""
    __tablename__ = "usage_daily"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True, index=True)
    # Trace-derived — owned by analytics.rollup (recomputed idempotently).
    calls: Mapped[int] = mapped_column(Integer, default=0)
    reports: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    tokens_cached: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    llm_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    active_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    # Live — owned by the auth routes (rollup must never overwrite this).
    logins: Mapped[int] = mapped_column(Integer, default=0)


# Engine + session. SQLite path comes from settings (lives inside the data
# volume so it is part of the daily backup).
_db_path = settings.auth_db_path.resolve()
_db_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{_db_path}",
    echo=False,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
    # Lightweight migration — add missing columns on already-deployed DBs.
    # SQLAlchemy's create_all only creates new tables, it doesn't ALTER. The
    # auth_db_path file persists in the docker volume and outlives image
    # rebuilds, so we patch it here on startup.
    from sqlalchemy import text
    with engine.begin() as conn:
        for table, column, ddl in (
            ("users", "allowed_models", "ALTER TABLE users ADD COLUMN allowed_models VARCHAR(255) DEFAULT ''"),
            ("invites", "allowed_models", "ALTER TABLE invites ADD COLUMN allowed_models VARCHAR(255) DEFAULT ''"),
            ("users", "role", "ALTER TABLE users ADD COLUMN role VARCHAR(255) DEFAULT 'other'"),
            ("invites", "role", "ALTER TABLE invites ADD COLUMN role VARCHAR(255) DEFAULT 'other'"),
            ("users", "spend_limit_usd", "ALTER TABLE users ADD COLUMN spend_limit_usd FLOAT DEFAULT 5.0"),
            ("users", "spent_usd", "ALTER TABLE users ADD COLUMN spent_usd FLOAT DEFAULT 0.0"),
            ("users", "first_name", "ALTER TABLE users ADD COLUMN first_name VARCHAR(128) DEFAULT ''"),
            ("users", "last_name", "ALTER TABLE users ADD COLUMN last_name VARCHAR(128) DEFAULT ''"),
            ("users", "company", "ALTER TABLE users ADD COLUMN company VARCHAR(255) DEFAULT ''"),
            ("users", "email", "ALTER TABLE users ADD COLUMN email VARCHAR(320) DEFAULT ''"),
            ("invites", "first_name", "ALTER TABLE invites ADD COLUMN first_name VARCHAR(128) DEFAULT ''"),
            ("invites", "last_name", "ALTER TABLE invites ADD COLUMN last_name VARCHAR(128) DEFAULT ''"),
            ("invites", "company", "ALTER TABLE invites ADD COLUMN company VARCHAR(255) DEFAULT ''"),
            ("invites", "email", "ALTER TABLE invites ADD COLUMN email VARCHAR(320) DEFAULT ''"),
            ("users", "last_login", "ALTER TABLE users ADD COLUMN last_login DATETIME"),
            ("users", "login_count", "ALTER TABLE users ADD COLUMN login_count INTEGER DEFAULT 0"),
        ):
            cols = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            existing = {c[1] for c in cols}
            if column not in existing:
                conn.execute(text(ddl))


def get_session() -> Session:
    """FastAPI dependency. Closes session after the request."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
