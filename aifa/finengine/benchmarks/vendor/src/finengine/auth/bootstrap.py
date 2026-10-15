"""Seed the very first admin user if the users table is empty."""
from __future__ import annotations
import secrets
import sys

from sqlalchemy import select

from finengine.auth.db import SessionLocal, User, init_db
from finengine.auth.security import hash_password
from finengine.config import settings
from finengine.roles.access import EXTRACTION_ROLES


def bootstrap_admin() -> None:
    """Idempotent: only seeds when the users table is empty."""
    init_db()
    with SessionLocal() as session:
        has_users = session.scalar(select(User).limit(1)) is not None

        if not has_users:
            password = settings.admin_password
            generated = False
            if not password:
                password = secrets.token_urlsafe(18)
                generated = True

            admin = User(
                username=settings.admin_username,
                password_hash=hash_password(password),
                is_admin=True,
                role="other",
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

        if settings.seed_role_test_users:
            _seed_role_test_users(session)


def _seed_role_test_users(session) -> None:
    password_hash = hash_password(settings.seed_role_test_password)
    premium_grants = "kimi,glm,gigachat,consensus"

    accounts: list[tuple[str, bool, str, str]] = []
    accounts.extend((f"admin{i}", True, "other", premium_grants) for i in range(1, 7))
    accounts.extend((f"role_{role}", False, role, "") for role in EXTRACTION_ROLES)

    created: list[str] = []
    for username, is_admin, role, allowed_models in accounts:
        if session.scalar(select(User).where(User.username == username)) is not None:
            continue
        session.add(
            User(
                username=username,
                password_hash=password_hash,
                is_admin=is_admin,
                role=role,
                allowed_models=allowed_models,
            )
        )
        created.append(username)

    if created:
        session.commit()
        print(
            "[auth] bootstrap: created role test users "
            f"{', '.join(created)} with password from FINENGINE_SEED_ROLE_TEST_PASSWORD",
            file=sys.stderr,
            flush=True,
        )
