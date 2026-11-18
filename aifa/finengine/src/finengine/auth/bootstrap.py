"""Seed the very first admin user if the users table is empty."""
from __future__ import annotations
import secrets
import sys

from sqlalchemy import select

from finengine.auth.db import SessionLocal, User, init_db
from finengine.auth.security import hash_password
from finengine.config import settings


def bootstrap_admin() -> None:
    """Idempotent: only seeds when the users table is empty."""
    init_db()
    with SessionLocal() as session:
        if session.scalar(select(User).limit(1)) is not None:
            return

        password = settings.admin_password
        generated = False
        if not password:
            password = secrets.token_urlsafe(18)
            generated = True

        admin = User(
            username=settings.admin_username,
            password_hash=hash_password(password),
            is_admin=True,
        )
        session.add(admin)
        session.commit()

        msg = f"[auth] bootstrap: created admin user '{settings.admin_username}'"
        if generated:
            # Print to stderr so it lands in container logs (Dozzle).
            # The password is printed exactly once; it is not stored anywhere
            # else. Owner should copy it to a password manager.
            msg += f" with generated password: {password}"
            msg += " — copy this NOW (logged once, not retrievable)."
        print(msg, file=sys.stderr, flush=True)
