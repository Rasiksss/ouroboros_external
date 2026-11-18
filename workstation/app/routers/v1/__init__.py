"""Versioned API router."""

from fastapi import APIRouter

from app.routers.v1.session import router as sessions_router

router = APIRouter()
router.include_router(sessions_router)

__all__ = ["router"]
