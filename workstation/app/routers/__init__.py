"""API router aggregator."""

from fastapi import APIRouter

from app.routers.common import router as common_router
from app.routers.v1 import router as v1_router

api_router = APIRouter()
api_router.include_router(common_router)
api_router.include_router(v1_router)

__all__ = ["api_router"]
