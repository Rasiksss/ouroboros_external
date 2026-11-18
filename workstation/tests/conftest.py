"""Pytest fixtures: test DB (SQLite in-memory) and AsyncClient."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("RUNTIME_MODE", "local")
os.environ.setdefault("METRICS_ENABLED", "true")
# Audit is gated off in prod for SQLite write-lock reasons; tests still assert
# audit rows are produced, so opt them back in here.
os.environ.setdefault("AUDIT_ENABLED", "true")
# Same idea for touch debouncing: tests rely on per-request updates landing
# immediately, so disable the debounce window in the test process.
os.environ.setdefault("SESSION_TOUCH_DEBOUNCE_SEC", "0")
os.environ.setdefault(
    "ROOTFS_ROOT",
    os.path.join(tempfile.gettempdir(), "workstation-bwrap-test-rootfs"),
)
os.environ.setdefault(
    "SANDBOX_BINARY",
    os.path.join(os.path.dirname(__file__), "mocks", "fake_sandbox"),
)
os.environ.setdefault(
    "ROOTFS_TEMPLATE_DIR",
    os.path.join(tempfile.gettempdir(), "workstation-bwrap-test-template"),
)

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.db import get_session
from app.main import app as fastapi_app
from app.models import AuditEvent, Base, Session  # noqa: F401


@pytest_asyncio.fixture(scope="session")
async def engine():
    # StaticPool keeps a single SQLite connection so all sessions see the same
    # in-memory DB. Without it, each new connection opens a fresh empty DB.
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    """Per-test session. Truncates tables after each test to keep isolation."""
    template_root = Path(os.environ["ROOTFS_TEMPLATE_DIR"])
    template_root.mkdir(parents=True, exist_ok=True)
    (template_root / "bin").mkdir(exist_ok=True)
    (template_root / ".template-marker").write_text("runtime-template", encoding="utf-8")

    maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with maker() as sess:
        try:
            yield sess
        finally:
            await sess.close()
    # Clean all rows after each test (cheap on in-memory SQLite).
    async with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())
    shutil.rmtree(os.environ["ROOTFS_ROOT"], ignore_errors=True)
    shutil.rmtree(os.environ["ROOTFS_TEMPLATE_DIR"], ignore_errors=True)


@pytest.fixture
def app(session: AsyncSession) -> FastAPI:
    """FastAPI app with `get_session` overridden to the per-test session."""

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        yield session

    fastapi_app.dependency_overrides[get_session] = _override_get_session
    try:
        yield fastapi_app
    finally:
        fastapi_app.dependency_overrides.pop(get_session, None)


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
